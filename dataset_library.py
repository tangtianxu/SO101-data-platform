"""Dataset-folder management. Removal is a recoverable workspace archive, never rmtree."""
from datetime import datetime
import json
from pathlib import Path
import re
import secrets

from common import ROBOT_HOME, atomic_json, robot_path
from dataset_naming import describe, registry, root_key

ARCHIVE = ROBOT_HOME / "dataset_archive"


def protected_roots(cfg):
    protected = {ROBOT_HOME / "datasets/so101_supplement_20261015": "新采的 30 条原始示范"}
    config = Path(cfg["checkpoint"]) / "train_config.json"
    if config.is_file():
        training = json.loads(config.read_text(encoding="utf-8"))
        root = training.get("dataset", {}).get("root")
        if root:
            protected[robot_path(root)] = "当前 ACT 权重实际使用的训练数据"
    return {p.resolve(): reason for p, reason in protected.items()}


def child_folder(cfg, root, existing=True):
    home = robot_path(cfg["dataset_home"], existing=True)
    folder = robot_path(root, existing=existing)
    known=registry()['datasets'].get(root_key(folder))
    if not known:
        from capture_groups import group_context
        known=group_context(folder)
    if (folder.parent != home and not known) or folder == home:
        raise ValueError("只能管理数据集总目录的直接子文件夹，不能操作总目录、嵌套路径或外部目录")
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise ValueError("不管理符号链接或普通文件")
    return folder


def dataset_time(folder, naming):
    """Acquisition time, never the mutable registry update/file modification time."""
    start=(naming.get('simple_spec') or {}).get('started_at')
    if isinstance(start,str) and start:
        try:
            stamp=datetime.fromisoformat(start.replace('Z','+00:00')).timestamp()
            return {'sort_time':stamp,'time_source':'collection_start','started_at':start}
        except (ValueError,TypeError,OverflowError,OSError):
            pass
    plan=folder/'.platform-plan.json'
    if plan.is_file():
        try:
            created=json.loads(plan.read_text(encoding='utf-8'))['created']
            if not isinstance(created,str):raise ValueError('invalid plan creation time')
            stamp=datetime.fromisoformat(created.replace('Z','+00:00')).timestamp()
            return {'sort_time':stamp,'time_source':'plan_created','started_at':created}
        except (OSError,ValueError,TypeError,KeyError,OverflowError):
            pass
    stat=folder.stat()
    stamp=getattr(stat,'st_birthtime',stat.st_ctime)
    return {'sort_time':stamp,'time_source':'filesystem_created','started_at':None}


def list_datasets(cfg):
    protected = protected_roots(cfg)
    names = registry()
    result = []
    folders=set(Path(cfg['dataset_home']).iterdir())
    folders.update(Path(item['root']) for item in names['datasets'].values() if Path(item['root']).is_dir())
    from capture_groups import collections
    for collection in collections(cfg):folders.update(Path(group['root']) for group in collection['groups'] if Path(group['root']).is_dir())
    for folder in sorted(folders, key=lambda p: p.name, reverse=True):
        if folder.name.startswith(".") or not folder.is_dir() or folder.is_symlink():
            continue
        info = folder / "meta/info.json"
        plan = folder / ".platform-plan.json"
        if not info.is_file() and not plan.is_file():
            continue
        try:
            meta = json.loads(info.read_text(encoding="utf-8")) if info.is_file() else {}
            naming=describe(folder,names)
            from capture_groups import group_context
            try:context=group_context(folder)
            except (OSError,ValueError,KeyError,TypeError):context=None
            result.append({"name": folder.name, "root": str(folder), "episodes": meta.get("total_episodes", 0),
                "frames": meta.get("total_frames", 0), "version": meta.get("codebase_version", "待采集"),
                "ready": info.is_file(), "protected": protected.get(folder.resolve()),
                "naming": naming, "capture_group":context, **dataset_time(folder,naming)})
        except (OSError, ValueError):
            continue
    # A label edit must not promote an old dataset, and older datasets must remain discoverable.
    result.sort(key=lambda item:(item['sort_time'],item['root'].casefold()),reverse=True)
    return result


def create_dataset(cfg, name):
    name = str(name).strip()
    if not re.fullmatch(r"[^\W_][\w-]{0,159}", name) or name.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}:
        raise ValueError("名称支持中文、字母、数字、下划线或短横线，不使用路径分隔符或 Windows 保留名")
    folder = child_folder(cfg, Path(cfg["dataset_home"]) / name, existing=False)
    folder.mkdir(exist_ok=False)
    atomic_json(folder / ".platform-plan.json", {"created": datetime.now().isoformat(), "recorded": False})
    return {"root": str(folder), "name": name}


def archive_dataset(cfg, root, confirmed_name):
    folder = child_folder(cfg, root)
    if confirmed_name != folder.name:
        raise ValueError("请核对要移除的完整数据集名称")
    if folder in protected_roots(cfg):
        raise PermissionError("此数据集已受保护：" + protected_roots(cfg)[folder])
    if not (folder / "meta/info.json").is_file() and not (folder / ".platform-plan.json").is_file():
        raise ValueError("目录不是已识别的数据集或采集计划，不移动未知文件")
    ARCHIVE.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3)
    destination = robot_path(ARCHIVE / (stamp + "__" + folder.name))
    if destination.parent != ARCHIVE.resolve() or destination.exists():
        raise ValueError("归档目标无效")
    # Manifest is committed BEFORE the same-volume rename, allowing recovery after interruption.
    manifest = ARCHIVE / (stamp + ".json")
    record = {"source": str(folder), "destination": str(destination), "status": "pending",
              "created": datetime.now().isoformat(), "recoverable": True, "permanent_delete": False,
              "manifest": str(manifest)}
    atomic_json(manifest, record)
    folder.rename(destination)
    record["status"] = "archived"
    atomic_json(manifest, record)
    return record


def restore_dataset(cfg, manifest_path):
    manifest = robot_path(manifest_path, existing=True)
    if manifest.parent != ARCHIVE.resolve() or manifest.suffix != ".json":
        raise ValueError("只接受归档目录内的清单")
    record = json.loads(manifest.read_text(encoding="utf-8"))
    source = child_folder(cfg, record["source"], existing=False)
    archived = robot_path(record["destination"], existing=True)
    if archived.parent != ARCHIVE.resolve() or source.exists() or record.get("status") != "archived":
        raise ValueError("归档状态不符或原目录已存在；不会覆盖")
    archived.rename(source)
    record.update(status="restored", restored=datetime.now().isoformat())
    atomic_json(manifest, record)
    return record


def list_archives():
    result = []
    for path in sorted(ARCHIVE.glob("*.json"), reverse=True):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("status") == "archived":
                result.append({"manifest": str(path), "name": Path(record["source"]).name,
                               "created": record.get("created"), "source": record["source"]})
        except (OSError, ValueError, KeyError):
            continue
    return result[:100]
