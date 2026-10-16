"""Stable dataset paths, explicit processing/review metadata, and human-readable labels.

Changing a review label NEVER claims to process data, move a folder, or alter a
training split. Existing model/script references remain valid.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import unicodedata
import uuid

from common import HERE, ROBOT_HOME, atomic_json, number, robot_path

REGISTRY = ROBOT_HOME / "platform_state/dataset_registry.json"
DOMAINS = {"real": "实机", "sim": "仿真"}
TASKS = {"doll_pick_place": "娃娃抓放", "pick_place": "通用抓放", "material_grasp": "材质夹取判别"}
PURPOSES = {"demo": "正式示范", "smoke": "联调测试", "eval": "评估"}
STAGES = {"raw": "原始", "processed": "已处理"}
REVIEWS = {"pending": "待审核", "train": "可训练·人工确认", "exclude": "排除训练"}
FIELDS = {"domain", "task", "purpose", "stage", "review", "date", "batch", "version", "note"}
PATTERN = re.compile(r"^so101_(real|sim)_([a-z][a-z0-9_]{0,23})_(demo|smoke|eval)_(raw|proc)_(\d{8})_b(\d{3})_v(\d{3})$")
SIMPLE_FIELDS = {"schema_version","domain","stage","review","started_at","instruction","description","note","sequence"}


def simple_defaults(instruction="把娃娃放进盒子"):
    return {"schema_version":2,"domain":"real","stage":"raw","review":"pending",
            "started_at":datetime.now().astimezone().isoformat(timespec="minutes"),
            "instruction":instruction,"description":"固定位置","note":"","sequence":1}


def simple_spec(spec):
    """Present legacy labels without inventing a collection time that was never recorded."""
    s=validate_spec(spec)
    if s.get("schema_version")==2:
        return s
    return {**simple_defaults(TASKS.get(s['task'],s['task'])),"domain":s['domain'],"stage":s['stage'],
        "review":s['review'],"started_at":datetime.strptime(s['date'],'%Y%m%d').strftime('%Y-%m-%d'),
        "description":s['note'][:100] or '历史数据',"note":s['note']}


def filename_text(value, limit):
    value=unicodedata.normalize("NFKC",value)
    value=re.sub(r'[<>:"/\\|?*\x00-\x1f\s]+','_',value).strip(' ._')
    return value[:limit].rstrip(' ._') or '未注明'


def capture_plan(cfg, request):
    """System timestamp is authoritative; previewing never creates a folder."""
    parent=robot_path(request.get('parent') or cfg['dataset_home'],existing=True)
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError('请选择 E 盘的实际数据集父目录')
    if any((p/'.capture-groups.json').is_file() for p in (parent,*parent.parents)):
        raise ValueError('已有分组采集目录内不能嵌套新建；请补录该总文件夹，或选择外部数据集父目录')
    for record in registry()['datasets'].values():
        root=Path(record['root'])
        if parent==root or parent.is_relative_to(root):
            raise ValueError('父目录不能是已有数据集或其内部，请选择 datasets 这样的总目录')
    spec=simple_defaults(str(request.get('instruction') or cfg['task']).strip())
    spec['description']=str(request.get('description') or '固定位置').strip()
    spec['note']=str(request.get('note') or '').strip()
    result=suggest_name({**cfg,'dataset_home':str(parent)},spec)
    result['parent']=str(parent)
    return result


def defaults():
    return {"domain": "real", "task": "doll_pick_place", "purpose": "demo", "stage": "raw",
            "review": "pending", "date": datetime.now().strftime("%Y%m%d"), "batch": 1, "version": 1, "note": ""}


def validate_spec(value):
    if isinstance(value,dict) and value.get('schema_version')==2:
        if set(value)!=SIMPLE_FIELDS:
            raise ValueError('命名信息不完整')
        s=value.copy()
        for field,choices in (('domain',DOMAINS),('stage',STAGES),('review',REVIEWS)):
            if not isinstance(s[field],str) or s[field] not in choices:raise ValueError('未知分类：'+field)
        if not isinstance(s['started_at'],str):raise ValueError('开始时间格式不正确')
        try:
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}',s['started_at']):datetime.strptime(s['started_at'],'%Y-%m-%d')
            else:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?:[+-]\d{2}:\d{2})?',s['started_at']):raise ValueError()
                datetime.fromisoformat(s['started_at'])
        except ValueError:raise ValueError('开始时间不是有效的日期或分钟时间') from None
        for field,limit in (('instruction',300),('description',100),('note',200)):
            if not isinstance(s[field],str) or len(s[field])>limit or any(ord(c)<32 and c not in '\n\t' for c in s[field]):raise ValueError(field+' 文本过长或含控制字符')
            s[field]=s[field].strip()
        if not s['instruction']:raise ValueError('请输入语言指令')
        s['sequence']=number(s['sequence'],1,999,'同分钟重名序号',True)
        return s
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("请通过命名选项填写完整分类，不直接输入任意文件夹名")
    spec=value.copy()
    for field, choices in (("domain", DOMAINS), ("purpose", PURPOSES), ("stage", STAGES), ("review", REVIEWS)):
        if not isinstance(spec[field],str) or spec[field] not in choices:
            raise ValueError("未知分类："+field)
    if not isinstance(spec["task"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", spec["task"]):
        raise ValueError("自定义任务代码需为 1–24 位小写字母、数字或下划线，以字母开头；中文说明写在备注")
    if not isinstance(spec["date"], str) or not re.fullmatch(r"\d{8}", spec["date"]):
        raise ValueError("日期需为 YYYYMMDD")
    try:
        datetime.strptime(spec["date"], "%Y%m%d")
    except ValueError:
        raise ValueError("日期不是有效日历日期") from None
    for key in ("batch", "version"):
        spec[key]=number(spec[key], 1, 999, key, True)
    if not isinstance(spec["note"], str) or len(spec["note"]) > 200 or any(ord(c)<32 and c not in "\n\t" for c in spec["note"]):
        raise ValueError("备注最多 200 字，不使用控制字符")
    spec["note"]=spec["note"].strip()
    if spec["purpose"] != "demo" and spec["review"] == "train":
        raise ValueError("联调/评估数据不直接标为训练示范；请保留实验边界，另行筛选导出")
    return spec


def canonical_name(spec):
    s=validate_spec(spec)
    stage="raw" if s["stage"]=="raw" else "proc"
    if s.get('schema_version')==2:
        stamp=s['started_at'][:16].replace('-','').replace(':','').replace('T','_')
        name=f"so101_{s['domain']}_{stage}_{stamp}_{filename_text(s['instruction'],24)}_{filename_text(s['description'],18)}"
        return name+(f"_{s['sequence']:02d}" if s['sequence']>1 else '')
    return f"so101_{s['domain']}_{s['task']}_{s['purpose']}_{stage}_{s['date']}_b{s['batch']:03d}_v{s['version']:03d}"


def parse_name(name):
    match=PATTERN.fullmatch(name)
    if not match:
        return None
    domain, task, purpose, stage, date, batch, version=match.groups()
    return validate_spec({"domain":domain,"task":task,"purpose":purpose,"stage":"raw" if stage=="raw" else "processed",
        "review":"pending","date":date,"batch":int(batch),"version":int(version),"note":""})


def root_key(root):
    return os.path.normcase(str(Path(root).resolve()))


def registry():
    if not REGISTRY.exists():
        return {"schema_version":1,"datasets":{}}
    value=json.loads(REGISTRY.read_text(encoding="utf-8"))
    if value.get("schema_version")!=1 or not isinstance(value.get("datasets"),dict):
        raise ValueError("数据集名称登记表格式异常，请检查，不静默覆盖")
    return value


def describe(root, records=None):
    root=Path(root).resolve()
    item=(records if records is not None else registry())["datasets"].get(root_key(root))
    spec=item["spec"] if item else parse_name(root.name)
    if not spec:
        return {"classified":False,"display_name":"[未分类] "+root.name,"canonical_name":None,
                "spec":None,"note":"","physical_name":root.name,"append_allowed":False}
    spec=validate_spec(spec)
    simple=simple_spec(spec)
    label=f"[{DOMAINS[spec['domain']]}·{STAGES[spec['stage']]}·{REVIEWS[spec['review']]}] {simple['started_at'][:16].replace('T',' ')} · {simple['instruction']} · {simple['description']}"
    if spec["note"]:
        label+=" · "+spec["note"].replace("\n"," ")
    return {"classified":True,"display_name":label,"canonical_name":canonical_name(simple),"spec":spec,
        "note":spec["note"],"physical_name":root.name,"dataset_id":item.get("id") if item else None,"simple_spec":simple,
        "append_allowed":spec["domain"]=="real" and spec["stage"]=="raw",
        "source":item.get("source") if item else "canonical folder name; review unconfirmed"}


def store_metadata(root, spec, *, review_confirmed=False, source="operator", lineage=None):
    root=robot_path(root)
    spec=validate_spec(spec)
    if spec["review"]=="train" and not review_confirmed:
        raise ValueError("标为可训练需要人工确认；命名不等于完成数据处理或质量审核")
    records=registry();key=root_key(root);previous=records["datasets"].get(key)
    alias=canonical_name(spec)
    for other_key,item in records["datasets"].items():
        other=Path(item["root"])
        if other_key!=key and other.parent==root.parent and other.exists() and canonical_name(item["spec"])==alias:
            raise ValueError("相同名称已被另一个数据集使用，请调整描述；新采集同分钟会自动添加重名序号")
    entry={"id":previous["id"] if previous else str(uuid.uuid4()),"root":str(root),"spec":spec,
        "created":previous["created"] if previous else datetime.now().isoformat(),"updated":datetime.now().isoformat(),
        "source":source,"review_confirmed":spec["review"]=="train" and review_confirmed,
        "lineage":lineage if lineage is not None else (previous.get("lineage") if previous else None)}
    records["datasets"][key]=entry
    # Preserve audit history but bound it; no source Parquet/MP4/metadata is modified.
    history=records.setdefault("history",[])
    history.append({"root":str(root),"at":entry["updated"],"before":previous.get("spec") if previous else None,"after":spec})
    records["history"]=history[-1000:]
    atomic_json(REGISTRY,records)
    return describe(root,records)


def preview_name(cfg, spec, ignore_root=None):
    spec=validate_spec(spec)
    name=canonical_name(spec);root=robot_path(Path(cfg["dataset_home"])/name)
    if root.parent!=Path(cfg["dataset_home"]).resolve():
        raise ValueError("命名输出目录不在数据集库")
    reserved=any(root_key(item["root"])!=root_key(root) and (ignore_root is None or root_key(item["root"])!=root_key(ignore_root)) and Path(item["root"]).parent==root.parent and Path(item["root"]).exists()
                 and canonical_name(item["spec"])==name for item in registry()["datasets"].values())
    physical=root.exists() and (ignore_root is None or root_key(root)!=root_key(ignore_root))
    return {"name":name,"root":str(root),"exists":physical or reserved,"physical_exists":physical,"name_reserved":reserved,"spec":spec}


def validate_capture(cfg, root, spec, append_confirmed):
    root=robot_path(root)
    if root.parent!=Path(cfg["dataset_home"]).resolve():
        raise ValueError("采集数据集必须是数据集库中的独立直接子目录")
    existing=describe(root)
    if root.exists():
        if not existing["append_allowed"]:
            raise ValueError("补录只写实机原始数据集，处理版保留不混入原始录像；请新建原始数据集")
        if append_confirmed is not True and (root/"meta/info.json").exists():
            raise ValueError("追加已有数据必须明确确认")
        return {**existing["spec"],"review":"pending"}
    checked=validate_spec(spec)
    if checked["domain"]!="real" or checked["stage"]!="raw" or checked["review"]!="pending":
        raise ValueError("真机采集的新数据固定为 real/raw/待审核，不能伪装为已处理或已审核")
    planned=preview_name(cfg,checked)
    if planned["root"]!=str(root):
        raise ValueError("采集目录与规范命名选项不符，请使用自动生成的目录")
    if planned["exists"]:
        raise ValueError("此规范名称已被占用，请增加批次或版本")
    return checked


def suggest_name(cfg, spec, increment="batch"):
    checked=validate_spec(spec)
    if checked.get('schema_version')==2:
        while preview_name(cfg,checked)['exists']:
            checked['sequence']+=1
            checked=validate_spec(checked)
        return preview_name(cfg,checked)
    if increment not in ("batch","version"):
        raise ValueError("只自动递增批次或版本")
    while preview_name(cfg,checked)["exists"]:
        checked[increment]+=1
        checked=validate_spec(checked)
    return preview_name(cfg,checked)


def seed_retained_datasets(cfg):
    """Classify only the two known retained datasets; do not infer quality from arbitrary filenames."""
    seeds=[("so101_supplement_20261015",{"stage":"raw","review":"pending","date":"20261015","note":"补录30条，待审核"}),
           ("so101_pick_place_clean_no_hand_20260925",{"stage":"processed","review":"train","date":"20260925","note":"去手版10条，现用ACT训练数据"})]
    current=registry();changed=[]
    for name, fields in seeds:
        root=Path(cfg["dataset_home"])/name
        if (root/"meta/info.json").is_file() and root_key(root) not in current["datasets"]:
            store_metadata(root,{**defaults(),**fields},review_confirmed=fields["review"]=="train",
                           source="migration_from_verified_project_history")
            changed.append(str(root))
    return changed
