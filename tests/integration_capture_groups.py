"""Optional backend integration: real LeRobot writers, fake devices, no cameras/USB."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import NAMES,ROBOT_HOME,load_config
AVAILABLE=all(importlib.util.find_spec(name) is not None for name in ('numpy','lerobot','scservo_sdk'))
if AVAILABLE:
    from data_tools import inspect_dataset
    from sensors import FeedbackSampler

@unittest.skipUnless(AVAILABLE,'Run with the configured LeRobot backend environment')
class RecordingIntegrationTests(unittest.TestCase):
    def setUp(self):
        (ROBOT_HOME/'temp').mkdir(parents=True,exist_ok=True)

    def sampler(self):
        from types import SimpleNamespace
        class Reader:
            def __init__(self,*args):self.data_dict={};self.calls=0
            def addParam(self,i):self.data_dict[i]=[0]*31;return True
            def txRxPacket(self):self.calls+=1;return 0
            def getData(self,i,address,length):return i if address!=40 else 1
        bus=SimpleNamespace(motors={n:SimpleNamespace(id=i+1,model='sts3215') for i,n in enumerate(NAMES)},port_handler=None,packet_handler=None)
        self.now=100.
        with patch('scservo_sdk.GroupSyncRead',Reader):return FeedbackSampler(bus,10,clock=lambda:self.now)

    def test_official_record_adapter_preserves_single_task_without_hardware(self):
        self._record_adapter(False)

    def test_group_recording_keeps_devices_connected_and_writes_separate_standard_datasets(self):
        self._record_adapter(True)

    def test_stop_between_groups_saves_first_group_without_starting_second(self):
        self._record_adapter(True,True)

    def _record_adapter(self,grouped,cancel=False):
        import numpy as np
        from types import SimpleNamespace
        import worker
        cfg=load_config();sys.path.insert(0,cfg['shared_scripts'])
        import so101_record_supplement as rec
        official=rec.official
        outer=self
        connections={'leader':0,'follower':0,'leader_close':0,'follower_close':0}
        class Leader:
            is_connected=False
            def __init__(self,*args):pass
            def connect(self):self.is_connected=True;connections['leader']+=1
            def disconnect(self):self.is_connected=False;connections['leader_close']+=1
            def get_action(self):return {n+'.pos':float(i) for i,n in enumerate(NAMES)}
        class Follower:
            name='so101_follower'
            is_connected=False
            cameras={}
            observation_features={n+'.pos':float for n in NAMES}
            action_features=observation_features
            def __init__(self,config,session):self.s=session;self.bus=None
            def connect(self):
                self.is_connected=True;self.s.sync_count+=1;connections['follower']+=1
                self.s.motion=SimpleNamespace(health_records=[])
            def disconnect(self):self.is_connected=False;connections['follower_close']+=1
            def get_observation(self):
                self.s.last_obs=dict.fromkeys(NAMES,0.);self.s.last_obs_time=1.;self.s.last_camera_times={}
                return dict.fromkeys((n+'.pos' for n in NAMES),0.)
            def send_action(self,action):
                target=np.array(list(action.values()),dtype=float);self.s.row(target,target,np.zeros(6,dtype=bool));return action
        def sample_factory(*args):return outer.sampler()
        def fake_loop(**kw):
            for _ in range(3):
                robot=kw['robot'];obs=robot.get_observation();action=kw['teleop'].get_action();robot.send_action(action)
                dataset=kw.get('dataset')
                if dataset is not None:
                    frame={**official.build_dataset_frame(dataset.features,obs,'observation'),
                           **official.build_dataset_frame(dataset.features,action,'action'),'task':kw['single_task']}
                    dataset.add_frame(frame)
        from unittest.mock import Mock
        events={'stop_recording':False,'exit_early':False,'rerecord_episode':False}
        with tempfile.TemporaryDirectory(dir=ROBOT_HOME/'temp') as folder:
            root=Path(folder)/'recorded';out=Path(folder)/'logs';out.mkdir()
            cfg.update(episodes=2,record_seconds=1,reset_seconds=1,task='把娃娃放进盒子')
            options={'dataset_root':str(root)};listener=Mock()
            if grouped:
                root.mkdir()
                options.update(capture_collection=str(root),capture_targets=[{'root':str(root/'g001'),'instruction':'把娃娃放进盒子','episodes':1},
                    {'root':str(root/'g002'),'instruction':'把布块放进盒子','episodes':1}])
            with patch.object(rec,'PreservedFollower',Follower),patch.object(rec,'PreservedLeader',Leader),\
                 patch.object(rec,'announce_phase'),patch.object(official,'record_loop',fake_loop),\
                 patch.object(official,'init_keyboard_listener',return_value=(listener,events)),\
                 patch.object(worker,'await_capture_group',side_effect=KeyboardInterrupt if cancel else None) as group_gate,\
                 patch.object(worker,'ports_for',return_value={'leader':'FAKE1','follower':'FAKE2'}),\
                 patch('sensors.FeedbackSampler',side_effect=sample_factory):
                if cancel:
                    with self.assertRaises(KeyboardInterrupt):worker.record_or_teleop(cfg,options,worker.Bridge(cfg),out,'record')
                else:result=worker.record_or_teleop(cfg,options,worker.Bridge(cfg),out,'record')
            group_gate.assert_called_once() if grouped else group_gate.assert_not_called()
            if cancel:
                self.assertEqual(inspect_dataset(root/'g001',0)['total_episodes'],1)
                self.assertFalse((root/'g002/meta/info.json').exists())
                self.assertEqual(connections,{'leader':1,'follower':1,'leader_close':1,'follower_close':1})
                listener.stop.assert_called_once();return
            data=inspect_dataset(root/'g002' if grouped else root,0 if grouped else 1)
            self.assertEqual(result['episodes_after'],2)
            self.assertEqual(result['startup_sync_count'],1)
            self.assertEqual(data['selected']['tasks'],['把布块放进盒子' if grouped else cfg['task']])
            self.assertEqual(len(data['trajectory']['state'][0]),6)
            logs_root=root/'g002' if grouped else root
            lines=[json.loads(line) for p in (logs_root/'extras/platform').glob('*.jsonl') for line in p.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(lines),3 if grouped else 6)
            self.assertEqual(lines[-1]['task'],'把布块放进盒子' if grouped else cfg['task'])
            self.assertEqual(lines[-1]['frame'],2)
            if grouped:
                self.assertEqual(inspect_dataset(root/'g001',0)['selected']['tasks'],['把娃娃放进盒子'])
                self.assertEqual(connections,{'leader':1,'follower':1,'leader_close':1,'follower_close':1})
                listener.stop.assert_called_once()


if __name__=='__main__':unittest.main()

