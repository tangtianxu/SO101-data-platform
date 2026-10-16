"""Portable configuration contracts. Importing this module never opens hardware."""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import sys
import tempfile

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
NAMES = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _local_settings():
    value = {}
    for path in (HERE / "config.json", STATE_DIR / "settings.json"):
        if path.is_file():
            item = _json(path)
            if not isinstance(item, dict):
                raise ValueError(f"配置必须是对象：{path}")
            value.update(item)
    return value


def workspace_path(value):
    if not str(value).strip():
        raise ValueError("工作区不能为空")
    path = Path(value).expanduser()
    path = (path if path.is_absolute() else HERE / path).resolve()
    if path.parent == path or path == Path.home().resolve():
        raise ValueError("工作区必须是明确子目录，不能使用磁盘根目录或整个用户目录")
    return path


def _workspace():
    saved = _local_settings()
    value = os.environ.get("SO101_WORKSPACE") or saved.get("workspace_root")
    if not value and saved.get("dataset_home"):
        value = Path(saved["dataset_home"]).parent
    return workspace_path(value or HERE / "workspace")


ROBOT_HOME = _workspace()


def robot_path(value, *, existing=False, root=None):
    if not str(value).strip():
        raise ValueError("路径不能为空")
    home = workspace_path(root or ROBOT_HOME)
    path = Path(value).expanduser()
    path = (path if path.is_absolute() else home / path).resolve()
    if path == home or not path.is_relative_to(home):
        raise ValueError(f"写入/管理路径必须位于工作区的明确子目录中：{home}")
    if existing and not path.exists():
        raise ValueError(f"文件或目录不存在：{path}")
    return path


def input_path(value, *, existing=True, root=None):
    """Explicit read-only resources may be outside the managed output workspace."""
    if not str(value).strip():
        raise ValueError("尚未配置此资源路径")
    path = Path(value).expanduser()
    path = (path if path.is_absolute() else Path(root or ROBOT_HOME) / path).resolve()
    if existing and not path.exists():
        raise ValueError(f"文件或目录不存在：{path}")
    return path


def defaults():
    return {
        "workspace_root": str(ROBOT_HOME), "python": sys.executable,
        "shared_scripts": str(HERE / "runtime/shared_scripts"),
        "act_project": "",  # Accepted for old private configs; no longer a runtime dependency.
        "dataset_home": str(ROBOT_HOME / "datasets"), "log_home": str(ROBOT_HOME / "logs/data_platform"),
        "feedback_home": str(ROBOT_HOME / "data_feedback"),
        "leader_usb_serial": "", "follower_usb_serial": "", "leader_port": "auto", "follower_port": "auto",
        "calibration": "", "leader_calibration": "", "checkpoint": "", "reference_start_pose": "", "server_port": 8791,
        "top_camera": 0, "wrist_camera": 1, "preview_hz": 5, "sensor_hz": 10,
        "manual_joint_limits": {}, "manual_limits_calibration_id": None,
        "reset_arm_speed_deg_s": 4., "reset_gripper_speed_percent_s": 10.,
        "reset_joint_weights": dict(zip(NAMES[:5], (1., .8, .6, .4, .2))),
        "record_seconds": 30, "reset_seconds": 20, "episodes": 5, "task": "抓取物体放到目标区",
        "act_seconds": 17.5, "rerun": False, "gripper_overload_policy": "stop",
    }


def environment(cfg=None):
    cfg = cfg or {"workspace_root": str(ROBOT_HOME)}
    root = workspace_path(cfg["workspace_root"])
    paths = {"HF_HOME": root / "hf-cache", "TORCH_HOME": root / "torch-cache", "TEMP": root / "temp", "TMP": root / "temp",
             "TRITON_CACHE_DIR": root / "cache/triton", "TORCHINDUCTOR_CACHE_DIR": root / "cache/torchinductor", "CUDA_CACHE_PATH": root / "cache/cuda"}
    values = {"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1",
              "SO101_WORKSPACE": str(root), **{key: str(path) for key, path in paths.items()}}
    os.environ.update(values)
    return os.environ.copy()


def ensure_workspace(cfg):
    root = workspace_path(cfg["workspace_root"])
    for path in (root / "temp", root / "cache", Path(cfg["dataset_home"]), Path(cfg["log_home"]), Path(cfg["feedback_home"])):
        robot_path(path, root=root).mkdir(parents=True, exist_ok=True)


def number(value, lo, hi, name, integer=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} 不能为布尔值")
    v = float(value)
    if not math.isfinite(v) or not lo <= v <= hi or (integer and v != int(v)):
        raise ValueError(f"{name} 应在 {lo}–{hi} 之间" + ("，且为整数" if integer else ""))
    return int(v) if integer else v


def validate_config(raw):
    cfg = defaults()
    saved = _local_settings()
    if not isinstance(raw, dict) or set(raw) - set(cfg) or set(saved) - set(cfg):
        raise ValueError("包含未知配置项")
    cfg.update(saved)
    cfg.update(raw)
    root = workspace_path(cfg["workspace_root"])
    cfg["workspace_root"] = str(root)
    for key in ("dataset_home", "log_home", "feedback_home"):
        cfg[key] = str(robot_path(cfg[key], root=root))
    managed = [Path(cfg[key]) for key in ("dataset_home", "log_home", "feedback_home")]
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(managed) for b in managed[i+1:]):
        raise ValueError("数据集、日志和回流目录不能重叠或互相嵌套")
    for key in ("python", "shared_scripts", "act_project", "calibration", "leader_calibration", "checkpoint", "reference_start_pose"):
        text = str(cfg[key]).strip()
        cfg[key] = str(input_path(text, existing=False, root=root)) if text else ""
    if Path(cfg['shared_scripts']).resolve() != (HERE/'runtime/shared_scripts').resolve():
        raise ValueError('便携版使用随仓库的 runtime/shared_scripts；请移除旧配置中的外部适配器路径，避免混用控制代码')
    for key in ("leader_usb_serial", "follower_usb_serial"):
        cfg[key] = str(cfg[key]).strip()
        if cfg[key].startswith("SET_") or len(cfg[key]) > 200:
            raise ValueError("请填写自己的 USB 序列号，或留空保持未配置")
    if cfg["leader_usb_serial"] and cfg["leader_usb_serial"] == cfg["follower_usb_serial"]:
        raise ValueError("主从臂不能绑定同一个 USB 身份")
    for key in ("leader_port", "follower_port"):
        cfg[key] = str(cfg[key]).strip() or "auto"
        if cfg[key].lower() == 'auto':cfg[key]='auto'
        elif cfg[key][:3].upper()=='COM' and cfg[key][3:].isdigit():cfg[key]=cfg[key].upper()
        if len(cfg[key]) > 200:
            raise ValueError("串口路径过长")
    if cfg['leader_port']!='auto' and cfg['leader_port']==cfg['follower_port']:
        raise ValueError('主从臂不能配置为同一个显式串口')
    for key in ("top_camera", "wrist_camera"):
        cfg[key] = number(cfg[key], 0, 63, key, True)
    if cfg["top_camera"] == cfg["wrist_camera"]:
        raise ValueError("Top 和 Wrist 必须是不同设备")
    for key, lo, hi in (("preview_hz", 1, 10), ("episodes", 1, 1000), ("record_seconds", 1, 120),
                        ("reset_seconds", 1, 120), ("sensor_hz", 1, 30), ("server_port", 1024, 65535)):
        cfg[key] = number(cfg[key], lo, hi, key, True)
    from reset_profile import validate_settings
    validate_settings(cfg)
    if not isinstance(cfg['manual_joint_limits'],dict):
        raise ValueError('软件限位必须是对象')
    if cfg["manual_joint_limits"]:
        from software_limits import effective
        effective(cfg, read_calibration(cfg["calibration"]))
    cfg["act_seconds"] = number(cfg["act_seconds"], .5, 17.5, "ACT 时限")
    cfg["task"] = str(cfg["task"]).strip()
    if not cfg["task"] or len(cfg["task"]) > 300:
        raise ValueError("任务描述应为 1–300 字")
    if not isinstance(cfg["rerun"], bool):
        raise ValueError("Rerun 开关必须为布尔值")
    if cfg["gripper_overload_policy"] not in ("stop", "log_gripper_only"):
        raise ValueError("夹爪报警策略无效")
    return cfg


def read_calibration(value):
    path = input_path(value)
    if not path.is_file() or path.suffix.lower() != ".json":
        raise ValueError("请指定机械臂标定 JSON 文件")
    if path.stat().st_size > 128000:
        raise ValueError('标定文件过大，请选择六关节标定 JSON')
    cal = _json(path)
    keys = {"id", "drive_mode", "homing_offset", "range_min", "range_max"}
    if not isinstance(cal, dict) or set(cal) != set(NAMES):
        raise ValueError("标定必须包含 SO101 的全部六个标准关节")
    for name in NAMES:
        item = cal[name]
        if not isinstance(item, dict) or set(item) != keys:
            raise ValueError(name + " 标定字段不完整")
        number(item["id"], 0, 253, name + " ID", True)
        number(item["drive_mode"], 0, 1, name + " drive_mode", True)
        number(item["homing_offset"], -4095, 4095, name + " offset", True)
        number(item["range_min"], 0, 4095, name + " 下限", True)
        number(item["range_max"], item["range_min"] + 1, 4095, name + " 上限", True)
    if len({cal[n]["id"] for n in NAMES}) != 6:
        raise ValueError("六个关节的舵机 ID 必须互不重复")
    return cal


def require_capability(cfg, kind):
    python = input_path(cfg["python"])
    if not python.is_file():
        raise ValueError("请配置 LeRobot 环境的 Python 解释器文件")
    if kind in ("monitor", "control", "record", "teleop", "act", "replay"):
        if not cfg["follower_usb_serial"]:
            raise ValueError("尚未绑定从臂 USB 序列号")
        read_calibration(cfg["calibration"])
    if kind in ("record", "teleop"):
        if not cfg["leader_usb_serial"]:
            raise ValueError("尚未绑定主臂 USB 序列号")
        read_calibration(cfg["leader_calibration"])
    if kind in ("control", "record", "teleop", "act", "replay"):
        for name in ("so101_guarded_real_act.py", "so101_act_shadow.py", "so101_guarded_real_replay.py", "so101_profile.py"):
            if not (input_path(cfg["shared_scripts"]) / name).is_file():
                raise ValueError("缺少控制适配脚本：" + name)
    if kind == "act":
        checkpoint = input_path(cfg["checkpoint"])
        if not checkpoint.is_dir() or not all((checkpoint / n).is_file() for n in ("config.json", "model.safetensors")):
            raise ValueError("请选择包含 config.json 和 model.safetensors 的 ACT pretrained_model 目录")
        from reset_profile import inspect_pose
        inspect_pose(cfg)


def capabilities(cfg):
    result = {}
    for kind in ("preview", "monitor", "control", "record", "teleop", "act", "replay", "crop"):
        try:
            require_capability(cfg, kind)
            result[kind] = {"ready": True, "reason": ""}
        except (ValueError, OSError, KeyError, TypeError) as error:
            result[kind] = {"ready": False, "reason": str(error)}
    return result


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
    cfg=validate_config({})
    if Path(cfg['workspace_root']) != ROBOT_HOME:
        raise ValueError('SO101_WORKSPACE 环境变量与保存的工作区冲突，请移除启动覆盖或恢复一致配置后重开')
    return cfg
