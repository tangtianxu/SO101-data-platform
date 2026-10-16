"""Explicit joint targets. Validation is pure; execution reuses the existing motion owner."""
import csv
import hashlib
import json
import math
from pathlib import Path
import time

from common import NAMES,atomic_json,number,read_calibration
from reset_profile import effective_speeds
import software_limits


def calibration_id(calibration):
    return hashlib.sha256(json.dumps(calibration,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def limits(calibration):
    return software_limits.calibrated(calibration)


def profile(cfg):
    calibration=read_calibration(cfg['calibration'])
    bounds=software_limits.effective(cfg,calibration);baseline=limits(calibration);rates=effective_speeds(cfg)
    return {'calibration_id':calibration_id(calibration),'calibration_path':cfg['calibration'],
        'limits_id':software_limits.identity(bounds,calibration),'scope':'basic_control_only',
        'joints':[{'name':name,'min':bounds[name][0],'max':bounds[name][1],
            'calibrated_min':baseline[name][0],'calibrated_max':baseline[name][1],
            'speed_min':.1,'speed_max':100. if name=='gripper' else 20.,'default_speed':rates[i],
            'unit':'%' if name=='gripper' else '°','speed_unit':'百分点/秒' if name=='gripper' else '°/秒'} for i,name in enumerate(NAMES)],
        'speed_semantics':'应用层目标插值速度，不是 FD 原始速度寄存器，不保证实测速度或无碰撞',
        'arrival_tolerance':[1.]*5+[2.],'unselected':'保持执行开始时的实测位置','changes_servo_parameters':False}


def validate_request(request,calibration,cfg=None):
    if request.get('calibration_id')!=calibration_id(calibration):raise ValueError('标定已变化，请刷新关节控制面板，不发送旧坐标目标')
    joints=request.get('joints')
    if not isinstance(joints,dict) or not joints or set(joints)-set(NAMES):raise ValueError('请勾选至少一个有效关节')
    bounds=limits(calibration) if cfg is None else software_limits.effective(cfg,calibration)
    if cfg is not None and request.get('limits_id')!=software_limits.identity(bounds,calibration):
        raise ValueError('软件限位已变化，请刷新关节控制面板')
    checked={}
    for name,item in joints.items():
        if not isinstance(item,dict) or set(item)!={'target','speed'}:raise ValueError('每个选中关节必须有目标位置和速度')
        if isinstance(item['target'],str) and not item['target'].strip():raise ValueError('目标位置不能为空')
        checked[name]={'target':number(item['target'],*bounds[name],name+' 目标'),
            'speed':number(item['speed'],.1,100. if name=='gripper' else 20.,name+' 目标速度')}
    return checked


def execute(motion,calibration,request,out,emit,cfg=None):
    """Same bus thread, unchanged limits/PID/firmware, independent selected-joint rates."""
    import numpy as np
    from so101_weighted_reset import weighted_targets
    selected=validate_request(request,calibration,cfg)
    if not motion.enabled:raise ValueError('请先开启当前位置保持，再执行目标；不会自动开启力矩')
    current=motion.bus.sync_read('Present_Position',num_retry=1)
    start=np.array([current[n] for n in NAMES],dtype=float);target=start.copy();rates=np.ones(6)
    for i,name in enumerate(NAMES):
        if name in selected:target[i]=selected[name]['target'];rates[i]=selected[name]['speed']
    bounds=limits(calibration) if cfg is None else software_limits.effective(cfg,calibration)
    if not np.isfinite(start).all() or any(not bounds[n][0]<=start[i]<=bounds[n][1] for i,n in enumerate(NAMES)):
        raise ValueError('当前反馈不在已核对的关节范围内，未下发新目标')
    estimated=float(np.max(abs(target-start)/rates))
    report={'operation':'selected_joint_targets','selected':selected,'start':dict(zip(NAMES,start.tolist())),
        'target':dict(zip(NAMES,target.tolist())),'unselected_hold_measured':True,'estimated_s':estimated,
        'calibration_id':calibration_id(calibration),'servo_parameters_changed':False,'collision_planned':False,
        'outcome':'interrupted','arrival_tolerance':[1.]*5+[2.]}
    started=motion.clock.perf_counter();last_notice=-float('inf');residual=None
    stem=Path(out)/('joint_targets_'+str(time.time_ns()))
    emit('stage',stage='joint_moving',message='执行勾选关节；未选关节保持起始实测位置。停止或 Esc 可中止。',target_pending=True)
    try:
        with stem.with_suffix('.csv').open('x',encoding='utf-8',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(['phase','elapsed_s',*[f'goal_{n}' for n in NAMES],*[f'state_{n}' for n in NAMES]])
            def sample(phase,goal):
                nonlocal last_notice
                actual=motion.bus.sync_read('Present_Position',num_retry=1)
                values=np.array([actual[n] for n in NAMES]);now=motion.clock.perf_counter()
                writer.writerow([phase,now-started,*goal,*values])
                if now-last_notice>=.1:
                    last_notice=now;emit('telemetry',state=values.tolist(),target=goal.tolist(),
                        target_elapsed_s=now-started,health=motion.health_records[-1] if motion.health_records else None)
                return values
            for goal in weighted_targets(start,target,rates):
                motion.send(goal);sample('moving',goal)
            deadline=motion.clock.perf_counter()+2.
            selected_mask=np.array([n in selected for n in NAMES]);tolerance=np.array(report['arrival_tolerance'])
            while True:
                measured=sample('arrival_check',target);residual=measured-target
                if np.all(abs(residual[selected_mask])<=tolerance[selected_mask]):
                    report['outcome']='reached';break
                if motion.clock.perf_counter()>=deadline:
                    report['outcome']='target_sent_not_reached';break
                motion.send(target)
        report['final_measured']=dict(zip(NAMES,measured.tolist()))
        report['residual']=dict(zip(NAMES,residual.tolist()))
        return report
    finally:
        report['elapsed_s']=motion.clock.perf_counter()-started
        atomic_json(stem.with_suffix('.json'),report)
