"""Official LeRobot recording with preserved servo settings and slow initial catch-up.

Default is a dry plan. --execute is required for any hardware access.
Data, diagnostic sidecars, caches and temporary files stay in the configured workspace.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import queue
import subprocess
import threading
import time
import traceback
from pathlib import Path
import sys
import so101_profile as profile
profile.initialize_environment()
os.environ["PATH"] = os.pathsep.join(str(p) for p in (
    Path(sys.executable).parent / "Scripts", Path(sys.executable).parent / "Library/bin")) + os.pathsep + os.environ.get("PATH", "")

import numpy as np
from serial.tools.list_ports import comports
from lerobot.cameras.opencv import OpenCVCameraConfig
from lerobot.configs.dataset import DatasetRecordConfig
from lerobot.configs.video import RGBEncoderConfig
from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig
from lerobot.teleoperators.so_leader import SO101Leader, SO101LeaderConfig
from lerobot.scripts import lerobot_record as official
import so101_guarded_real_act as safety

NAMES = safety.NAMES
import so101_profile as profile
CAL = profile.workspace() / "hf-cache/lerobot/calibration"
SERIALS = {role: profile.serial(role) for role in ("leader", "follower")}


class RecorderInstanceLock:
    """Atomic Windows single-instance guard, including a retained finished GUI."""
    def __init__(self, name="Local\\SO101_SupplementRecorder"):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.name, self.handle = name, None

    def acquire(self):
        self.ctypes.set_last_error(0)
        handle = self.kernel.CreateMutexW(None, False, self.name)
        error = self.ctypes.get_last_error()
        if not handle:
            raise self.ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS: do not open a second GUI/device.
            self.kernel.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def resolve_ports():
    ports = list(comports())
    return {role: profile.resolve_port(role, ports) for role in ("leader", "follower")}


def clip_target(action, limits):
    requested = np.array([action[f"{n}.pos"] for n in NAMES], dtype=float)
    bounded, clipped = safety.bounded_target(requested, limits)
    return requested, bounded, clipped


def catchup_step(previous, target, dt):
    # Speed in degrees/s for the five arm joints, percentage-points/s for gripper.
    maximum = np.array([5.] * 5 + [15.]) * min(max(dt, 0.), .1)
    return previous + np.clip(target - previous, -maximum, maximum)


def install_idempotent_episode_save(dataset):
    """Commit before reset; the official post-reset save then becomes a no-op."""
    if getattr(dataset, "_supplement_save_installed", False):
        return
    original = dataset.save_episode

    def save(*args, **kwargs):
        if args or kwargs.get("episode_data") is not None or dataset.has_pending_frames():
            return original(*args, **kwargs)
        return None

    dataset.save_episode = save
    dataset._supplement_save_installed = True


def await_first_record_start(session, keep_following, clock=time):
    """One explicit start gate per process, with low-speed following, no dataset."""
    if session.start_gate_completed:
        return
    session.state("WAITING_START")
    while not session.recording_start.is_set():
        if session.stop.is_set():
            raise KeyboardInterrupt
        keep_following()
    session.state("COUNTDOWN", duration=3)
    deadline = clock.perf_counter()+3
    while clock.perf_counter() < deadline:
        if session.stop.is_set():
            raise KeyboardInterrupt
        keep_following()
    session.start_gate_completed = True


def announce_phase(text):
    """Audio is secondary to the panel and must not block motor/data timing."""
    def play():
        import winsound
        try:
            winsound.Beep(1200 if text == "START" else 600, 180)
            command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "Add-Type -AssemblyName System.Speech; "
                f"(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{text}')"]
            subprocess.run(command, timeout=5, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            logging.warning("Audio cue failed; use START/END panel: %s", exc)
    threading.Thread(target=play, daemon=True).start()


class SpaceAdvanceControl:
    """One phase-advance request per physical key press, never a widget click."""
    def __init__(self, session):
        self.session, self.down = session, False

    def press(self):
        if not self.down:
            self.down = True
            self.session.request_advance(source="space")
        return "break"

    def release(self):
        self.down = False
        return "break"


def install_space_control(root, session):
    space = SpaceAdvanceControl(session)
    tag = "SO101PhaseAdvance"
    root.bind_class(tag, "<KeyPress-space>", lambda event: space.press())
    root.bind_class(tag, "<KeyRelease-space>", lambda event: space.release())
    root.bind_class(tag, "<FocusOut>", lambda event: space.release())

    def tag_widgets(widget):
        # Before Tk's default Space-to-click, even on the start/stop buttons.
        widget.bindtags((tag, *widget.bindtags()))
        for child in widget.winfo_children():
            tag_widgets(child)

    tag_widgets(root)
    return space


class Session:
    def __init__(self, args, updates):
        self.args, self.updates = args, updates
        self.stop = threading.Event()
        self.scene_approved = threading.Event()
        self.recording_start = threading.Event()
        self.start_gate_completed = False
        self.sync_count = 0
        self.control_lock = threading.RLock()
        self.advance_requests = []
        self.phase_frame_count = 0
        self.events = None
        self.phase = "PREPARING"
        self.deadline = None
        self.motion = None
        self.robot = self.leader = None
        self.frames = 0
        self.episode = 0
        self.diag = None
        self.last_obs = None
        self.last_obs_time = None
        self.last_camera_times = None
        self.out = profile.resource("log_home", "logs/recording") / f"{args.group}_{time.strftime('%Y%m%d_%H%M%S')}"

    def state(self, phase, duration=None, **extra):
        with self.control_lock:
            if phase == "SYNCING":
                self.sync_count += 1
            # A click racing with a natural timer end belongs to the old phase,
            # never to the next reset/recording loop. Preserve Esc/stop requests.
            if self.events is not None and not self.events["stop_recording"]:
                self.events["exit_early"] = False
            self.phase = phase
            if phase == "RECORDING":
                self.phase_frame_count = 0
            self.deadline = time.perf_counter()+duration if duration is not None else None
        value = {"phase": phase, "episode": self.episode, "group": self.args.group, **extra}
        self.updates.put(value)
        print("RECORD_STATE " + json.dumps(value, ensure_ascii=False), flush=True)

    def request_stop(self):
        with self.control_lock:
            if self.events is not None:
                self.events.update(stop_recording=True, exit_early=True)
            # Let the loop exit at its next frame boundary and commit its buffer.
            if self.phase not in ("RECORDING", "RESET"):
                self.stop.set()

    def request_advance(self, expected_phase=None, source="button"):
        with self.control_lock:
            if self.phase not in ("RECORDING", "RESET"):
                return False
            if expected_phase is not None and expected_phase != self.phase:
                return False
            if (self.events is None or self.events["stop_recording"]
                    or self.events["exit_early"] or self.stop.is_set()):
                return False
            if self.phase == "RECORDING" and self.phase_frame_count == 0:
                return False  # Never create a zero-frame episode.
            self.events["exit_early"] = True
            event = {"wall_time": time.time(), "perf_time": time.perf_counter(),
                     "phase": self.phase, "episode_index": self.episode, "source": source,
                     "behavior": "save_episode" if self.phase == "RECORDING" else "finish_reset"}
            self.advance_requests.append(event)
            print("RECORD_ADVANCE " + json.dumps(event), flush=True)
            return True

    def row(self, requested, sent, clipped):
        if self.diag:
            self.diag.writerow([time.time(), time.perf_counter(), self.phase, self.episode,
                                self.last_obs_time, json.dumps(requested.tolist()),
                                json.dumps(sent.tolist()), json.dumps(clipped.tolist()),
                                json.dumps(self.last_obs), json.dumps(self.last_camera_times)])
        if self.phase == "RECORDING":
            self.frames += 1
            self.phase_frame_count += 1
        if clipped.any() and not getattr(self, "clip_reported", False):
            self.clip_reported = True
            print("RECORD_BOUNDARY_LOG: requested leader targets preserved; sent targets use unchanged follower bounds. No calibration/limit expansion.", flush=True)


def live_calibration(bus, calibration):
    for register, field in [("Homing_Offset", "homing_offset"),
                            ("Min_Position_Limit", "range_min"),
                            ("Max_Position_Limit", "range_max")]:
        live = bus.sync_read(register, normalize=False, num_retry=2)
        if any(live[n] != getattr(c, field) for n, c in calibration.items()):
            raise RuntimeError(f"Live calibration mismatch: {register}; no automatic recalibration")


def connect_camera_at_start(camera, attempts=3):
    """Bounded capture-only retries before any motion; never relax camera settings."""
    for attempt in range(attempts):
        try:
            camera.connect()
            frame = camera.read_latest()
            if frame.shape != (480, 640, 3):
                raise RuntimeError(f"Unexpected camera shape: {frame.shape}")
            return
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            if camera.is_connected:
                camera.disconnect()
            if attempt+1 == attempts:
                raise
            logging.warning("Startup camera reconnect %s/%s, unchanged configuration: %s",
                            attempt+2, attempts, exc)
            time.sleep(.5)


class PreservedLeader(SO101Leader):
    def connect(self, calibrate=False):
        safety.guard_bus_writes(self.bus)
        self.bus.connect(handshake=False)
        live_calibration(self.bus, self.calibration)
        for reg in ("Operating_Mode", "Status", "Torque_Enable"):
            if any(self.bus.sync_read(reg, normalize=False, num_retry=2).values()):
                raise RuntimeError(f"Leader {reg} must be zero; existing parameters left unchanged")

    def disconnect(self):
        if self.bus.is_connected:
            self.bus.disconnect(disable_torque=False)


class PreservedFollower(SO101Follower):
    def __init__(self, config, session):
        super().__init__(config)
        self.session = session
        self.cal_dict = {n: vars(c) for n, c in self.calibration.items()}
        self.limits = safety.limits_from_calibration(self.cal_dict)

    def connect(self, calibrate=False):
        safety.guard_bus_writes(self.bus)
        self.bus.connect(handshake=False)
        s = self.session
        safety.preflight(self.bus, self.cal_dict, 1., s.stop, allow_gripper_overload=profile.allow_gripper_overload())
        for camera in self.cameras.values():
            connect_camera_at_start(camera)
        # Fresh live previews must be reviewed before the first motion write.
        # No Goal_Position/Torque_Enable write has happened in this process yet.
        s.state("SCENE_CHECK")
        while not s.scene_approved.wait(.05):
            if s.stop.is_set():
                raise KeyboardInterrupt
        if s.stop.is_set():
            raise KeyboardInterrupt
        s.motion = safety.Motion(self.bus, self.cal_dict, s.stop,
                                 soft_interlocks=False, temperature_confirmation_s=1.,
                                 allow_gripper_overload=profile.allow_gripper_overload())
        s.motion.start_hold()
        s.state("SYNCING")
        last = time.perf_counter()
        stable = 0

        def approach():
            nonlocal last
            now = time.perf_counter()
            requested, target, clipped = clip_target(s.leader.get_action(), self.limits)
            measured = self.bus.sync_read("Present_Position", num_retry=2)
            measured = np.array([measured[n] for n in NAMES])
            command = catchup_step(s.motion.last_target, target, now-last)
            last = now
            s.motion.send(command)
            return abs(measured-target)

        while not s.stop.is_set():
            error = approach()
            stable = stable+1 if np.all(error <= np.array([3.]*5+[5.])) else 0
            if stable >= 3:
                s.state("SYNCED", maximum_arm_residual_deg=float(error[:5].max()))
                await_first_record_start(s, approach)
                return
        raise KeyboardInterrupt

    def get_observation(self):
        obs = super().get_observation()
        self.session.last_obs = {n: float(obs[f"{n}.pos"]) for n in NAMES}
        self.session.last_obs_time = time.perf_counter()
        self.session.last_camera_times = {n: cam.latest_timestamp for n, cam in self.cameras.items()}
        return obs

    def send_action(self, action):
        requested, target, clipped = clip_target(action, self.limits)
        self.session.motion.send(target)
        self.session.row(requested, target, clipped)
        return {f"{n}.pos": float(v) for n, v in zip(NAMES, target)}

    def disconnect(self):
        s = self.session
        if self.bus.is_connected:
            try:
                if s.motion and s.motion.enabled:
                    s.exit_health = s.motion.hold_on_exit()
            finally:
                self.bus.disconnect(disable_torque=False)
        for cam in self.cameras.values():
            if cam.is_connected:
                cam.disconnect()


def run_session(s):
    args = s.args
    originals = {key: getattr(official, key) for key in
                 ("make_robot_from_config", "make_teleoperator_from_config", "record_loop", "init_keyboard_listener", "log_say")}
    report = {"group": args.group, "requested_episodes": args.episodes,
              "process_id": os.getpid(),
              "dataset_root": str(args.dataset_root), "task": "把娃娃放进盒子",
              "position_condition": "vary_doll_box_fixed" if args.group in ("G1", "G2", "G3") else "vary_box_doll_fixed",
              "exact_positions_measured": False, "status": "preparing", "servo_parameter_changes": False,
              "sync_once_at_session_start": True, "manual_first_record_start": True,
              "initial_countdown_s": 3,
              "exit_holds_follower_torque": True, "expert_success_authority": "onsite_operator"}
    diag_file = None
    key_listener = None
    try:
        ports = resolve_ports()
        report["ports"] = ports
        info = args.dataset_root/"meta/info.json"
        before = json.loads(info.read_text())["total_episodes"] if info.exists() else 0
        if before+args.episodes > 30:
            raise ValueError("This authorized supplement is limited to 30 saved episodes")
        report["episodes_before"] = before
        s.episode = before
        s.out.mkdir(parents=True, exist_ok=False)
        (s.out/"session.json").write_text(json.dumps({"process_id": os.getpid(),
            "group": args.group, "dataset_root": str(args.dataset_root)}, indent=2), encoding="utf-8")
        diag_file = (s.out/"teleoperation.csv").open("w", newline="", encoding="utf-8")
        s.diag = csv.writer(diag_file)
        s.diag.writerow(["wall_time", "perf_time", "phase", "episode_index", "observation_read_perf",
                         "leader_requested_deg_and_percent", "follower_sent_deg_and_percent",
                         "boundary_clipped", "follower_measured_deg_and_percent", "camera_capture_perf_times"])
        cams = {n: OpenCVCameraConfig(index_or_path=i, fps=30, width=640, height=480,
                                      backend=700, fourcc="MJPG", color_mode="rgb")
                for n, i in (("top", 0), ("wrist", 1))}
        follower_cal = profile.resource("calibration", "config/follower.json")
        leader_cal = profile.resource("leader_calibration", "config/leader.json")
        rcfg = SO101FollowerConfig(port=ports["follower"], id=follower_cal.stem,
                                  calibration_dir=follower_cal.parent, cameras=cams,
                                  use_degrees=True, disable_torque_on_disconnect=False)
        tcfg = SO101LeaderConfig(port=ports["leader"], id=leader_cal.stem,
                                calibration_dir=leader_cal.parent, use_degrees=True)
        s.robot, s.leader = PreservedFollower(rcfg, s), PreservedLeader(tcfg)
        official.make_robot_from_config = lambda cfg: s.robot
        official.make_teleoperator_from_config = lambda cfg: s.leader

        # Install the stop control BEFORE any catch-up movement, not afterwards.
        key_listener, s.events = originals["init_keyboard_listener"](allow_episode_navigation=False)
        if key_listener is None:
            raise RuntimeError("No working stop-key listener; no movement started")

        def relay_stop():
            while not s.stop.wait(.02):
                if s.events["stop_recording"]:
                    s.request_stop()
                    return

        relay = threading.Thread(target=relay_stop, daemon=True)
        relay.start()

        def listener(**kwargs):
            return key_listener, s.events

        def loop(**kwargs):
            active_dataset = kwargs.get("dataset")
            is_recording = active_dataset is not None
            if is_recording:
                s.episode = active_dataset.num_episodes
                install_idempotent_episode_save(active_dataset)
            s.state("RECORDING" if is_recording else "RESET", duration=kwargs["control_time_s"])
            if is_recording:
                announce_phase("START")
            result = originals["record_loop"](**kwargs)
            s.state("SAVING" if is_recording else "RESET_FINISHED")
            if is_recording:
                announce_phase("END")
                active_dataset.save_episode()
                print(f"RECORD_EPISODE_COMMITTED total={active_dataset.num_episodes}", flush=True)
            return result

        official.init_keyboard_listener, official.record_loop = listener, loop
        # Emit START/END from the actual phase transition, not before the gate.
        # Avoid spawning a blocking/profile-loading PowerShell in the control loop.
        official.log_say = lambda text, *a, **kw: logging.info(text)
        cfg = official.RecordConfig(
            robot=rcfg, teleop=tcfg,
            dataset=DatasetRecordConfig(
                repo_id="local/so101_supplement_20261015", root=args.dataset_root,
                single_task="把娃娃放进盒子", fps=30, episode_time_s=30, reset_time_s=20,
                num_episodes=args.episodes, push_to_hub=False, no_stamp=True,
                rgb_encoder=RGBEncoderConfig(vcodec="h264", g=30, crf=20, preset="fast"),
                streaming_encoding=True, encoder_threads=2,
            ), display_data=True, play_sounds=True, allow_episode_navigation=False, resume=info.exists(),
        )
        report["status"] = "running"
        dataset = official.record(cfg)
        report["episodes_after"] = dataset.num_episodes
        report["status"] = "stopped_by_operator" if s.stop.is_set() or (s.events and s.events["stop_recording"]) else "group_complete"
    except BaseException as exc:
        report["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "error"
        report["error"] = str(exc)
        if not isinstance(exc, KeyboardInterrupt):
            traceback.print_exc()
    finally:
        for obj in (s.robot, s.leader):
            if obj is not None:
                try:
                    obj.disconnect()
                except Exception as exc:
                    report.setdefault("cleanup_errors", []).append(str(exc))
        for key, value in originals.items():
            setattr(official, key, value)
        if key_listener is not None:
            key_listener.stop()
        if diag_file:
            diag_file.close()
        if s.motion:
            report["health"] = s.motion.health_records
            report["diagnostics"] = s.motion.warning_records
        report["exit_health"] = getattr(s, "exit_health", None)
        report["record_loop_frames"] = s.frames
        report["startup_sync_count"] = s.sync_count
        report["manual_start_completed"] = s.start_gate_completed
        report["manual_phase_advances"] = s.advance_requests
        if s.out.exists():
            (s.out/"report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        s.state("FINISHED", result=report["status"], error=report.get("error"))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset-root", type=Path, default=profile.workspace()/"datasets/new_recording")
    p.add_argument("--group", choices=[f"G{i}" for i in range(1, 7)], default="G1")
    p.add_argument("--episodes", type=int, default=5)
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()
    if not args.dataset_root.resolve().is_relative_to(profile.workspace()) or args.dataset_root.resolve() == profile.workspace():
        p.error("A specific dataset directory inside the configured workspace is required")
    if not 1 <= args.episodes <= 5:
        p.error("Record 1..5 episodes per position group")
    if not args.execute:
        print(json.dumps(vars(args), default=str, indent=2))
        print("DRY PLAN ONLY: no device access; 30s recording, 20s reset; slow catch-up then unrestricted relative following.")
        return
    instance = RecorderInstanceLock()
    if not instance.acquire():
        print("RECORDER_ALREADY_OPEN: close the previous recorder window first; no new window or device access.", flush=True)
        return
    try:
        run_gui(args)
    finally:
        instance.close()


def run_gui(args):
    import tkinter as tk
    from PIL import Image, ImageTk
    updates = queue.Queue()
    s = Session(args, updates)
    root = tk.Tk()
    root.title("SO101 补录 · START / END")
    root.geometry("760x690")
    root.attributes("-topmost", True)
    title = tk.Label(root, text="准备设备", font=("Microsoft YaHei", 25))
    title.pack(pady=12)
    detail = tk.Label(root, text="先同步，再手动点击开始；之后连续录制", font=("Microsoft YaHei", 15))
    detail.pack(pady=6)
    countdown = tk.Label(root, font=("Microsoft YaHei", 20))
    countdown.pack(pady=6)
    previews = tk.Frame(root)
    previews.pack(pady=5)
    preview_labels = {}
    for name in ("top", "wrist"):
        preview_labels[name] = tk.Label(previews, text=name, width=40, height=12)
        preview_labels[name].pack(side="left")
    approve = tk.Button(root, text="两路画面检查通过 → 开始低速同步", state="disabled",
                        command=s.scene_approved.set, font=("Microsoft YaHei", 13))
    approve.pack(pady=5)
    record_start = tk.Button(root, text="开始本组采集（3 秒倒计时）", state="disabled",
                             command=s.recording_start.set, font=("Microsoft YaHei", 13))
    record_start.pack(pady=5)
    phase_controls = tk.Frame(root)
    phase_controls.pack(pady=5)
    end_record = tk.Button(phase_controls, text="结束本条并保存（Space）", state="disabled",
        command=lambda: s.request_advance("RECORDING"), font=("Microsoft YaHei", 12))
    end_record.pack(side="left", padx=5)
    end_reset = tk.Button(phase_controls, text="结束复位，开始下一条（Space）", state="disabled",
        command=lambda: s.request_advance("RESET"), font=("Microsoft YaHei", 12))
    end_reset.pack(side="left", padx=5)
    tk.Button(root, text="停止整批（保持从臂力矩）", command=s.request_stop, font=("Microsoft YaHei", 13)).pack()
    tk.Label(root, text="Space 仅在此采集窗口获得焦点时生效；Esc 可停止整批",
             font=("Microsoft YaHei", 10)).pack(pady=5)
    install_space_control(root, s)
    completed = False

    def close():
        if completed:
            root.destroy()
        else:
            s.request_stop()

    root.protocol("WM_DELETE_WINDOW", close)
    labels = {"PREPARING": "准备设备", "SCENE_CHECK": "检查异物 / 手 / 线缆 · 尚未运动",
              "SYNCING": "低速同步 · 请扶稳主臂", "SYNCED": "同步完成",
              "WAITING_START": "未采集 · 低速跟随中 · 请点击开始",
              "COUNTDOWN": "准备开始 · 倒计时结束后抓放",
              "RECORDING": "START · 正在录制抓放任务", "RESET": "END · 复位 / 摆物（不录入）",
              "SAVING": "END · 正在保存本条", "RESET_FINISHED": "复位结束", "FINISHED": "本组已停止"}

    def refresh():
        nonlocal completed
        while not updates.empty():
            value = updates.get_nowait()
            title.config(text=labels[value["phase"]])
            title.config(fg="#15803d" if value["phase"] == "RECORDING" else
                            "#b45309" if value["phase"] in ("RESET", "SAVING") else "#1d4ed8")
            detail.config(text=f'{args.group} · episode {value["episode"]+1} / 30' + (f' · {value.get("result", "")}' if value["phase"] == "FINISHED" else ""))
            if value.get("error"):
                detail.config(text=value["error"][:95])
            completed = value["phase"] == "FINISHED"
            if completed and value.get("result") == "error":
                # The worker emits FINISHED only after report + device cleanup.
                root.after(2000, root.destroy)
            approve.config(state="normal" if value["phase"] == "SCENE_CHECK" else "disabled")
            record_start.config(state="normal" if value["phase"] == "WAITING_START" else "disabled")
        countdown.config(text=f'剩余 {max(0, s.deadline-time.perf_counter()):.1f} 秒' if s.deadline else "")
        end_record.config(state="normal" if s.phase == "RECORDING" and s.phase_frame_count > 0 else "disabled")
        end_reset.config(state="normal" if s.phase == "RESET" else "disabled")
        if s.robot is not None:
            for name, camera in s.robot.cameras.items():
                try:
                    frame = camera.read_latest()
                    photo = ImageTk.PhotoImage(Image.fromarray(frame).resize((320, 240)))
                    preview_labels[name].config(image=photo, width=320, height=240)
                    preview_labels[name].image = photo
                except Exception:
                    pass
        root.after(100, refresh)

    root.after(100, refresh)
    threading.Thread(target=run_session, args=(s,), daemon=False).start()
    root.mainloop()


if __name__ == "__main__":
    main()
