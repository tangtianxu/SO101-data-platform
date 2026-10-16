"""Per-worker profile; no fixed machine paths, implicit device binding or device IO."""
import os
from pathlib import Path


def workspace():
    return Path(os.environ.get("SO101_WORKSPACE", Path(__file__).resolve().parents[2] / "workspace")).resolve()


def resource(key, fallback):
    return Path(os.environ.get("SO101_" + key.upper()) or workspace() / fallback)


def serial(role):
    return os.environ.get("SO101_" + role.upper() + "_SERIAL", "").strip()


def allow_gripper_overload():
    return os.environ.get('SO101_GRIPPER_OVERLOAD_POLICY','stop') == 'log_gripper_only'


def configure(cfg):
    for key in ("workspace_root", "calibration", "leader_calibration", "checkpoint", "reference_start_pose", "dataset_home", "log_home"):
        os.environ["SO101_" + ("WORKSPACE" if key == "workspace_root" else key.upper())] = str(cfg.get(key) or "")
    for role in ("leader", "follower"):
        os.environ["SO101_" + role.upper() + "_SERIAL"] = cfg[role + "_usb_serial"]
        os.environ["SO101_" + role.upper() + "_PORT"] = cfg.get(role + "_port", "auto")
    os.environ["SO101_GRIPPER_OVERLOAD_POLICY"] = cfg.get("gripper_overload_policy", "stop")


def output_path(value):
    path = Path(value).resolve()
    root = workspace()
    if path == root or not path.is_relative_to(root):
        raise ValueError("输出必须位于配置工作区的明确子目录中")
    return path


def initialize_environment():
    root = workspace()
    for key, relative in (("HF_HOME", "hf-cache"), ("TORCH_HOME", "torch-cache"), ("TEMP", "temp"), ("TMP", "temp")):
        os.environ.setdefault(key, str(root / relative))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")


def resolve_port(role, ports, explicit="auto"):
    expected = serial(role)
    if not expected or expected.startswith("SET_"):
        raise ValueError(role + " USB 身份尚未配置；不会猜测或连接任意串口")
    matches = [p for p in ports if p.serial_number == expected]
    if len(matches) != 1:
        raise RuntimeError(f"{role} USB 身份应唯一匹配，实际 {len(matches)} 个；未连接设备")
    chosen = os.environ.get("SO101_" + role.upper() + "_PORT", "auto") if explicit == "auto" else explicit
    if chosen != "auto" and matches[0].device != chosen:
        raise RuntimeError(role + " 串口号与配置 USB 身份不一致；未连接设备")
    return matches[0].device
