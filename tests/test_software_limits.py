"""Software limits and diagnostics tested without hardware or user config changes."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import HERE,NAMES,load_config,validate_config
import joint_control
import software_limits as limits
from server import Platform


class SoftwareLimitsTests(unittest.TestCase):
    def setUp(self):
        self.cfg=load_config()
        self.cal_path=Path(self.cfg['calibration'])
        self.cal_bytes=self.cal_path.read_bytes()
        self.cal=json.loads(self.cal_bytes)
        self.baseline=limits.calibrated(self.cal)
        self.values={n:{'min':b[0],'max':b[1]} for n,b in self.baseline.items()}

    def request(self,values=None):
        return {'confirmed':True,'calibration_id':limits.digest(self.cal),
                'limits_id':limits.identity(limits.effective(self.cfg,self.cal),self.cal),
                'limits':self.values if values is None else values}

    def test_default_bounds_preserve_exact_original_coordinates(self):
        self.assertEqual(limits.effective({**self.cfg,'manual_joint_limits':{}},self.cal),self.baseline)
        self.assertEqual(limits.digest(self.cal),joint_control.calibration_id(self.cal))
        profile=joint_control.profile(self.cfg)
        self.assertEqual(profile['scope'],'basic_control_only')
        self.assertEqual(profile['joints'][2]['calibrated_max'],(2976-991)*180/4095)

    def test_tightening_and_restore_do_not_change_calibration(self):
        values=copy.deepcopy(self.values);values['elbow_flex']['max']=80.
        cfg=validate_config(limits.settings_for_request(self.cfg,self.cal,self.request(values)))
        self.assertEqual(limits.effective(cfg,self.cal)['elbow_flex'][1],80.)
        self.assertEqual(self.cal_path.read_bytes(),self.cal_bytes)
        self.assertEqual(joint_control.profile(cfg)['joints'][2]['calibrated_max'],self.baseline['elbow_flex'][1])

    def test_widening_nan_boolean_missing_or_reversed_rejected(self):
        for name,field,value in [('elbow_flex','max',90),('shoulder_pan','min',-100),
                                 ('gripper','max',101),('gripper','min',-1),
                                 ('shoulder_pan','min',float('nan')),('shoulder_pan','max',True),
                                 ('elbow_flex','max',-90)]:
            data=copy.deepcopy(self.values);data[name][field]=value
            with self.subTest(name=name,field=field,value=value):
                with self.assertRaises(ValueError):limits.checked(data,self.cal)
        with self.assertRaises(ValueError):limits.checked({},self.cal)

    def test_confirm_calibration_and_limits_revision_required(self):
        for field,value in [('confirmed',False),('calibration_id','old'),('limits_id','old')]:
            with self.assertRaises(ValueError):limits.settings_for_request(self.cfg,self.cal,{**self.request(),field:value})
        cfg=limits.settings_for_request(self.cfg,self.cal,self.request())
        changed=copy.deepcopy(self.cal);changed['elbow_flex']['range_min']+=1
        with self.assertRaises(ValueError):limits.effective(cfg,changed)

    def test_guard_rejects_before_send_and_preserves_send_keywords(self):
        motion=Mock();send=motion.send
        bounds=copy.deepcopy(self.baseline);bounds['elbow_flex']=(-80.,80.)
        limits.guard_motion(motion,bounds)
        with self.assertRaises(ValueError):motion.send([0,0,81,0,0,50])
        send.assert_not_called()
        goal=[0,0,79,0,0,50];motion.send(goal,source_time=123)
        send.assert_called_once_with(goal,source_time=123)

    def test_selected_joint_request_checks_software_bounds_before_io(self):
        values=copy.deepcopy(self.values);values['elbow_flex']['max']=80.
        cfg=limits.settings_for_request(self.cfg,self.cal,self.request(values))
        request={'calibration_id':limits.digest(self.cal),'limits_id':joint_control.profile(cfg)['limits_id'],
                 'joints':{'elbow_flex':{'target':81.,'speed':1.}}}
        motion=Mock()
        with self.assertRaises(ValueError):joint_control.execute(motion,self.cal,request,HERE/'state/tests',Mock(),cfg)
        motion.bus.sync_read.assert_not_called();motion.send.assert_not_called()
        request['joints']['elbow_flex']['target']=79.
        self.assertEqual(joint_control.validate_request(request,self.cal,cfg)['elbow_flex']['target'],79.)
        with self.assertRaises(ValueError):joint_control.validate_request({**request,'limits_id':'old'},self.cal,cfg)

    def test_save_is_offline_only_configuration_and_backup(self):
        (HERE/'state/tests').mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=HERE/'state/tests') as folder:
            app=Platform(offline=True)
            with patch('server.HERE',Path(folder)),patch('server.subprocess.Popen') as spawn:
                result=app.save_manual_limits(self.request())
                spawn.assert_not_called()
            self.assertFalse(result['hardware_accessed']);self.assertFalse(app.active())
            saved=json.loads((Path(folder)/'state/settings.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['manual_limits_calibration_id'],limits.digest(self.cal))
            self.assertEqual(len(list((Path(folder)/'state/manual_limits_history').glob('*.json'))),1)
        self.assertEqual(self.cal_path.read_bytes(),self.cal_bytes)

    def test_main_active_blocks_save_but_camera_only_does_not(self):
        app=Platform(offline=True);app.proc=Mock();app.proc.poll.return_value=None
        with patch('server.atomic_json') as write:
            with self.assertRaises(RuntimeError):app.save_manual_limits(self.request())
            write.assert_not_called()
        app.proc=None;app.preview_proc=Mock();app.preview_proc.poll.return_value=None
        with patch('server.atomic_json') as write:
            result=app.save_manual_limits(self.request())
            self.assertFalse(result['hardware_accessed']);self.assertEqual(write.call_count,2)

    def test_preflight_diagnostic_names_outside_joint_without_writes(self):
        sys.path.insert(0,self.cfg['shared_scripts'])
        import so101_guarded_real_act as safety
        raw={n:(c['range_min']+c['range_max'])//2 for n,c in self.cal.items()}
        raw['elbow_flex']=self.cal['elbow_flex']['range_max']+2
        def read(register,**_):
            fields={'Homing_Offset':'homing_offset','Min_Position_Limit':'range_min','Max_Position_Limit':'range_max'}
            if register in fields:return {n:c[fields[register]] for n,c in self.cal.items()}
            if register=='Present_Position':return raw
            return dict.fromkeys(NAMES,35 if register=='Present_Temperature' else 0)
        bus=Mock();bus.sync_read.side_effect=read
        with self.assertRaisesRegex(RuntimeError,r'elbow_flex: raw=2978, allowed=\[991, 2976\]'):
            safety.preflight(bus,self.cal)
        bus.write.assert_not_called();bus.sync_write.assert_not_called()


if __name__=='__main__':unittest.main()
