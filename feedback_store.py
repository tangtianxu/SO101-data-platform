"""ACT history/provenance. Stdlib only; no device, model, or video imports."""
from datetime import datetime
import json
import re
from pathlib import Path

from common import atomic_json, robot_path
from dataset_naming import filename_text


def allocate(cfg,job_id,log_root,options):
    home=robot_path(cfg['feedback_home']);home.mkdir(parents=True,exist_ok=True)
    task=str(options.get('task') or cfg['task']).strip()
    scene=str(options.get('group') or '固定位置').strip()
    name='act_'+datetime.now().strftime('%Y%m%d_%H%M')+'_'+filename_text(task,24)+'_'+filename_text(scene,18)
    root=home/name;sequence=1
    while root.exists():
        sequence+=1;root=home/(name+f'_{sequence:02d}')
    root.mkdir(exist_ok=False)
    value={'schema_version':1,'kind':'act_policy_rollout','id':job_id,'root':str(root),'log_root':str(log_root),
        'created':datetime.now().astimezone().isoformat(),'task':task,'description':scene,
        'checkpoint':cfg['checkpoint'],'calibration':cfg['calibration'],'reference_start_pose':cfg['reference_start_pose'],
        'dataset_root':str(root/'dataset'),'diagnostics':str(root/'diagnostics'),
        'state':'preparing','task_success':None,'result_source':'unreviewed','action_source':'policy',
        'expert_approved':False,'expert_corrections_recorded':False,'operator_takeover_supported':False,
        'reward_defined':False,'frames':0,'complete':False,'training_eligible':False,
        'configuration':cfg,'calibration_snapshot':json.loads(Path(cfg['calibration']).read_text(encoding='utf-8')),
        'start_pose_snapshot':json.loads(Path(cfg['reference_start_pose']).read_text(encoding='utf-8')),
        'checkpoint_files':{p.name:{'bytes':p.stat().st_size,'modified_ns':p.stat().st_mtime_ns} for p in Path(cfg['checkpoint']).iterdir() if p.is_file()},
        'terminal_boundary_annotated':False,'rewards_annotated':False,
        'data_contract':'One observation per sent action. Nominal timestamp is frame index / 30; actual observation/send/camera/feedback clocks are stored separately. No reward or expert correction is fabricated.'}
    atomic_json(root/'manifest.json',value)
    return str(root)


def manifest(cfg,root):
    root=robot_path(root,existing=True)
    if root.parent!=Path(cfg['feedback_home']).resolve() or not root.is_dir() or root.is_symlink():
        raise ValueError('请选择本平台数据回流目录中的独立 ACT 记录')
    value=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if value.get('kind')!='act_policy_rollout' or Path(value.get('root','')).resolve()!=root:
        raise ValueError('ACT 回流记录身份不一致')
    if Path(value['dataset_root']).resolve()!=root/'dataset' or Path(value['diagnostics']).resolve()!=root/'diagnostics':
        raise ValueError('回流记录路径异常')
    return root,value


def update(root,**values):
    root=robot_path(root,existing=True)
    item=json.loads((root/'manifest.json').read_text(encoding='utf-8'));item.update(values)
    item['updated']=datetime.now().astimezone().isoformat()
    atomic_json(root/'manifest.json',item)
    return item


def label(cfg,root,success,note='',failure_reason=''):
    if success is not True and success is not False and success is not None:raise ValueError('结果只能是成功、失败或未判断')
    path,item=manifest(cfg,root)
    if item['state'] in ('preparing','recording','finalizing'):raise ValueError('请等待本次 ACT 保存完成后再标记结果')
    if not isinstance(note,str) or len(note)>1000 or not isinstance(failure_reason,str) or len(failure_reason)>200:raise ValueError('结果备注过长')
    history=item.get('result_history',[])
    history.append({'at':datetime.now().astimezone().isoformat(),'success':success,'note':note,'failure_reason':failure_reason})
    return update(path,task_success=success,result_source='operator_ui',note=note,failure_reason=failure_reason,
        result_history=history[-100:],expert_approved=False,training_eligible=False)


def history(cfg):
    result=[];home=Path(cfg['feedback_home']);seen=set()
    if home.is_dir():
        for root in sorted(home.iterdir(),reverse=True):
            if not (root/'manifest.json').is_file():continue
            try:
                _,item=manifest(cfg,root);seen.add(item['id'])
                item['has_dataset']=(root/'dataset/meta/info.json').is_file() and item.get('frames',0)>0
                for key in ('configuration','calibration_snapshot','start_pose_snapshot','checkpoint_files'):item.pop(key,None)
                result.append(item)
            except (OSError,ValueError,KeyError):continue
    # Existing platform trials remain visible; do not pretend old missing sensors exist.
    for root in sorted(Path(cfg['log_home']).glob('*'),reverse=True)[:500]:
        if root.name in seen or not (root/'session.json').is_file():continue
        try:
            session=json.loads((root/'session.json').read_text(encoding='utf-8'))
            if session.get('kind')!='act':continue
            report=root/'act/report.json';operator=root/'act/operator_result.json'
            outcome=json.loads(report.read_text(encoding='utf-8')) if report.is_file() else {}
            label_value=json.loads(operator.read_text(encoding='utf-8')) if operator.is_file() else {}
            result.append({'id':root.name,'root':str(root),'legacy':True,'created':session.get('created',root.name),
                'task':session['config'].get('task',''),'description':'历史日志（未回填缺失传感器）',
                'checkpoint':session['config'].get('checkpoint'),'state':outcome.get('outcome','无完整报告'),
                'task_success':label_value.get('task_success'),'has_dataset':False,'frames':outcome.get('model_steps',0),
                'expert_approved':False,'training_eligible':False})
        except (OSError,ValueError,KeyError):continue
    return sorted(result,key=lambda r:re.sub(r'\D','',r['created'])[:14],reverse=True)[:200]
