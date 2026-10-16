"""Selected-joint motion tested against fake motors/clock only."""
import csv
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import HERE,NAMES,load_config
import joint_control as joint
sys.path.insert(0,load_config()['shared_scripts'])
import so101_guarded_real_act as safety
from test_so101_guarded_real_replay import FakeBus,Clock


class JointControlTests(unittest.TestCase):
    def setUp(self):
        self.cfg=load_config();self.cal=json.loads(Path(self.cfg['calibration']).read_text(encoding='utf-8'))
        self.request={'calibration_id':joint.calibration_id(self.cal),'joints':{'shoulder_pan':{'target':2.,'speed':4.},'gripper':{'target':15.,'speed':10.}}}
    def make_motion(self):
        bus=FakeBus('FAKE',{}, {n:SimpleNamespace(**self.cal[n]) for n in NAMES});bus.positions=dict.fromkeys(NAMES,0.);bus.positions['gripper']=10.
        motion=safety.Motion(bus,self.cal,threading.Event(),Clock(),soft_interlocks=False,
            temperature_confirmation_s=1.,allow_gripper_overload=True)
        safety.guard_bus_writes(bus);motion.start_hold();bus.events.clear();return motion

    def test_profile_is_calibrated_and_uses_explicit_units(self):
        p=joint.profile(self.cfg)
        self.assertEqual(len(p['joints']),6);self.assertEqual(p['joints'][-1]['unit'],'%')
        self.assertAlmostEqual(p['joints'][2]['max'],(2976-991)*180/4095)
        self.assertFalse(p['changes_servo_parameters'])

    def test_empty_unknown_nan_speed_and_stale_calibration_rejected_before_io(self):
        invalid=[{**self.request,'joints':{}},{**self.request,'calibration_id':'old'},
            {**self.request,'joints':{'unknown':{'target':0,'speed':1}}}]
        for field,value in [('target',float('nan')),('target',True),('target',999),('target',''),('speed',0),('speed',21)]:
            invalid.append({**self.request,'joints':{'shoulder_pan':{'target':0,'speed':1,field:value}}})
        for request in invalid:
            motion=Mock()
            with self.assertRaises(ValueError):joint.execute(motion,self.cal,request,HERE/'state/tests',Mock())
            motion.bus.sync_read.assert_not_called();motion.send.assert_not_called()

    def test_single_and_multiple_selection_keep_unselected_fixed_and_rate_bounded(self):
        for names in (('shoulder_pan',),('shoulder_pan','gripper')):
            motion=self.make_motion();request={**self.request,'joints':{n:self.request['joints'][n] for n in names}}
            with tempfile.TemporaryDirectory(dir=HERE/'state/tests') as folder:
                result=joint.execute(motion,self.cal,request,folder,Mock())
                with next(Path(folder).glob('*.csv')).open(encoding='utf-8') as stream:rows=list(csv.DictReader(stream))
            self.assertEqual(result['outcome'],'reached')
            for name in NAMES:
                goals=[float(row['goal_'+name]) for row in rows]
                if name not in names:self.assertEqual(len(set(goals)),1)
                else:self.assertTrue(all(abs(b-a)<=request['joints'][name]['speed']/30+1e-8 for a,b in zip(goals,goals[1:])))
            self.assertEqual({r for r,_ in motion.bus.events},{'Goal_Position'})
            self.assertTrue(all(v==1 for v in motion.bus.torque.values()))

    def test_no_automatic_torque_enable(self):
        motion=Mock();motion.enabled=False
        with self.assertRaises(ValueError):joint.execute(motion,self.cal,self.request,HERE/'state/tests',Mock())
        motion.bus.sync_read.assert_not_called();motion.start_hold.assert_not_called()

    def test_position_stall_reports_not_reached_instead_of_claiming_success(self):
        motion=self.make_motion();original=motion.bus.sync_write
        def stalled(register,*args,**kwargs):
            before=motion.bus.positions.copy();original(register,*args,**kwargs);motion.bus.positions=before
        motion.bus.sync_write=stalled
        with tempfile.TemporaryDirectory(dir=HERE/'state/tests') as folder:result=joint.execute(motion,self.cal,self.request,folder,Mock())
        self.assertEqual(result['outcome'],'target_sent_not_reached')

    def test_stop_interrupts_targets_and_exit_hold_never_releases(self):
        motion=self.make_motion();send=motion.send;calls=[]
        def stop_after_three(goal):
            result=send(goal);calls.append(result)
            if len(calls)==3:motion.stop.set()
            return result
        motion.send=stop_after_three
        with tempfile.TemporaryDirectory(dir=HERE/'state/tests') as folder:
            with self.assertRaises(KeyboardInterrupt):joint.execute(motion,self.cal,self.request,folder,Mock())
            report=json.loads(next(Path(folder).glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(report['outcome'],'interrupted');self.assertEqual(len(calls),3)
        motion.hold_on_exit();self.assertTrue(all(v==1 for v in motion.bus.torque.values()))

    def test_server_rejects_parallel_modes_and_repeated_pending_target(self):
        from server import Platform
        app=Platform(offline=False);app.proc=Mock();app.proc.poll.return_value=None
        request={**self.request,'limits_id':joint.profile(app.config)['limits_id'],'command':'joint_target','confirmed':True,'job_id':'job'}
        for kind,phase,pending in [('record','CONTROL',False),('act','RUNNING',False),('control','JOINT_MOVING',True)]:
            app.job={'id':'job','kind':kind,'phase':phase,'target_pending':pending}
            with self.assertRaises(ValueError):app.command(request)
        app.job={'id':'job','kind':'control','phase':'CONTROL'};app.command(request)
        self.assertTrue(app.job['target_pending'])
        with self.assertRaises(ValueError):app.command(request)
        self.assertEqual(app.proc.stdin.write.call_count,1)


if __name__=='__main__':unittest.main(verbosity=2)
