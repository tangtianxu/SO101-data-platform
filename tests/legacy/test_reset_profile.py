"""No physical IO: saved-start contracts and both reset entry-point rates."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import HERE,NAMES,environment,load_config,validate_config
environment()
from reset_profile import effective_speeds,inspect_pose,save_pose_copy
import reset_profile
sys.path.insert(0,load_config()['shared_scripts'])
import numpy as np
import so101_guarded_real_act as act
from so101_weighted_reset import SPEEDS,weighted_targets
from test_so101_guarded_real_replay import FakeBus,Clock


class ResetProfileTests(unittest.TestCase):
    def test_default_rates_exactly_match_existing_reset(self):
        np.testing.assert_array_equal(effective_speeds(load_config()),SPEEDS)

    def test_invalid_rates_or_joint_proportions_rejected(self):
        for value in (0,-1,True,float('nan'),float('inf')):
            with self.assertRaises(ValueError):validate_config({'reset_arm_speed_deg_s':value})
        cfg=load_config()
        for bad in ([],{},dict.fromkeys(NAMES,1),{**cfg['reset_joint_weights'],'wrist_roll':0}):
            with self.assertRaises(ValueError):validate_config({'reset_joint_weights':bad})

    def test_pose_is_bound_to_calibration_and_not_assumed_servo_zero(self):
        cfg=load_config();pose=inspect_pose(cfg)
        self.assertEqual(pose['source_episode_zero_based'],3)
        self.assertLess(pose['positions']['shoulder_lift'],-90)
        self.assertFalse(pose['collision_checked'])
        calibration=json.loads(Path(cfg['calibration']).read_text(encoding='utf-8'))
        target=act.load_reference_start(cfg['reference_start_pose'],calibration)
        np.testing.assert_array_equal(target,[pose['positions'][n] for n in NAMES])

    def test_copy_pose_preserves_original_and_rejects_bad_coordinates(self):
        cfg=load_config();original=Path(cfg['reference_start_pose']).read_bytes();pose=inspect_pose(cfg)
        with tempfile.TemporaryDirectory(dir=HERE/'state/tests') as folder,patch.object(reset_profile,'HERE',Path(folder)):
            new=save_pose_copy(cfg,{**pose['positions'],'shoulder_pan':1.0})
            self.assertNotEqual(new['path'],cfg['reference_start_pose'])
            self.assertEqual(new['positions']['shoulder_pan'],1.0)
            self.assertEqual(new['source'],'operator_ui_position_editor')
            self.assertIsNone(new['source_dataset'])
            self.assertEqual(Path(cfg['reference_start_pose']).read_bytes(),original)
            with self.assertRaises(ValueError):save_pose_copy(cfg,{**pose['positions'],'elbow_flex':1000})
            with self.assertRaises(ValueError):save_pose_copy(cfg,{**pose['positions'],'gripper':101})

    def test_copy_selected_file_preserves_precision_and_its_lineage(self):
        cfg=load_config();pose=inspect_pose(cfg)
        with tempfile.TemporaryDirectory(dir=HERE/'state/tests') as folder,patch.object(reset_profile,'HERE',Path(folder)):
            selected=save_pose_copy(cfg,{**pose['positions'],'shoulder_pan':1.25})
            selected_bytes=Path(selected['path']).read_bytes()
            copied=save_pose_copy(cfg,selected['positions'],selected['path'])
            saved=json.loads(Path(copied['path']).read_text(encoding='utf-8'))
            self.assertEqual(saved['previous_preset'],selected['path'])
            self.assertEqual(copied['positions'],selected['positions'])
            self.assertEqual(copied['positions']['shoulder_lift'],pose['positions']['shoulder_lift'])
            self.assertEqual(Path(selected['path']).read_bytes(),selected_bytes)

    def test_custom_interpolation_respects_rates_no_overshoot(self):
        start=np.zeros(6);target=np.array([2.,-1.,3.,1.,-2.,50.]);speeds=np.array([2.,1.,.5,.3,.2,20.])
        points=np.vstack([start,list(weighted_targets(start,target,speeds))])
        self.assertTrue(np.all(abs(np.diff(points,axis=0))<=speeds*act.PERIOD+1e-10))
        np.testing.assert_allclose(points[-1],target)
        self.assertTrue(np.all(points>=np.minimum(start,target)-1e-9))
        self.assertTrue(np.all(points<=np.maximum(start,target)+1e-9))

    def test_core_reset_custom_rates_fake_bus_only(self):
        from types import SimpleNamespace
        cfg=load_config();cal=json.loads(Path(cfg['calibration']).read_text(encoding='utf-8'))
        target=act.load_reference_start(cfg['reference_start_pose'],cal)
        bus=FakeBus('FAKE',{}, {n:SimpleNamespace(**cal[n]) for n in NAMES})
        bus.positions=dict(zip(NAMES,target.tolist()));bus.positions['shoulder_pan']+=.8
        clock=Clock();motion=act.Motion(bus,cal,threading.Event(),clock,soft_interlocks=False)
        act.guard_bus_writes(bus);motion.start_hold();speeds=[2.,1.6,1.2,.8,.4,5.];report={}
        with tempfile.TemporaryDirectory(dir=HERE/'state/tests') as folder:
            act.reset_to_reference(motion,target,Path(folder)/'reset.csv',report,speeds=speeds)
        self.assertEqual(list(report['speeds'].values()),speeds)
        self.assertEqual(report['outcome'],'reached_reference')
        self.assertFalse(report['collision_planning'])
        self.assertEqual(set(e[0] for e in bus.events),{'Goal_Position','Torque_Enable'})

    def test_invalid_core_rates_rejected_before_any_bus_read(self):
        for speeds in ([0]*6,[1]*5,[1]*5+[float('nan')],[999]*6):
            motion=Mock()
            with self.assertRaises(ValueError):act.reset_to_reference(motion,np.zeros(6),'unused',{},speeds=speeds)
            motion.bus.sync_read.assert_not_called()

    def test_act_launch_passes_same_configured_rates_without_hardware(self):
        import worker
        cfg=load_config();cfg['reset_arm_speed_deg_s']=5.
        with patch.object(act,'main',return_value={'outcome':'completed_timed_trial'}) as main:
            worker.run_act(cfg,{},Mock(),HERE/'state/tests')
        arguments=main.call_args.args[0];index=arguments.index('--reset-speeds')
        self.assertEqual(list(map(float,arguments[index+1:index+7])),effective_speeds(cfg))
        self.assertIn('--reset-before-act',arguments)


if __name__=='__main__':unittest.main(verbosity=2)
