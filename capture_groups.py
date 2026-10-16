"""Instruction groups: a collection folder with standard LeRobot datasets beneath it.

Planning is read-only. Commit only creates missing plan folders and application metadata;
it never moves existing datasets or changes calibration/motion parameters.
"""
from datetime import datetime
import json
from pathlib import Path

from common import atomic_json, number, robot_path
from capture_counts import episode_count
from dataset_naming import capture_plan, describe, filename_text, registry, simple_defaults, store_metadata, validate_capture

MARKER='.capture-groups.json'


def normalize_groups(value):
    if value is None:return []
    if not isinstance(value,list) or len(value)>30:raise ValueError('分组计划应为列表，最多 30 行')
    merged={}
    for row in value:
        if not isinstance(row,dict):raise ValueError('分组格式无效')
        task=row.get('instruction','')
        if not isinstance(task,str):raise ValueError('小组指令应为文本')
        task=task.strip()
        if not task:continue
        if len(task)>300:raise ValueError('每组语言指令最多 300 字')
        count=episode_count(row.get('episodes'),'每组条数')
        merged[task]=merged.get(task,0)+count
    if merged:episode_count(sum(merged.values()),'本次总条数')
    return [{'instruction':task,'episodes':count} for task,count in merged.items()]


def read_collection(root):
    raw=Path(root)
    if raw.is_symlink():raise ValueError('不管理符号链接形式的采集总文件夹')
    root=robot_path(raw,existing=True);file=root/MARKER
    if not file.is_file() or file.is_symlink() or file.stat().st_size>1000000:raise ValueError('不是有效的分组采集总文件夹')
    data=json.loads(file.read_text(encoding='utf-8'))
    if data.get('schema_version')!=1 or data.get('kind')!='instruction_groups' or not isinstance(data.get('groups'),list):raise ValueError('分组采集清单格式不支持')
    tasks=set();ids=set()
    for group in data['groups']:
        if not isinstance(group,dict) or not isinstance(group.get('instruction'),str) or not group['instruction'].strip():raise ValueError('分组清单指令无效')
        folder=group.get('folder');gid=group.get('id')
        if not isinstance(folder,str) or not folder or Path(folder).name!=folder or '/' in folder or '\\' in folder or folder in ('.','..'):raise ValueError('分组路径无效')
        target=(root/folder).resolve()
        if target.parent!=root or (root/folder).is_symlink():raise ValueError('分组路径越出采集总文件夹')
        if not isinstance(gid,str) or gid in ids or group['instruction'] in tasks:raise ValueError('分组清单存在重复指令或编号')
        ids.add(gid);tasks.add(group['instruction'])
    return data


def collections(cfg):
    roots=set(Path(cfg['dataset_home']).iterdir())
    roots.update(Path(item['root']).parent for item in registry()['datasets'].values())
    result=[]
    for root in roots:
        if not (root/MARKER).is_file():continue
        try:
            data=read_collection(root)
            result.append({'root':str(root.resolve()),'name':root.name,'created_at':data['created_at'],
                'groups':[dict(group,root=str((root/group['folder']).resolve())) for group in data['groups']]})
        except (OSError,ValueError,KeyError,TypeError):continue
    return sorted(result,key=lambda item:item['created_at'],reverse=True)


def group_context(root):
    root=Path(root)
    if not (root.parent/MARKER).is_file():return None
    data=read_collection(root.parent)
    group=next((item for item in data['groups'] if item['folder']==root.name),None)
    return dict(group,collection_root=str(root.parent),collection_name=root.parent.name) if group else None


def _prepare_groups(cfg,root,requests,base_spec,existing):
    marker=read_collection(root) if existing else {'schema_version':1,'kind':'instruction_groups',
        'created_at':base_spec['started_at'],'description':base_spec['description'],'groups':[]}
    marker={**marker,'groups':[dict(g) for g in marker['groups']]}
    targets=[]
    for request in requests:
        group=next((g for g in marker['groups'] if g['instruction']==request['instruction']),None)
        if group is None:
            index=len(marker['groups'])+1;gid=f'g{index:03}'
            while any(g['id']==gid for g in marker['groups']):index+=1;gid=f'g{index:03}'
            group={'id':gid,'folder':gid+'_'+filename_text(request['instruction'],36),'instruction':request['instruction']}
            marker['groups'].append(group)
        child=(root/group['folder']).resolve()
        if child.parent!=root or (root/group['folder']).is_symlink():raise ValueError('小组目录越界')
        info=child/'meta/info.json'
        if child.exists():
            if not child.is_dir():raise ValueError('小组目标不是文件夹')
            if not info.is_file() and any(p.name!='.platform-plan.json' for p in child.iterdir()):raise ValueError('小组有未完成或未知文件，不覆盖；请检查后重试')
            current=describe(child)
            if not current.get('append_allowed'):raise ValueError('已有小组只能补录原始数据集')
            if current['simple_spec']['instruction']!=group['instruction']:raise ValueError('小组显示指令与分组清单不一致，请先核对元数据，不混入其他任务')
            spec={**current['simple_spec'],'review':'pending'}
        else:
            spec={**simple_defaults(request['instruction']),
                  'description':(group['id']+' · '+marker['description'])[:100]}
            if not existing:spec['started_at']=marker['created_at']
        targets.append({'root':str(child),'id':group['id'],'instruction':group['instruction'],
                        'episodes':request['episodes'],'spec':spec})
    return {'root':str(root),'marker':marker,'targets':targets,'existing':existing}


def prepare_record(cfg,payload):
    """Validate the full new/append plan before any folder or worker is created."""
    from dataset_library import child_folder
    groups=normalize_groups(payload.get('instruction_groups'))
    if payload.get('episode_tasks'):raise ValueError('不再支持逐条指令；请按每组数量和每组指令配置')
    cfg['record_seconds']=number(payload.get('record_seconds',cfg['record_seconds']),1,120,'录制时长',True)
    cfg['reset_seconds']=number(payload.get('reset_seconds',cfg['reset_seconds']),1,120,'复位时长',True)
    cfg['task']=groups[0]['instruction'] if groups else str(payload.get('task',cfg['task'])).strip()
    if not cfg['task'] or len(cfg['task'])>300:raise ValueError('需要 1–300 字的默认语言指令')
    payload['group']=str(payload.get('group') or '固定位置').strip()[:100]
    cfg['episodes']=episode_count(sum(g['episodes'] for g in groups) if groups else payload.get('episodes',cfg['episodes']))
    mode=payload.get('capture_mode');new=mode=='new'
    if new:
        instruction=('分组采集' if len(groups)>1 else groups[0]['instruction']) if groups else cfg['task']
        plan=capture_plan(cfg,{'parent':payload.get('parent'),'instruction':instruction,'description':payload['group'],'note':payload.get('note','')})
        cfg['dataset_home']=plan['parent'];root=Path(plan['root']);payload['dataset_spec']=plan['spec']
    elif mode in ('append',None):
        root=robot_path(payload.get('dataset_root',''),existing=mode=='append')
        if (root/MARKER).is_file():
            if str(root) not in {item['root'] for item in collections(cfg)}:raise ValueError('请选择已登记的分组采集文件夹')
        elif mode=='append':
            root=child_folder(cfg,root);cfg['dataset_home']=str(root.parent)
    else:raise ValueError('采集模式只能是新建或补录')
    payload['dataset_root']=str(root)
    grouped=bool(groups) or (root/MARKER).is_file()
    if grouped:
        if not new and not (root/MARKER).is_file():raise ValueError('已有普通数据集不能改成分组目录；请新建分组采集或选择已有分组总文件夹')
        if not new and payload.get('append_confirmed') is not True:raise ValueError('补录分组采集需要明确的追加操作')
        requests=groups or [{'instruction':cfg['task'],'episodes':cfg['episodes']}]
        plan=_prepare_groups(cfg,root,requests,payload.get('dataset_spec'),existing=not new)
        payload.update(instruction_groups=requests,capture_targets=plan['targets'],capture_collection=str(root))
        return plan
    if not root.is_relative_to(Path(cfg['dataset_home']).resolve()) or root==Path(cfg['dataset_home']).resolve():raise ValueError('请选择独立数据集，不可使用数据集总目录')
    if (root/'meta/info.json').exists() and payload.get('append_confirmed') is not True:raise ValueError('已有数据集，必须明确追加')
    if root.exists() and not (root/'meta/info.json').exists() and any(p.name!='.platform-plan.json' for p in root.iterdir()):raise ValueError('非空目录没有完整元数据，不覆盖残留文件')
    context=group_context(root)
    if context and cfg['task']!=context['instruction']:raise ValueError('补录此小组需沿用其语言指令；录其他指令请选分组总文件夹')
    payload['dataset_spec']=validate_capture(cfg,root,payload.get('dataset_spec'),payload.get('append_confirmed'))
    payload['instruction_groups']=[];payload.pop('capture_targets',None);payload.pop('capture_collection',None)
    return None


def commit_groups(plan):
    if Path(plan['root']).is_symlink():raise ValueError('采集总文件夹已变化，拒绝符号链接')
    root=robot_path(plan['root'])
    if plan['existing']:read_collection(root)
    else:root.mkdir(exist_ok=False)
    atomic_json(root/MARKER,plan['marker'])
    for target in plan['targets']:
        child=robot_path(target['root'])
        if child.parent!=root:raise ValueError('小组目录不在采集总文件夹中')
        if not child.exists():
            child.mkdir();atomic_json(child/'.platform-plan.json',{'created':datetime.now().isoformat(),'recorded':False})
        store_metadata(child,target['spec'],source='instruction_group',lineage={'operation':'instruction_group','collection_root':str(root),'group_id':target['id'],'instruction':target['instruction']})


class RecordingLease:
    """Let the official recorder finalize each dataset without closing shared devices.

    The outer worker always closes the real objects in finally. connect is idempotent;
    this does not disable any alarm/boundary checks or torque-exit policy.
    """
    def __init__(self,device):self.device=device
    def __getattr__(self,name):return getattr(self.device,name)
    def connect(self,*args,**kwargs):
        if not self.device.is_connected:return self.device.connect(*args,**kwargs)
    def disconnect(self,*args,**kwargs):pass


class ListenerLease:
    """The outer worker owns and stops the shared Esc listener exactly once."""
    def __init__(self,listener):self.listener=listener
    def __getattr__(self,name):return getattr(self.listener,name)
    def stop(self):pass
