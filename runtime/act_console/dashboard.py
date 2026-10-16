"""Operator UI. One worker owns both cameras, policy and follower serial bus.

The main Tk thread only paints copied images and posts explicit control events.
It never advances the policy, inspects task success or opens a second camera.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import traceback
from uuid import uuid4

from session import MAX_TRIAL_DURATION, SessionControl, runner_arguments, save_operator_result

STAGES = {"idle": "未启动", "preflight": "预检 不运动", "model_loading": "加载模型 不运动",
          "cameras": "打开相机 不运动", "warmup": "GPU 预热 不运动", "ready": "就绪 等待你确认",
          "resetting": "标准起点复位 正在运动", "aligning": "低速接入 正在运动", "running": "ACT 推理 正在运动",
          "finishing": "正在停止和保存", "finished": "已停止 等待你判断结果"}
JOINTS = ("底座旋转", "肩部抬升", "肘部", "腕部俯仰", "腕部旋转", "夹爪")


class Dashboard:
    def __init__(self, cfg, simulate=False):
        self.cfg, self.simulate = cfg, simulate
        self.root = tk.Tk()
        self.root.title("SO101 ACT 自主启动" + ("  离线界面演示 无硬件" if simulate else ""))
        self.root.geometry("1120x920")
        self.root.minsize(980, 800)
        self.worker = self.control = None
        self.resources = None
        self.output = None
        self.stage = "idle"
        self.closing = False
        self.failure = None
        self.images = {}
        self.stage_text = tk.StringVar(value=STAGES["idle"])
        self.detail = tk.StringVar(value="先点击预检与预热。打开本窗口不会启动相机或机械臂。")
        self.metrics = tk.StringVar(value="尚无运行数据")
        self.health = tk.StringVar(value="力矩与温度：尚未读取")
        self.joints = tk.StringVar(value="关节位置：尚未读取")
        self.duration = tk.StringVar(value=f"{cfg['duration_s']:g}")
        self.confirmed = [tk.BooleanVar(value=False) for _ in range(3)]
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda event: self.stop())
        self.root.after(80, self.poll)

    def _build(self):
        panel = ttk.Frame(self.root, padding=14)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, textvariable=self.stage_text, font=("Microsoft YaHei UI", 21)).pack(anchor="w")
        ttk.Label(panel, textvariable=self.detail, wraplength=1040).pack(anchor="w", pady=(4, 10))
        cameras = ttk.Frame(panel)
        cameras.pack(fill="x")
        self.views = {}
        for name, title in (("top", "顶部相机  输入 observation.images.top"),
                            ("wrist", "腕部相机  输入 observation.images.wrist")):
            frame = ttk.LabelFrame(cameras, text=title, padding=4)
            frame.pack(side="left", fill="both", expand=True, padx=4)
            label = ttk.Label(frame, text="等待预检与预热", anchor="center")
            label.pack(fill="both", expand=True)
            self.views[name] = label
        ttk.Label(panel, textvariable=self.metrics, wraplength=1040).pack(anchor="w", pady=(10, 4))
        ttk.Label(panel, textvariable=self.health, wraplength=1040).pack(anchor="w")
        ttk.Label(panel, textvariable=self.joints, wraplength=1040).pack(anchor="w", pady=(4, 8))
        controls = ttk.Frame(panel)
        controls.pack(fill="x", pady=4)
        self.prepare_button = ttk.Button(controls, text="1 预检与预热 不运动", command=self.prepare)
        self.prepare_button.pack(side="left", padx=3)
        self.hold_button = ttk.Button(controls, text="2 当前位置保持 需要时使用", command=self.hold, state="disabled")
        self.hold_button.pack(side="left", padx=3)
        self.start_button = ttk.Button(controls, text="3 开始 ACT", command=self.start, state="disabled")
        self.start_button.pack(side="left", padx=3)
        self.stop_button = ttk.Button(controls, text="停止模型动作 Esc", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=3)
        ttk.Label(controls, text="限时 秒").pack(side="left", padx=(10, 2))
        self.duration_box = ttk.Spinbox(controls, from_=1, to=MAX_TRIAL_DURATION, increment=0.5,
                                      width=5, textvariable=self.duration)
        self.duration_box.pack(side="left")
        for var, title in zip(self.confirmed, ("两路画面身份正确，娃娃与目标区已摆好",
                                               "从臂已夹紧，夹爪为空，整个复位及抓放路径无障碍，可随时切断电源",
                                               "开始 ACT 前已移开手和支撑；或保持力矩时已可靠支撑")):
            ttk.Checkbutton(panel, text=title, variable=var, command=self.update_buttons).pack(anchor="w")
        ttk.Label(panel, text="预检不改变现有力矩。停止后不自动归位，不释放力矩。软件停止不是硬件急停。",
                  foreground="#a11c1c", wraplength=1040).pack(anchor="w", pady=(8, 4))
        result = ttk.Frame(panel)
        result.pack(fill="x", pady=5)
        self.disconnect_button=ttk.Button(result,text='释放模型与相机（不卸力）',command=self.release_resources,state='disabled')
        self.disconnect_button.pack(side='right',padx=3)
        ttk.Label(result, text="你先判断任务结果：").pack(side="left")
        self.result_buttons = []
        for title, value in (("成功", True), ("失败", False), ("未判断", None)):
            button = ttk.Button(result, text=title, state="disabled", command=lambda v=value: self.result(v))
            button.pack(side="left", padx=3)
            self.result_buttons.append(button)
        self.path_label = ttk.Label(panel, text=f"模型：{self.cfg['checkpoint']}\n日志：{self.cfg['log_root']}", wraplength=1040)
        self.path_label.pack(anchor="w", pady=(6, 4))
        self.log = tk.Text(panel, height=7, wrap="word", state="disabled", font=("Microsoft YaHei UI", 9))
        self.log.pack(fill="both", expand=True)

    def write(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", f"{datetime.now():%H:%M:%S} {message}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def active(self):
        return self.worker is not None and self.worker.is_alive()

    def update_buttons(self):
        ready = self.active() and self.stage == "ready" and not self.control.stop.is_set() and not self.closing
        confirmed = all(var.get() for var in self.confirmed)
        self.start_button.configure(state="normal" if ready and confirmed else "disabled")
        self.hold_button.configure(state="normal" if ready and confirmed else "disabled")
        self.prepare_button.configure(state="disabled" if self.active() or self.closing else "normal")
        self.prepare_button.configure(text='1 准备下一次 · 不重复预热' if self.resources is not None and self.resources.warmed else '1 连接与首次预热 · 不运动')
        self.disconnect_button.configure(state='normal' if self.resources is not None and not self.active() else 'disabled')
        self.stop_button.configure(state="normal" if self.active() else "disabled")
        self.duration_box.configure(state="disabled" if self.active() else "normal")

    def prepare(self):
        if self.active():
            return
        try:
            duration = float(self.duration.get())
            if not 0 < duration <= MAX_TRIAL_DURATION:
                raise ValueError(f"测试时限必须大于 0 且不超过示范上限 {MAX_TRIAL_DURATION:g} 秒")
        except ValueError as error:
            messagebox.showerror("限时设置", str(error))
            return
        for var in self.confirmed:
            var.set(False)
        for button in self.result_buttons:
            button.configure(state="disabled")
        self.output = Path(self.cfg["log_root"])/(datetime.now().strftime("%Y%m%d-%H%M%S")+"-"+uuid4().hex[:6])
        self.control = SessionControl(self.output if not self.simulate else None, self.cfg["preview_hz"])
        self.failure = None
        self.stage = "preflight"
        self.stage_text.set(STAGES[self.stage])
        self.detail.set('快速核对当前设备并复用模型与相机；不重复预热，尚未授权模型动作。' if self.resources is not None and self.resources.warmed else "正在启动预检；尚未授权模型动作。")
        self.path_label.configure(text=f"本次日志：{self.output}" + ("  演示模式不会保存真实运行日志" if self.simulate else ""))
        self.worker = threading.Thread(target=self.work, args=(duration,), name="SO101-session", daemon=False)
        self.worker.start()
        self.update_buttons()

    def work(self, duration):
        try:
            if self.simulate:
                self.simulate_session(duration)
            else:
                import so101_guarded_real_act as act
                if self.cfg.get('resident_runtime',False):
                    if self.resources is None:
                        from so101_act_runtime import ResidentResources
                        self.resources=ResidentResources()
                    act.main(runner_arguments(self.cfg,self.output,duration),controller=self.control,resources=self.resources)
                else:
                    act.main(runner_arguments(self.cfg, self.output, duration), controller=self.control)
        except BaseException as error:
            self.control.emit("error", message=f"{type(error).__name__}: {error}")
            self.control.emit("finished", report={"outcome": "startup_exception", "failure": repr(error),
                                                  "task_success": None}, output=str(self.output))
            traceback.print_exc()

    def hold(self):
        if not (self.active() and self.stage == "ready" and all(v.get() for v in self.confirmed)):
            return
        if not messagebox.askyesno("恢复当前位置保持", "机械臂已经可靠支撑？\n将当前实测位置写为目标后开启力矩，不执行模型轨迹。\n开启后移开手和支撑，再重新勾选确认并开始 ACT。"):
            return
        self.control.hold.set()
        for var in self.confirmed:
            var.set(False)
        self.update_buttons()

    def start(self):
        if self.active() and self.stage == "ready" and all(v.get() for v in self.confirmed):
            self.control.start.set()
            # Disable until the worker has consumed and validated the request.
            self.start_button.configure(state="disabled")
            self.hold_button.configure(state="disabled")
            for var in self.confirmed:
                var.set(False)
            self.write("已请求开始。先低速复位标准起点，再用新观测直接运行 ACT；软阈值只记录。"
                       if self.cfg.get("control_mode") == "standard" else
                       "已请求开始。先复位标准起点，再重新读取输入并低速接入模型；之后才开始 ACT 计时。")

    def stop(self):
        if self.active():
            self.control.stop.set()
            self.detail.set("停止请求已发出，等待控制线程停止并保存。若程序不响应，请切断硬件电源。")
            self.write("STOP：不自动归位或关闭力矩。")
            self.update_buttons()

    def result(self, value):
        if self.simulate:
            self.write("离线演示：不保存真实任务结果。")
            return
        try:
            save_operator_result(self.output, value)
            self.write("已保存你的判断："+{True: "成功", False: "失败", None: "未判断"}[value]+"；不会自动分析录像。")
        except Exception as error:
            messagebox.showerror("结果保存", str(error))

    def show_frames(self, frames):
        from PIL import Image, ImageTk
        for name, frame in frames.items():
            image = Image.fromarray(frame)
            image.thumbnail((480, 330))
            self.images[name] = ImageTk.PhotoImage(image)
            self.views[name].configure(image=self.images[name], text="")

    def show_metrics(self, data):
        if "elapsed_s" in data:
            age = data.get("camera_age_ms", {})
            self.metrics.set(f"ACT {data['elapsed_s']:.1f} / {data['duration_s']:g} 秒  |  已发 {data['steps']} 步  |  "
                             f"实测累计频率 {data['effective_hz']:.1f} Hz  |  处理 {data['processing_ms']:.1f} ms\n"
                             f"画面年龄 顶部 {age.get('top', 0):.0f} / 腕部 {age.get('wrist', 0):.0f} ms  |  "
                             f"各关节边界裁剪累计 {data['clipped_counts']}")
        elif "alignment_steps" in data:
            self.metrics.set(f"低速对齐 {data['alignment_step']} / {data['alignment_steps']} 步")
        elif "reset_elapsed_s" in data:
            self.metrics.set(f"标准起点复位 {data['reset_elapsed_s']:.1f} 秒；ACT 尚未开始计时")
        elif "camera_age_ms" in data:
            age = data["camera_age_ms"]
            self.metrics.set(f"相机画面年龄 顶部 {age['top']:.0f} / 腕部 {age['wrist']:.0f} ms  |  "
                             "GPU 已预热，尚未执行模型动作")
        if "state" in data:
            self.joints.set("实测位置："+"  |  ".join(f"{name} {value:.1f}{'%' if i == 5 else '°'}"
                                                    for i, (name, value) in enumerate(zip(JOINTS, data["state"]))))
        health = data.get("health")
        if health:
            temperatures = health.get("Present_Temperature", health.get("temperature", {}))
            torque = health.get("Torque_Enable", health.get("torque", {}))
            self.health.set(f"最高舵机温度 {max(temperatures.values(), default=0)} °C  |  力矩状态 {list(torque.values())}"
                            "  （1 开启，0 关闭；关闭时需支撑）")

    def poll(self):
        if self.control is not None:
            events, frames, telemetry = self.control.drain()
            if frames is not None:
                self.show_frames(frames)
            if telemetry is not None:
                self.show_metrics(telemetry)
            for event in events:
                if event["kind"] == "stage":
                    self.stage = event["stage"]
                    self.stage_text.set(STAGES.get(self.stage, self.stage))
                    self.detail.set(event["message"])
                    self.write(event["message"])
                elif event["kind"] == "error":
                    self.failure = event["message"]
                    self.write("异常停止："+self.failure)
                elif event["kind"] == "warning":
                    self.write("警告："+event["message"])
                elif event["kind"] == "finished":
                    self.stage = "finished"
                    self.stage_text.set(STAGES["finished"])
                    report = event["report"]
                    warning = report.get("holding_update_failure")
                    message = f"程序状态：{report['outcome']}。请由你判断抓取结果；不会自动归位或查看录像。"
                    if self.failure or report.get("failure"):
                        message += " 停止原因："+(self.failure or report["failure"])
                    if warning:
                        message += " 力矩保持更新失败，请检查硬件："+warning
                    if report.get("cleanup_errors"):
                        message += " 设备释放出现异常，请关闭程序并检查占用后再启动。"
                    if report.get('resident_resources_retained'):
                        message += ' 模型与双相机保持加载，下一次不重复预热；舵机栏为本次最终反馈，不是持续监控。'
                    else:
                        message += ' 本次设备连接已结束，显示为最后反馈。'
                    self.detail.set(message)
                    self.write(message)
                    if not self.simulate and (self.output/"report.json").exists():
                        for button in self.result_buttons:
                            button.configure(state="normal")
            self.update_buttons()
        if not self.active() and self.resources is not None:
            frames=self.resources.preview()
            if frames is not None:self.show_frames(frames)
        if self.closing and not self.active():
            self.release_resources()
            self.root.destroy()
            return
        self.root.after(80, self.poll)

    def close(self):
        if self.active():
            self.closing = True
            self.stop()
            self.write("等待工作线程退出后关闭窗口。不要直接强杀进程。")
        else:
            self.release_resources()
            self.root.destroy()

    def release_resources(self):
        if self.active():return
        if self.resources is not None:
            errors=self.resources.close()
            if errors:
                self.write('相机释放未全部确认：'+'; '.join(errors))
            else:
                self.resources=None
                self.write('已释放常驻模型与相机；没有连接或写入舵机，不改变力矩。')
                for label in self.views.values():label.configure(image='',text='相机已断开')
                self.images.clear()
        self.update_buttons()

    def simulate_session(self, duration):
        """Synthetic UI check only. Never imports the robot adapter."""
        import numpy as np
        picture = np.zeros((480, 640, 3), dtype=np.uint8)
        picture[:] = (40, 75, 100)
        picture[120:360, 200:440] = (210, 200, 130)
        frames = {"top": picture, "wrist": picture[:, ::-1].copy()}
        self.control.emit("stage", stage="ready", message="离线界面演示：图片为合成图，无硬件连接。")
        while not self.control.stop.wait(0.1):
            self.control.emit("preview", frames=frames)
            if self.control.hold.is_set():
                self.control.hold.clear()
                self.control.emit("stage", stage="ready", message="离线演示：保持请求，不涉及真实力矩。")
            if self.control.start.is_set():
                break
        if not self.control.stop.is_set():
            self.control.emit("stage", stage="running", message="离线演示计时，不运行 ACT 或机械臂。")
            started = time.perf_counter()
            while not self.control.stop.wait(0.1) and time.perf_counter()-started < duration:
                self.control.emit("preview", frames=frames)
        self.control.emit("finished", report={"outcome": "offline_ui_demo", "task_success": None}, output="")

    def run(self):
        self.root.mainloop()
