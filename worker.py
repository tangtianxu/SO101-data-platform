"""One short-lived hardware owner. Reuses verified LeRobot/safety code lazily.

No device is accessed by importing this module. The desktop host starts a worker
only after an explicit UI request. A closed stdin is a normal stop request.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import traceback
from types import SimpleNamespace

from common import NAMES, atomic_json, environment, number, robot_path, require_capability, read_calibration
from reset_profile import effective_speeds

environment()
PRINT_LOCK = threading.Lock()


def command_lines(stop):
    """Poll the pipe before os.read, never hold a CRT stdin lock while importing DLLs.

    Blocking TextIOWrapper.readline in a background thread can deadlock NumPy's
    native initialization on Windows. PeekNamedPipe is only IPC, not device IO.
    """
    pending = b""
    fd = sys.stdin.fileno()
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        peek = kernel.PeekNamedPipe
        peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
        peek.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(fd)
        def available():
            count = wintypes.DWORD()
            if not peek(handle, None, 0, None, ctypes.byref(count), None):
                if ctypes.get_last_error() in (109, 232):
                    return -1
                raise ctypes.WinError(ctypes.get_last_error())
            return count.value
    else:
        import select
        def available():
            return 65536 if select.select([fd], [], [], 0)[0] else 0
    while not stop.is_set():
        count = available()
        if count < 0:
            return
        if not count:
            stop.wait(.025)
            continue
        data = os.read(fd, min(count, 65536))
        if not data:
            return
        pending += data
        if len(pending) > 65536:
            raise ValueError("后台控制消息超出大小限制")
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            yield line.decode("utf-8")


def event(event_type, **values):
    with PRINT_LOCK:
        print("SO101_EVENT " + json.dumps({"type": event_type, **values}, ensure_ascii=False, allow_nan=False), flush=True)


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stop, self.start, self.hold = threading.Event(), threading.Event(), threading.Event()
        self.commands = queue.Queue(maxsize=32)
        self.phase = "LOADING"
        self.session = None
        self.last_preview = -float("inf")
        self.last_telemetry = -float("inf")
        self.lock = threading.Lock()

    def state(self, phase, message="", **values):
        self.phase = phase
        event("phase", phase=phase, message=message or phase, **values)

    def emit(self, kind, **values):
        if kind == "preview":
            self.preview(values["frames"])
        elif kind == "telemetry":
            if time.perf_counter() - self.last_telemetry >= .2:
                self.last_telemetry = time.perf_counter()
                event("telemetry", **values)
        elif kind == "stage":
            self.state(values.pop("stage").upper(), **values)
        elif kind == "finished":
            event("phase", phase="FINISHING", message="模型已停止，正在收尾", outcome=values["report"]["outcome"])
        elif kind == "error":
            event("error", **values)
        else:
            event("phase", phase=self.phase, **values)

    def preview(self, frames):
        now = time.perf_counter()
        if now - self.last_preview < 1 / self.cfg["preview_hz"]:
            return
        self.last_preview = now
        import cv2
        for name, frame in frames.items():
            if name not in ("top", "wrist"):
                continue
            image = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            if image.shape[1] > 640:
                image = cv2.resize(image, (640, 480))
            ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 72])
            if ok:
                event("frame", camera=name, data=base64.b64encode(encoded).decode("ascii"))

    def read_commands(self):
        try:
            for line in command_lines(self.stop):
                try:
                    cmd = json.loads(line)
                    name = cmd.get("command")
                    if name == "stop":
                        if self.session is not None:
                            self.session.request_stop()
                        else:
                            self.stop.set()
                        self.state("STOPPING", "已请求停止，等待保存和最终设备反馈")
                    elif self.session is not None and name in ("scene", "start", "advance"):
                        s = self.session
                        if name == "scene" and s.phase == "SCENE_CHECK":
                            s.scene_approved.set()
                        elif name == "start" and s.phase == "WAITING_START":
                            s.recording_start.set()
                        elif name == "advance":
                            s.request_advance(expected_phase=cmd.get("phase"), source="desktop_button")
                    elif name == "start" and self.phase == "READY":
                        self.start.set()
                    elif name == "hold" and self.phase == "READY":
                        self.hold.set()
                    else:
                        self.commands.put_nowait(cmd)
                except (ValueError, queue.Full) as exc:
                    event("phase", phase=self.phase, message=f"忽略无效或过多请求：{exc}")
        finally:
            if self.session is not None:
                self.session.request_stop()
            else:
                self.stop.set()


def ports_for(cfg, roles=("follower",)):
    from serial.tools.list_ports import comports
    ports = list(comports())
    result = {}
    for role in roles:
        expected = cfg[role + "_usb_serial"]
        if not expected or expected.startswith('SET_'):
            raise ValueError(role + ' USB 身份尚未配置，不猜测串口')
        matches = [p.device for p in ports if p.serial_number == expected]
        if len(matches) != 1:
            raise RuntimeError(f"{role} USB 身份不唯一或未连接：{matches}，未发送动作")
        result[role] = matches[0]
        if cfg.get(role+'_port','auto') not in ('auto',matches[0]):
            raise RuntimeError(role+' 显式串口与 USB 身份不一致，未连接设备')
    return result


def install_escape(bridge):
    from pynput import keyboard
    def on_press(key):
        if key == keyboard.Key.esc:
            if bridge.session:
                bridge.session.request_stop()
            else:
                bridge.stop.set()
    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    return listener


def preview_only(cfg, bridge, scan=False):
    import cv2
    if scan:
        bridge.state("SCANNING", "正在检查常用及已配置相机索引；仅打开视频，不访问舵机")
        found = []
        for index in sorted(set(range(6)) | {cfg['top_camera'],cfg['wrist_camera']}):
            if bridge.stop.is_set():
                break
            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            try:
                if cap.isOpened():
                    ok, frame = cap.read()
                    if ok:
                        found.append({"index": index, "width": frame.shape[1], "height": frame.shape[0]})
            finally:
                cap.release()
        event("cameras", cameras=found)
        return {"outcome": "camera_scan", "cameras": found, "servo_accessed": False}
    caps = {}
    try:
        for name in ("top", "wrist"):
            cap = cv2.VideoCapture(cfg[name + "_camera"], cv2.CAP_DSHOW)
            caps[name] = cap
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS, 30)
            if not cap.isOpened():
                raise RuntimeError(f"无法打开 {name}；检查设备占用和配置")
        bridge.state("PREVIEW", "双相机预览；未连接机械臂，不录数据")
        while not bridge.stop.is_set():
            frames = {}
            for name, cap in caps.items():
                ok, frame = cap.read()
                if not ok:
                    raise RuntimeError(f"{name} 采集失败；结束预览")
                frames[name] = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            bridge.preview(frames)
    finally:
        for cap in caps.values():
            cap.release()
    return {"outcome": "preview_stopped", "servo_accessed": False}


def bus_from_config(cfg, role="follower"):
    from lerobot.motors import Motor, MotorCalibration, MotorNormMode
    from lerobot.motors.feetech import FeetechMotorsBus
    port = ports_for(cfg, (role,))[role]
    cal_path = cfg['calibration' if role == 'follower' else 'leader_calibration']
    cal = read_calibration(cal_path)
    bus = FeetechMotorsBus(port, {n: Motor(cal[n]["id"], "sts3215",
        MotorNormMode.RANGE_0_100 if n == "gripper" else MotorNormMode.DEGREES) for n in NAMES},
        calibration={n: MotorCalibration(**cal[n]) for n in NAMES})
    return bus, cal, port


def read_health(bus):
    health = {}
    for register, key in (("Present_Temperature", "temperature"), ("Torque_Enable", "torque"),
                          ("Status", "status"), ("Present_Voltage", "voltage_raw"),
                          ("Present_Current", "current_raw"), ("Present_Load", "load_raw")):
        health[key] = bus.sync_read(register, normalize=False, num_retry=1)
    return health


def monitor_or_control(cfg, options, bridge, out, kind):
    import numpy as np
    safety = __import__("so101_guarded_real_act") if kind != "monitor" else None
    overload = cfg.get('gripper_overload_policy','stop') == 'log_gripper_only'
    bus, cal, port = bus_from_config(cfg)
    if safety:
        safety.guard_bus_writes(bus)
    else:
        def reject(*a, **kw):
            raise RuntimeError("只读监控禁止写舵机寄存器")
        bus.write = bus.sync_write = reject
    motion, listener, final = None, None, None
    try:
        listener = install_escape(bridge)
        bus.connect(handshake=False)
        for register, field in (("Homing_Offset", "homing_offset"), ("Min_Position_Limit", "range_min"), ("Max_Position_Limit", "range_max")):
            actual = bus.sync_read(register, normalize=False, num_retry=1)
            if any(actual[n] != cal[n][field] for n in NAMES):
                raise RuntimeError("标定不一致，未写入任何参数")
        if safety:
            safety.preflight(bus, cal, 1., bridge.stop, allow_gripper_overload=overload)
            motion = safety.Motion(bus, cal, bridge.stop, soft_interlocks=False,
                                   temperature_confirmation_s=1., allow_gripper_overload=overload)
            import software_limits
            manual_bounds=software_limits.effective(cfg,cal)
            software_limits.guard_motion(motion,manual_bounds)
        bridge.state("MONITOR" if kind == "monitor" else "CONTROL", "从臂只读反馈已连接；尚未改变力矩或位置", port=port)
        if options.get("initial_command"):
            bridge.commands.put({"command": options["initial_command"], "confirmed": options.get("confirmed")})
        last_health, health = -float("inf"), None
        while not bridge.stop.is_set():
            tick = time.perf_counter()
            state = bus.sync_read("Present_Position", num_retry=1)
            if tick - last_health >= 1:
                health = read_health(bus)
                last_health = tick
            bridge.emit("telemetry", state=[state[n] for n in NAMES], health=health, port=port)
            try:
                cmd = bridge.commands.get(timeout=.1)
            except queue.Empty:
                continue
            name = cmd.get("command")
            if not safety:
                event("phase", phase=bridge.phase, message="只读会话不接受运动请求；请停止后连接基本控制")
                continue
            if cmd.get("confirmed") is not True:
                raise ValueError("操作未确认")
            if name == "hold":
                safety.preflight(bus, cal, 1., bridge.stop, allow_gripper_overload=overload)
                motion.start_hold()
                bridge.state("CONTROL", "当前位置保持已确认；可以移开支撑")
            elif name == "release":
                bus.sync_write("Torque_Enable", dict.fromkeys(NAMES, 0), normalize=False)
                if any(bus.sync_read("Torque_Enable", normalize=False, num_retry=1).values()):
                    raise RuntimeError("没有全部确认力矩关闭")
                motion.enabled = False
                bridge.state("CONTROL", "已回读确认六个舵机力矩关闭；可能仍有被动摩擦，必须可靠支撑，不保证自由下垂")
            elif name == "jog":
                if not motion.enabled:
                    raise ValueError("请先支撑并执行当前位置保持，再进行点动")
                joint = number(cmd.get("joint"), 0, 5, "关节", True)
                delta = number(cmd.get("delta"), -2, 2, "点动量")
                start = np.array([state[n] for n in NAMES], dtype=float)
                target = start.copy()
                target[joint] += delta
                software_limits.check_positions(start,manual_bounds,'当前位置')
                software_limits.check_positions(target,manual_bounds,'点动目标')
                # Explicit small-step jog, not an uncontrolled target-position slider.
                for weight in np.linspace(0, 1, 16)[1:]:
                    motion.send(start + (target - start) * weight)
            elif name == "reset":
                reference=safety.load_reference_start(cfg["reference_start_pose"],cal)
                software_limits.check_positions([state[n] for n in NAMES],manual_bounds,'复位起始位置')
                software_limits.check_positions(reference,manual_bounds,'参考起点')
                if not motion.enabled:
                    motion.start_hold()
                safety.reset_to_reference(motion, reference,
                                          out / ("reset_" + str(time.time_ns()) + ".csv"), {}, emit=bridge.emit,
                                          speeds=effective_speeds(cfg))
                bridge.state("CONTROL", "已到参考起点，继续保持力矩")
            elif name == 'joint_target':
                from joint_control import execute
                try:
                    result=execute(motion,cal,cmd,out,bridge.emit,cfg)
                    message='已确认选中关节到达目标，继续保持力矩' if result['outcome']=='reached' else '目标已下发，但未在等待时间内确认到位；未自动加力或反复追赶，请检查残差'
                    bridge.state('CONTROL',message,target_pending=False,target_result=result)
                except ValueError as error:
                    bridge.state('CONTROL','目标未执行：'+str(error),target_pending=False,target_result={'outcome':'rejected','error':str(error)})
            if options.get("one_shot"):
                health = read_health(bus)
                position = bus.sync_read("Present_Position", num_retry=1)
                event("telemetry", state=[position[n] for n in NAMES], health=health, port=port)
                bridge.state("CONTROL", "力矩已释放，必须继续支撑" if name == "release" else "操作已完成，正在保存最终反馈；继续保持力矩")
                break
        return {"outcome": "stopped", "port": port, "calibration_or_pid_writes": False,
                "last_action": options.get("initial_command"),
                "torque_released": options.get("initial_command") == "release" and not motion.enabled if motion else None}
    finally:
        try:
            if motion and motion.enabled and bus.is_connected:
                final = motion.hold_on_exit()
        finally:
            try:
                if bus.is_connected:
                    bus.disconnect(disable_torque=False)
            finally:
                if listener:
                    listener.stop()
                atomic_json(out / "final_feedback.json", {"feedback": final, "torque_released_automatically": False})


def await_capture_group(session,rec):
    """Pause on group changes, with existing low-speed following and an explicit start."""
    session.recording_start.clear();session.start_gate_completed=False
    last=time.perf_counter();residual=None
    def approach():
        nonlocal last,residual
        observation=session.robot.get_observation()
        _,target,_=rec.clip_target(session.leader.get_action(),session.robot.limits)
        residual=[abs(float(observation[n+'.pos'])-float(v)) for n,v in zip(NAMES,target)]
        now=time.perf_counter();step=rec.catchup_step(session.motion.last_target,target,now-last);last=now
        session.robot.send_action({n+'.pos':float(v) for n,v in zip(NAMES,step)})
    rec.await_first_record_start(session,approach)
    # Finalization may pause writes while the leader is moved. Never jump straight from
    # that stale target to unrestricted following; retain the existing startup tolerances.
    if session.stop.is_set():raise KeyboardInterrupt
    if residual is None or any(e>t for e,t in zip(residual,[3.]*5+[5.])):
        session.state('GROUP_CATCHUP')
        stable=0
        while stable<3:
            if session.stop.is_set():raise KeyboardInterrupt
            approach()
            stable=stable+1 if all(e<=t for e,t in zip(residual,[3.]*5+[5.])) else 0


def record_or_teleop(cfg, options, bridge, out, kind):
    import so101_record_supplement as rec
    from lerobot.configs.dataset import DatasetRecordConfig
    from lerobot.configs.video import RGBEncoderConfig
    official = rec.official
    from sensors import FeedbackSampler, features as sensor_features
    original = {k: getattr(official, k) for k in ("make_robot_from_config", "make_teleoperator_from_config",
                "record_loop", "init_keyboard_listener", "log_say", "combine_feature_dicts", "build_dataset_frame")}
    labels = {"SCENE_CHECK": "核对双相机、手、线缆和支撑；确认后才启动同步", "SYNCING": "首次同步：从臂靠近主臂，5°/秒，夹爪 15 百分点/秒",
              "SYNCED": "同步完成", "WAITING_START": "未采集，低速跟随中；请点击开始", "COUNTDOWN": "3 秒倒计时后开始",
              "RECORDING": "START · 抓放任务，正在录制", "SAVING": "END · 正在保存本条", "RESET": "END · 遥操复位/摆物，不录入任务数据"}
    class WebSession(rec.Session):
        def state(self, phase, duration=None, **extra):
            extra={**getattr(self,'capture_group_data',{}),**extra}
            super().state(phase, duration, **extra)
            bridge.state(phase, labels.get(phase, phase), episode=self.episode,
                         deadline=time.time()+duration if duration is not None else None, **extra)

        def row(self, requested, sent, clipped):
            super().row(requested, sent, clipped)
            self.last_requested, self.last_sent, self.last_clipped = requested.tolist(), sent.tolist(), clipped.tolist()
            now = time.perf_counter()
            if now - bridge.last_telemetry >= .2:
                health = self.robot.feedback.latest["health"] if self.robot.feedback and self.robot.feedback.latest else None
                bridge.emit("telemetry", state=[self.last_obs.get(n) for n in NAMES] if self.last_obs else None,
                            target=sent.tolist(), health=health, frames=self.frames, episode_frames=self.phase_frame_count,
                            camera_age_ms={n: (now-v)*1000 for n, v in (self.last_camera_times or {}).items()})

    s = WebSession(SimpleNamespace(group=options.get("group", "遥操"), episodes=cfg["episodes"],
                dataset_root=Path(options.get("dataset_root", cfg["dataset_home"]))), queue.Queue(maxsize=64))
    s.stop, s.out = bridge.stop, out
    bridge.session = s
    report = {"kind": kind, "dataset_root": options.get("dataset_root"), "servo_parameter_changes": False,
              "sync_once": True, "manual_start": True, "camera_mapping": {n: cfg[n+"_camera"] for n in ("top", "wrist")}}
    diag, listener, sensor_log = None, None, None
    preview_stop = threading.Event()

    def preview_pump():
        while not preview_stop.wait(1/cfg["preview_hz"]):
            # Read only the camera's latest buffer. Never open a second capture or touch a servo here.
            if s.robot:
                frames = {}
                for name, cam in s.robot.cameras.items():
                    with cam.frame_lock:
                        if cam.latest_frame is not None:
                            frames[name] = cam.latest_frame.copy()
                bridge.preview(frames)
            while not s.updates.empty():
                try:
                    s.updates.get_nowait()
                except queue.Empty:
                    break

    pump = threading.Thread(target=preview_pump, daemon=True)
    try:
        ports = ports_for(cfg, ("leader", "follower"))
        report["ports"] = ports
        listener, s.events = original["init_keyboard_listener"](allow_episode_navigation=False)
        if listener is None:
            raise RuntimeError("Esc 监听不可用，未开始运动")
        def relay():
            while not preview_stop.wait(.02):
                if s.events["stop_recording"]:
                    s.request_stop()
                    return
        threading.Thread(target=relay, daemon=True).start()
        cameras = {n: rec.OpenCVCameraConfig(index_or_path=cfg[n+"_camera"], fps=30, width=640,
            height=480, backend=700, fourcc="MJPG", color_mode="rgb") for n in ("top", "wrist")}
        follower_cal, leader_cal = Path(cfg['calibration']), Path(cfg['leader_calibration'])
        rcfg = rec.SO101FollowerConfig(port=ports["follower"], id=follower_cal.stem,
            calibration_dir=follower_cal.parent, cameras=cameras, use_degrees=True, disable_torque_on_disconnect=False)
        tcfg = rec.SO101LeaderConfig(port=ports["leader"], id=leader_cal.stem,
            calibration_dir=leader_cal.parent, use_degrees=True)
        class SensorFollower(rec.PreservedFollower):
            feedback = None
            sensor_frame = None

            def get_observation(self):
                observation = super().get_observation()
                if self.feedback is None:
                    self.feedback = FeedbackSampler(self.bus, cfg["sensor_hz"])
                self.sensor_frame = self.feedback.frame()
                return observation

        s.robot, s.leader = SensorFollower(rcfg, s), rec.PreservedLeader(tcfg)
        pump.start()
        diag = (out / "teleoperation.csv").open("w", encoding="utf-8", newline="")
        s.diag = csv.writer(diag)
        s.diag.writerow(["wall_time", "perf_time", "phase", "episode_index", "observation_read_perf",
                         "leader_requested", "follower_sent", "clipped", "follower_measured", "camera_capture_perf"])
        if kind == "teleop":
            s.leader.connect()
            s.robot.connect()
            s.state("FOLLOWING")
            bridge.state("FOLLOWING", "遥操中；未创建数据集，停止后保持从臂力矩")
            while not s.stop.is_set():
                s.robot.get_observation()
                s.robot.send_action(s.leader.get_action())
        else:
            from capture_groups import RecordingLease,ListenerLease
            targets=options.get("capture_targets") or [{"root":options["dataset_root"],"instruction":cfg["task"],"episodes":cfg["episodes"]}]
            grouped=bool(options.get("capture_collection"));completed_in_session=0
            robot_owner=RecordingLease(s.robot) if grouped else s.robot
            leader_owner=RecordingLease(s.leader) if grouped else s.leader
            listener_owner=ListenerLease(listener) if grouped else listener
            for group_index,target in enumerate(targets,1):
                if s.stop.is_set() or s.events["stop_recording"]:break
                root = Path(target["root"])
                resume = (root / "meta/info.json").exists()
                before = json.loads((root / "meta/info.json").read_text(encoding="utf-8"))["total_episodes"] if resume else 0
                report["episodes_before"] = report.get("episodes_before",0)+before
                group_report={"root":str(root),"instruction":target["instruction"],"episodes_before":before,
                              "requested":target["episodes"],"started_perf":time.perf_counter()}
                report.setdefault("groups",[]).append(group_report)
                s.capture_group_data={"group_index":group_index,"group_count":len(targets),
                    "group_task":target["instruction"],"group_requested":target["episodes"],"group_root":str(root)}
                if sensor_log:
                    sensor_log.close();sensor_log=None
                s.episode=before;s.capture_group_data['group_saved']=0
                if group_index>1:
                    await_capture_group(s,rec)
                report["sensor_target_hz"] = cfg["sensor_hz"]
                s.episode = before
                rich = not resume or "observation.sensors.current_raw" in json.loads((root/"meta/info.json").read_text(encoding="utf-8"))["features"]
                report["sensors_in_parquet"] = rich
                group_report['sensors_in_parquet']=rich
                if not rich:
                    event("phase", phase=bridge.phase, message="旧数据集保持原 schema；新增传感器写入 extras/platform，可导出训练，旧条目缺失值不补零")
                def combined(*args, **kwargs):
                    result = original["combine_feature_dicts"](*args, **kwargs)
                    if rich:
                        result.update(sensor_features())
                    return result
                official.combine_feature_dicts = combined
                # Preserve official 6-D observation.state/action, bypass its scalar-name mapper for sensor vectors.
                official.build_dataset_frame = lambda feature_map, values, prefix: original["build_dataset_frame"](
                    {k: v for k, v in feature_map.items() if not k.startswith("observation.sensors.")}, values, prefix)
                official.make_robot_from_config = lambda config: robot_owner
                official.make_teleoperator_from_config = lambda config: leader_owner
                official.init_keyboard_listener = lambda **kw: (listener_owner, s.events)
                official.log_say = lambda *a, **kw: None
                def loop(**kwargs):
                    nonlocal sensor_log
                    dataset = kwargs.get("dataset")
                    if dataset is not None:
                        if sensor_log is None:
                            sensor_dir = root / "extras/platform"
                            sensor_dir.mkdir(parents=True, exist_ok=True)
                            sensor_log = (sensor_dir / (out.name + ".jsonl")).open("w", encoding="utf-8", buffering=1)
                        s.episode = dataset.num_episodes
                        rec.install_idempotent_episode_save(dataset)
                        task = target["instruction"]
                        kwargs["single_task"] = task
                        if not getattr(dataset, "_platform_sensor_hook", False):
                            add_original = dataset.add_frame
                            def add_frame(frame):
                                if s.robot.sensor_frame is None:
                                    raise RuntimeError("扩展反馈未就绪，不写入缺失传感器的伪数据")
                                if rich:
                                    frame.update(s.robot.sensor_frame)
                                task_text = frame["task"]
                                add_original(frame)
                                sensor_log.write(json.dumps({"episode": s.episode, "frame": s.phase_frame_count-1,
                                    "time": (s.phase_frame_count-1)/30, "observation_perf": s.last_obs_time,
                                    "camera_perf": s.last_camera_times, "task": task_text,
                                    "leader_requested": s.last_requested, "follower_sent": s.last_sent, "clipped": s.last_clipped,
                                    "sample": s.robot.feedback.latest}, ensure_ascii=False, allow_nan=False)+"\n")
                            dataset.add_frame = add_frame
                            dataset._platform_sensor_hook = True
                    s.state("RECORDING" if dataset is not None else "RESET", kwargs["control_time_s"])
                    if dataset is not None:
                        event("phase", phase="RECORDING", task=task, message="START · "+task)
                    if dataset is not None:
                        rec.announce_phase("START")
                    result = original["record_loop"](**kwargs)
                    if dataset is not None:
                        s.state("SAVING")
                        rec.announce_phase("END")
                        dataset.save_episode()
                        s.capture_group_data['group_saved']=dataset.num_episodes-before
                        event("saved", total=completed_in_session+dataset.num_episodes-before if grouped else dataset.num_episodes,
                              **s.capture_group_data)
                    return result
                official.record_loop = loop
                config = official.RecordConfig(robot=rcfg, teleop=tcfg,
                    dataset=DatasetRecordConfig(repo_id="local/so101_"+hashlib.sha256(str(root).encode()).hexdigest()[:16], root=root, single_task=target["instruction"],
                        fps=30, episode_time_s=cfg["record_seconds"], reset_time_s=cfg["reset_seconds"],
                        num_episodes=target["episodes"], push_to_hub=False, no_stamp=True,
                        rgb_encoder=RGBEncoderConfig(vcodec="h264", g=30, crf=20, preset="fast"),
                        streaming_encoding=True, encoder_threads=2),
                    display_data=cfg["rerun"], play_sounds=False, allow_episode_navigation=False, resume=resume)
                if not resume and root.exists():
                    # LeRobot.create requires an absent root. Only remove our own empty plan marker,
                    # then rmdir the now-empty exact target, never recursively delete user contents.
                    contents = list(root.iterdir())
                    if contents and (len(contents) != 1 or contents[0].name != ".platform-plan.json"):
                        raise ValueError("新数据目录已变化；拒绝覆盖")
                    if contents:
                        contents[0].unlink()
                    root.rmdir()
                dataset = official.record(config)
                group_report.update(episodes_after=dataset.num_episodes,saved=dataset.num_episodes-before,ended_perf=time.perf_counter())
                completed_in_session+=dataset.num_episodes-before
                report["episodes_after"]=report.get("episodes_after",0)+dataset.num_episodes
                if s.stop.is_set() or s.events["stop_recording"]:break
        report["outcome"] = "stopped" if s.stop.is_set() or s.events["stop_recording"] else "complete"
        return report
    finally:
        preview_stop.set()
        if pump.is_alive():
            pump.join(timeout=2)
        cleanup = []
        for obj in (s.robot, s.leader):
            if obj:
                try:
                    obj.disconnect()
                except Exception as exc:
                    cleanup.append(str(exc))
        for key, value in original.items():
            setattr(official, key, value)
        if listener:
            listener.stop()
        if diag:
            diag.close()
        if sensor_log:
            sensor_log.close()
        report.update(startup_sync_count=s.sync_count, frames=s.frames,
                      exit_health=getattr(s, "exit_health", None), cleanup_errors=cleanup,
                      advances=s.advance_requests, parameters_changed=False,
                      health=s.motion.health_records if s.motion else [])
        atomic_json(out / "record_report.json", report)
        bridge.session = None


def run_act(cfg, options, bridge, out):
    import so101_guarded_real_act as runner
    capture=None;controller=bridge;destination=out/'act'
    if options.get('feedback_root'):
        from policy_capture import PolicyCapture,CaptureController
        capture=PolicyCapture(options['feedback_root'],cfg);controller=CaptureController(bridge,capture)
        destination=Path(options['feedback_root'])/'diagnostics'
    args = ["--execute", "--standalone-cameras", "--output", str(destination),
            "--duration", str(cfg["act_seconds"]), "--port", "auto", "--top-camera", str(cfg["top_camera"]),
            "--wrist-camera", str(cfg["wrist_camera"]), "--checkpoint", cfg["checkpoint"],
            "--calibration", cfg["calibration"], "--control-mode", "standard",
            "--gripper-overload-policy", cfg.get('gripper_overload_policy','stop'), "--reset-before-act",
            "--reference-start-pose", cfg["reference_start_pose"], "--clear-reset-route-confirmed", "--empty-gripper-confirmed"]
    args += ["--reset-speeds", *map(str, effective_speeds(cfg))]
    # The verified runner waits at READY before any hold/reset/model action.
    # The UI's separate start confirmation grants the route/empty-gripper assertions.
    result={'outcome':'not_started'}
    try:
        result = runner.main(args, controller=controller)
    finally:
        if capture:
            feedback=capture.finish(result)
            result.update(feedback_root=options['feedback_root'],feedback_frames=feedback['frames'],feedback_complete=feedback['complete'])
    if result["outcome"] not in ("completed_timed_trial", "user_stopped"):
        raise RuntimeError(result.get("failure", result["outcome"]))
    return result


def replay(cfg, options, bridge, out):
    import numpy as np
    import so101_guarded_real_replay as prepared
    import so101_guarded_real_act as safety
    prepared.ROOT = Path(options["dataset_root"])
    prepared.CALIBRATION = Path(cfg["calibration"])
    commands, calibration, fps, summary = prepared.prepare(int(options["episode"]))
    bus, cal, port = bus_from_config(cfg)
    if calibration != cal:
        raise ValueError("回放标定不一致")
    safety.guard_bus_writes(bus)
    motion = safety.Motion(bus, cal, bridge.stop, soft_interlocks=False,
                           temperature_confirmation_s=1., allow_gripper_overload=cfg.get('gripper_overload_policy','stop')=='log_gripper_only')
    listener = install_escape(bridge)
    try:
        bus.connect(handshake=False)
        safety.preflight(bus, cal, 1., bridge.stop, allow_gripper_overload=cfg.get('gripper_overload_policy','stop')=='log_gripper_only')
        bridge.state("REPLAY_READY", "轨迹已检查；源为 observation.state。确认完整路线后开始低速对齐", summary=summary)
        while not bridge.stop.is_set():
            try:
                request = bridge.commands.get(timeout=.1)
                if request.get("command") == "start" and request.get("confirmed") is True:
                    break
            except queue.Empty:
                pass
        if bridge.stop.is_set():
            raise KeyboardInterrupt
        current = np.array(list_from_bus(bus))
        if np.any(abs(current-commands[0]) > prepared.MAX_INITIAL_DIFFERENCE):
            raise ValueError("起始差超过既有 30°/百分点回放约定，未开启力矩；请先调整姿态")
        motion.start_hold()
        bridge.state("ALIGNING", "先低速对齐该条示范起点，2°/秒，夹爪 5 百分点/秒")
        last = time.perf_counter()
        stable = 0
        align_deadline = last + float(np.max(abs(current-commands[0])/np.array([2.]*5+[5.]))) + 15
        while stable < 3:
            if bridge.stop.is_set():
                raise KeyboardInterrupt
            now = time.perf_counter()
            if now > align_deadline:
                raise TimeoutError("回放起点对齐未收敛，未进入示范执行")
            delta = prepared.ALIGN_BODY_SPEED_DEG_S * min(now-last, .1)
            step = np.array([delta]*5 + [prepared.ALIGN_GRIPPER_SPEED_PERCENT_S*min(now-last, .1)])
            last = now
            goal = motion.last_target + np.clip(commands[0]-motion.last_target, -step, step)
            motion.send(goal)
            current = np.array(list_from_bus(bus))
            stable = stable+1 if np.all(abs(current-commands[0]) <= prepared.ALIGN_TOLERANCE) else 0
            bridge.emit("telemetry", state=current.tolist(), target=goal.tolist(), health=motion.health_records[-1] if motion.health_records else None)
        bridge.state("REPLAYING", "正在回放实测从臂轨迹；结束不自动归位")
        started = time.perf_counter()
        with (out / "replay.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["frame", "elapsed", *["goal_"+n for n in NAMES], *["state_"+n for n in NAMES]])
            for index, target in enumerate(commands):
                motion.wait_until(started + index/fps)
                motion.send(target)
                current = list_from_bus(bus)
                writer.writerow([index, time.perf_counter()-started, *target, *current])
                bridge.emit("telemetry", state=current, target=target.tolist(), frames=index+1,
                            health=motion.health_records[-1] if motion.health_records else None)
        return {"outcome": "replayed", "source": "observation.state", "summary": summary, "port": port}
    finally:
        try:
            final = motion.hold_on_exit()
            atomic_json(out / "final_feedback.json", final)
        finally:
            if bus.is_connected:
                bus.disconnect(disable_torque=False)
            listener.stop()


def list_from_bus(bus):
    values = bus.sync_read("Present_Position", num_retry=1)
    return [float(values[n]) for n in NAMES]


def main():
    # Read one line without creating a buffered stdin reader shared with the control thread.
    initial = bytearray()
    while not initial.endswith(b"\n"):
        chunk = os.read(sys.stdin.fileno(), 1)
        if not chunk or len(initial) >= 32768:
            raise ValueError("后台启动配置不完整")
        initial.extend(chunk)
    first = json.loads(initial)
    cfg, options = first["config"], first["options"]
    environment(cfg)
    require_capability(cfg, 'preview' if first['kind']=='camera_scan' else first['kind'])
    out = robot_path(first["output"], existing=True)
    import faulthandler
    trace = None
    if first["kind"] == "crop":
        trace = (out / "worker_trace.log").open("w", encoding="utf-8")
        faulthandler.dump_traceback_later(15, file=trace)
    sys.path.insert(0, cfg["shared_scripts"])
    from so101_profile import configure
    configure(cfg)
    bridge = Bridge(cfg)
    threading.Thread(target=bridge.read_commands, daemon=True).start()
    report = {"outcome": "not_started"}
    try:
        kind = first["kind"]
        if kind in ("preview", "camera_scan"):
            report = preview_only(cfg, bridge, kind == "camera_scan")
        elif kind in ("monitor", "control"):
            report = monitor_or_control(cfg, options, bridge, out, kind)
        elif kind in ("record", "teleop"):
            report = record_or_teleop(cfg, options, bridge, out, kind)
        elif kind == "act":
            report = run_act(cfg, options, bridge, out)
        elif kind == "replay":
            report = replay(cfg, options, bridge, out)
        elif kind == "crop":
            from motion_trim import run_job
            try:
                report = run_job(cfg,options,bridge,out)
            except InterruptedError as error:
                report = {"outcome":"operator_stopped","message":str(error),"original_data_unchanged":True}
        else:
            raise ValueError("未知工作类型")
    except KeyboardInterrupt:
        report = {"outcome": "operator_stopped", "task_success": None}
    except BaseException as exc:
        report = {"outcome": "error", "error": str(exc), "task_success": None}
        traceback.print_exc()
        event("error", message=str(exc))
    finally:
        if trace is not None:
            faulthandler.cancel_dump_traceback_later()
            trace.close()
        atomic_json(out / "worker_report.json", report)
        event("done", **report)
    return 1 if report["outcome"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
