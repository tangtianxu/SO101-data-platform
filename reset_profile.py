"""Task-start and reset settings; no hardware imports, no firmware writes."""
from datetime import datetime
import json
from pathlib import Path
import secrets

from common import HERE, NAMES, atomic_json, number, robot_path

ARM = NAMES[:5]


def validate_settings(cfg):
    cfg["reset_arm_speed_deg_s"] = number(cfg["reset_arm_speed_deg_s"], .1, 20, "复位基准目标速度")
    cfg["reset_gripper_speed_percent_s"] = number(cfg["reset_gripper_speed_percent_s"], .1, 100, "夹爪复位目标速度")
    weights = cfg["reset_joint_weights"]
    if not isinstance(weights, dict) or set(weights) != set(ARM):
        raise ValueError("复位速度比例必须包含五个转动关节，不混用夹爪百分比单位")
    cfg["reset_joint_weights"] = {n: number(weights[n], .05, 1, n+" 复位速度比例") for n in ARM}
    return cfg


def effective_speeds(cfg):
    checked = validate_settings(cfg.copy())
    return [checked["reset_arm_speed_deg_s"]*checked["reset_joint_weights"][n] for n in ARM]+[checked["reset_gripper_speed_percent_s"]]


def inspect_pose(cfg, path=None):
    path = robot_path(path or cfg["reference_start_pose"], existing=True)
    if not path.is_file() or path.stat().st_size > 128000:
        raise ValueError("请选择有效的任务起点 JSON 文件")
    reference = json.loads(path.read_text(encoding="utf-8"))
    calibration = json.loads(Path(cfg["calibration"]).read_text(encoding="utf-8"))
    if reference.get("joint_order") != list(NAMES) or reference.get("calibration_reference") != calibration:
        raise ValueError("任务起点与当前标定/关节顺序不一致，不允许直接套用")
    if reference.get("units") != ["degrees"]*5+["gripper_percent"]:
        raise ValueError("任务起点单位不一致")
    target = reference.get("normalized_position", {})
    if set(target) != set(NAMES):
        raise ValueError("任务起点需要完整的六关节位置")
    positions = {}
    for n in NAMES:
        c = calibration[n]
        span = (c["range_max"]-c["range_min"])*180/4095
        lo, hi = (0, 100) if n == "gripper" else (-span, span)
        positions[n] = number(target[n], lo, hi, n+" 起点")
    return {"path": str(path), "positions": positions, "units": reference["units"],
            "source_dataset": reference.get("source_dataset"), "source_episode_zero_based": reference.get("source_episode_zero_based"),
            "source": reference.get("source", reference.get("source_field", "saved task preset")),
            "effective_speeds": effective_speeds(cfg), "collision_checked": False,
            "note": "任务起点不是舵机中位；保存目标不证明途中无自碰撞或环境碰撞"}


def save_pose_copy(cfg, positions, source_path=None):
    if not isinstance(positions, dict) or set(positions) != set(NAMES):
        raise ValueError("起点必须包含完整六关节位置")
    source = inspect_pose(cfg, source_path)
    calibration = json.loads(Path(cfg["calibration"]).read_text(encoding="utf-8"))
    # Build a new task preset; never overwrite the existing reference/calibration or claim
    # these edited coordinates still originate from an expert demonstration frame.
    reference = {"status": "application task-start preset, not servo midpoint or factory home",
        "source": "operator_ui_position_editor", "created": datetime.now().isoformat(),
        "joint_order": list(NAMES), "units": ["degrees"]*5+["gripper_percent"],
        "calibration_reference": calibration, "normalized_position": positions,
        "previous_preset": source["path"], "collision_checked": False,
        "requires_operator_route_confirmation": True}
    for n in NAMES:
        c=calibration[n];span=(c["range_max"]-c["range_min"])*180/4095
        number(positions[n], *((0,100) if n=="gripper" else (-span,span)), n+" 起点")
    path=HERE/"state/start_poses"/(datetime.now().strftime("%Y%m%d_%H%M%S")+"_"+secrets.token_hex(3)+".json")
    atomic_json(path,reference)
    return inspect_pose(cfg,path)
