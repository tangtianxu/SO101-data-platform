"""Small, dependency-free contracts. Importing the platform never opens hardware."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile

HERE = Path(__file__).resolve().parent
ROBOT_HOME = Path("E:/robot").resolve()
NAMES = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")
IDENTITIES = {"leader_usb_serial": "SET_LEADER_USB_SERIAL", "follower_usb_serial": "SET_FOLLOWER_USB_SERIAL"}


def environment():
    values = {"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
              "PYTHONUTF8": "1", "HF_HOME": "E:/robot/hf-cache",
              "TORCH_HOME": "E:/robot/torch-cache", "HF_HUB_OFFLINE": "1",
              "TEMP": "E:/robot/temp", "TMP": "E:/robot/temp",
              "TRITON_CACHE_DIR": "E:/robot/cache/triton",
              "TORCHINDUCTOR_CACHE_DIR": "E:/robot/cache/torchinductor",
              "CUDA_CACHE_PATH": "E:/robot/cuda-cache"}
    os.environ.update(values)
    return os.environ.copy()


def robot_path(value, *, existing=False):
    p = Path(str(value)).resolve()
    if p == ROBOT_HOME or not p.is_relative_to(ROBOT_HOME):
        raise ValueError("路径必须位于 E:\\robot 的明确子目录中")
    if existing and not p.exists():
        raise ValueError(f"文件或目录不存在：{p}")
    return p


def number(value, lo, hi, name, integer=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} 不能为布尔值")
    v = float(value)
    if not math.isfinite(v) or not lo <= v <= hi or (integer and v != int(v)):
        raise ValueError(f"{name} 应在 {lo}–{hi} 之间" + ("，且为整数" if integer else ""))
    return int(v) if integer else v


def validate_config(raw):
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) - set(cfg):
        raise ValueError("包含未知配置项")
    cfg.update(raw)
    for key in ("python", "shared_scripts", "act_project", "dataset_home", "log_home",
                "calibration", "checkpoint", "reference_start_pose", "feedback_home"):
        cfg[key] = str(robot_path(cfg[key], existing=key not in ("log_home","feedback_home")))
    if Path(cfg["python"]).resolve() != Path("E:/robot/envs/lerobot/python.exe").resolve():
        raise ValueError("第一版固定复用现有 E 盘解释器")
    for key, identity in IDENTITIES.items():
        if cfg[key] != identity:
            raise ValueError("更换机械臂身份需要另行验证，不能直接覆盖本机配置")
    for key in ("top_camera", "wrist_camera"):
        cfg[key] = number(cfg[key], 0, 15, key, True)
    if cfg["top_camera"] == cfg["wrist_camera"]:
        raise ValueError("Top 和 Wrist 必须是不同设备")
    for key, lo, hi in (("preview_hz", 1, 10), ("episodes", 1, 30),
                        ("record_seconds", 1, 120), ("reset_seconds", 1, 120)):
        cfg[key] = number(cfg[key], lo, hi, key, True)
    cfg["sensor_hz"] = number(cfg["sensor_hz"], 1, 30, "扩展反馈频率", True)
    from reset_profile import validate_settings
    validate_settings(cfg)
    from software_limits import effective
    effective(cfg,json.loads(Path(cfg['calibration']).read_text(encoding='utf-8')))
    cfg["act_seconds"] = number(cfg["act_seconds"], .5, 17.5, "ACT 时限")
    cfg["task"] = str(cfg["task"]).strip()
    if not cfg["task"] or len(cfg["task"]) > 300:
        raise ValueError("任务描述应为 1–300 字")
    if not isinstance(cfg["rerun"], bool):
        raise ValueError("Rerun 开关必须为布尔值")
    return cfg


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(name, path)
    finally:
        if Path(name).exists():
            Path(name).unlink()


def load_config():
    state = HERE / "state/settings.json"
    return validate_config(json.loads(state.read_text(encoding="utf-8")) if state.exists() else {})
