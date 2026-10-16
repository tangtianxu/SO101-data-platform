"""Application-only manual-control bounds; never writes calibration or devices."""
import hashlib
import json

from common import NAMES, number


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def calibrated(calibration):
    result={}
    for name in NAMES:
        c=calibration[name]
        span=number((c['range_max']-c['range_min'])*180/4095,.0001,360,name+' 标定跨度')
        result[name]=(0.,100.) if name=='gripper' else (-span,span)
    return result


def checked(value,calibration):
    if not isinstance(value,dict) or set(value)!=set(NAMES):
        raise ValueError('软件限位必须包含全部六个关节')
    baseline=calibrated(calibration);result={}
    for name in NAMES:
        item=value[name]
        if not isinstance(item,dict) or set(item)!={'min','max'}:
            raise ValueError(name+' 软件限位需要下限和上限')
        low=number(item['min'],*baseline[name],name+' 软件下限')
        high=number(item['max'],*baseline[name],name+' 软件上限')
        if low>=high:raise ValueError(name+' 软件下限必须小于上限')
        result[name]=(low,high)
    return result


def effective(cfg,calibration):
    value=cfg.get('manual_joint_limits',{})
    if value=={}:return calibrated(calibration)
    if cfg.get('manual_limits_calibration_id')!=digest(calibration):
        raise ValueError('软件限位绑定的标定已变化，请重新确认限位；没有更改坐标或设备')
    return checked(value,calibration)


def identity(bounds,calibration):
    return digest({'calibration':digest(calibration),'bounds':bounds,'scope':'basic_control_only'})


def check_positions(values,bounds,label='目标'):
    if len(values)!=6:raise ValueError(label+'必须包含六个关节')
    for i,name in enumerate(NAMES):
        number(values[i],*bounds[name],name+' '+label+'（软件限位）')


def guard_motion(motion,bounds):
    """Only manual send() targets. Explicit hold/release retain their existing semantics."""
    send=motion.send
    def restricted(target,*args,**kwargs):
        check_positions(target,bounds)
        return send(target,*args,**kwargs)
    motion.send=restricted


def settings_for_request(cfg,calibration,request):
    if request.get('confirmed') is not True:raise ValueError('请确认仅保存软件限位，不更改舵机或标定')
    if request.get('calibration_id')!=digest(calibration):raise ValueError('标定已变化，请刷新面板')
    if request.get('limits_id')!=identity(effective(cfg,calibration),calibration):
        raise ValueError('软件限位已变化，请刷新后再保存')
    bounds=checked(request.get('limits'),calibration)
    return {**cfg,'manual_joint_limits':{n:{'min':v[0],'max':v[1]} for n,v in bounds.items()},
            'manual_limits_calibration_id':digest(calibration)}
