"""Non-blocking UI bridge. This module never opens cameras or serial ports."""
from __future__ import annotations

from collections import deque
from datetime import datetime
import json
from pathlib import Path
import threading
import time

MAX_TRIAL_DURATION = 17.5  # Must match guarded runner; checked by offline tests.


def load_settings(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("python", "shared_scripts", "lerobot_source", "checkpoint", "calibration", "log_root"):
        if Path(cfg[key]).resolve().drive.upper() != "E:":
            raise ValueError(f"{key} must stay on E drive")
    for key in ("python", "shared_scripts", "lerobot_source", "checkpoint", "calibration"):
        if not Path(cfg[key]).exists():
            raise FileNotFoundError(f"{key}: {cfg[key]}")
    if not 0 < cfg["duration_s"] <= MAX_TRIAL_DURATION:
        raise ValueError(f"The demonstration-derived maximum is {MAX_TRIAL_DURATION:g} seconds")
    if cfg["follower_usb_serial"] != "SET_FOLLOWER_USB_SERIAL":
        raise ValueError("Changing physical robot identity requires a separate review")
    if cfg["top_camera"] < 0 or cfg["wrist_camera"] < 0 or cfg["top_camera"] == cfg["wrist_camera"]:
        raise ValueError("Use two distinct camera indices")
    if (cfg["chunk_size"], cfg["n_action_steps"], cfg["temporal_ensemble_coeff"], cfg["fps"]) != (100, 1, 0.01, 30):
        raise ValueError("Inference contract changed; do not silently override the approved control loop")
    if not 1 <= cfg["preview_hz"] <= 15:
        raise ValueError("Preview rate must be 1..15 Hz; it is separate from 30 Hz control")
    if cfg.get("control_mode", "guarded") not in ("guarded", "standard"):
        raise ValueError("Unknown control mode")
    if cfg.get("gripper_overload_policy", "stop") not in ("stop", "log_gripper_only"):
        raise ValueError("Unknown gripper overload policy")
    if cfg.get("control_mode") == "standard":
        if not cfg.get("reset_before_act"):
            raise ValueError("The approved direct ACT startup requires a reference reset")
        if (cfg.get("temperature_limit_c"), cfg.get("temperature_confirmation_s")) != (60, 1.0):
            raise ValueError("Temperature confirmation contract changed")
    if cfg.get("reset_before_act", False):
        reference = Path(cfg["reference_start_pose"])
        if reference.resolve().drive.upper() != "E:" or not reference.exists():
            raise ValueError("Reference start must be an existing E-drive file")
    if not isinstance(cfg.get('resident_runtime',False),bool):raise ValueError('resident_runtime must be boolean')
    reset_speeds(cfg)
    return cfg


def reset_speeds(cfg):
    import math
    speeds=cfg.get('reset_speeds',[4.,3.2,2.4,1.6,.8,10.])
    if not isinstance(speeds,list) or len(speeds)!=6 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<v<=maximum for v,maximum in zip(speeds,[20.]*5+[100.])):
        raise ValueError('Reset rates require five arm rates in (0,20] deg/s and a gripper rate in (0,100] percent/s')
    return speeds


def runner_arguments(cfg, output, duration):
    if not 0 < duration <= MAX_TRIAL_DURATION:
        raise ValueError(f"Duration must be in (0, {MAX_TRIAL_DURATION:g}] seconds")
    args = ["--execute", "--standalone-cameras", "--output", str(output),
            "--duration", str(duration), "--port", cfg["follower_port"],
            "--top-camera", str(cfg["top_camera"]), "--wrist-camera", str(cfg["wrist_camera"]),
            "--checkpoint", cfg["checkpoint"], "--calibration", cfg["calibration"],
            "--control-mode", cfg.get("control_mode", "guarded"),
            "--gripper-overload-policy", cfg.get("gripper_overload_policy", "stop")]
    if cfg.get("reset_before_act", False):
        args += ["--reset-before-act", "--reference-start-pose", cfg["reference_start_pose"],
                 "--clear-reset-route-confirmed", "--empty-gripper-confirmed"]
        args += ['--reset-speeds',*map(str,reset_speeds(cfg))]
    return args


class SessionControl:
    def __init__(self, output=None, preview_hz=10):
        self.stop, self.start, self.hold = threading.Event(), threading.Event(), threading.Event()
        self.output = Path(output) if output is not None else None
        self.lock = threading.Lock()
        self.events = deque(maxlen=64)
        self.telemetry = None
        self.frames = None
        self.preview_period = 1 / preview_hz
        self.last_preview = -float("inf")
        self.logging_error = None

    def emit(self, kind, **values):
        now = time.perf_counter()
        with self.lock:
            if kind == "preview":
                if now-self.last_preview >= self.preview_period:
                    # The worker already owns independent RGB frame copies.
                    self.frames = values["frames"]
                    self.last_preview = now
            elif kind == "telemetry":
                self.telemetry = values
            else:
                event = {"kind": kind, **values}
                self.events.append(event)
                if self.output is not None and self.output.exists():
                    try:
                        with (self.output / "events.jsonl").open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps({"time": datetime.now().isoformat(), **event}, ensure_ascii=False)+"\n")
                    except Exception as error:
                        # A side-channel logging failure must not skip the
                        # runner's finally/hold routine or block the stop flag.
                        if self.logging_error is None:
                            self.logging_error = repr(error)
                            self.events.append({"kind": "warning", "message": "阶段日志写入失败："+self.logging_error})

    def drain(self):
        with self.lock:
            events = list(self.events)
            frames, telemetry = self.frames, self.telemetry
            self.events.clear()
            self.frames = self.telemetry = None
        return events, frames, telemetry


def save_operator_result(output, success):
    if success not in (True, False, None):
        raise ValueError("Result must be success, failure or not assessed")
    folder = Path(output)
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    result = {"task_success": success, "source": "operator_button",
              "reported_at": datetime.now().isoformat(), "software_outcome": report["outcome"],
              "automatic_video_review": False,
              "note": "An operator task judgment is independent of software completion."}
    (folder / "operator_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
