"""Offline additions: folder transactions, preserved task/state schema, sensor evidence."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import HERE, NAMES, atomic_json, environment, load_config
environment()
import dataset_library as dl
from data_tools import inspect_dataset
from sensors import FeedbackSampler, REGISTERS, features
from server import Platform


class LibraryTests(unittest.TestCase):
    def test_archive_restore_roundtrip_and_path_guards(self):
        with tempfile.TemporaryDirectory(dir=HERE/'state/tests') as temporary:
            base=Path(temporary); home=base/'datasets';home.mkdir();archive=base/'archive'
            cfg={**load_config(),'dataset_home':str(home)}
            result=dl.create_dataset(cfg,'temporary_dataset')
            root=Path(result['root']); original=(root/'.platform-plan.json').read_bytes()
            with patch.object(dl,'ARCHIVE',archive):
                with self.assertRaises(ValueError):dl.archive_dataset(cfg,home,home.name)
                with self.assertRaises(ValueError):dl.archive_dataset(cfg,root,'wrong')
                moved=dl.archive_dataset(cfg,root,root.name)
                self.assertFalse(root.exists());self.assertEqual((Path(moved['destination'])/'.platform-plan.json').read_bytes(),original)
                dl.restore_dataset(cfg,moved['manifest'])
                self.assertEqual((root/'.platform-plan.json').read_bytes(),original)
                with self.assertRaises(ValueError):dl.restore_dataset(cfg,moved['manifest'])

    def test_preserve_actual_training_and_new_thirty(self):
        cfg=load_config();protected=dl.protected_roots(cfg)
        self.assertIn(Path('E:/robot/datasets/so101_pick_place_clean_no_hand_20260925').resolve(),protected)
        self.assertIn(Path('E:/robot/datasets/so101_supplement_20261015').resolve(),protected)
        for root in protected:
            with self.assertRaises(PermissionError):dl.archive_dataset(cfg,root,root.name)

    def test_reject_invalid_names(self):
        cfg=load_config()
        for name in ('../bad','E:/bad','CON','LPT1','a/b',''):
            with self.assertRaises(ValueError):dl.create_dataset(cfg,name)

    def test_no_archive_during_running_session(self):
        app=Platform(offline=True)
        with patch.object(app,'active',return_value=True):
            with self.assertRaises(RuntimeError):app.manage_dataset({'action':'archive'})


class FeedbackTests(unittest.TestCase):
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

    def test_one_bus_transaction_and_no_missing_zero_fill(self):
        sample=self.sampler();frame=sample.frame()
        self.assertEqual(sample.reader.calls,1);self.assertEqual(frame['observation.sensors.current_raw'].tolist(),[1,2,3,4,5,6])
        self.now+=.02;sample.frame();self.assertEqual(sample.reader.calls,1)
        self.now+=.11;sample.frame();self.assertEqual(sample.reader.calls,2)
        sample.reader.data_dict[1]=[];self.now+=.2
        with self.assertRaises(ConnectionError):sample.frame()

    def test_communication_failure_not_silenced(self):
        sample=self.sampler();sample.reader.txRxPacket=lambda:-2
        from types import SimpleNamespace
        sample.bus.packet_handler=SimpleNamespace(getTxRxResult=lambda code:'failed')
        with self.assertRaises(ConnectionError):sample.sample()

    def test_standard_writer_accepts_sensor_schema_and_tasks(self):
        import numpy as np
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        with tempfile.TemporaryDirectory(dir=HERE/'state/tests') as folder:
            root=Path(folder)/'rich'
            schema={'observation.state':{'dtype':'float32','shape':(6,),'names':list(NAMES)},
                    'action':{'dtype':'float32','shape':(6,),'names':list(NAMES)},**features()}
            dataset=LeRobotDataset.create('local/offline_sensor_contract',fps=30,root=root,features=schema,use_videos=False)
            sampler=self.sampler()
            try:
                for instruction in ('把娃娃放进盒子','把布块放进盒子'):
                    for i in range(3):
                        self.now+=1/30
                        frame={'observation.state':np.zeros(6,dtype='float32'),'action':np.ones(6,dtype='float32'),
                               'task':instruction,**sampler.frame()}
                        dataset.add_frame(frame)
                    dataset.save_episode()
                dataset.finalize()
                data=inspect_dataset(root,1)
                self.assertEqual(data['total_episodes'],2)
                self.assertEqual(data['selected']['tasks'],['把布块放进盒子'])
                self.assertEqual(len(data['trajectory']['state'][0]),6)
                self.assertIn('observation.sensors.current_raw',{s['key'] for s in data['signals']})
            finally:dataset.finalize()

    def test_real_historical_health_is_recoverable_but_current_is_missing(self):
        data=inspect_dataset('E:/robot/datasets/so101_supplement_20261015',0)
        keys={s['key'] for s in data['signals']}
        self.assertIn('observation.sensors.temperature',keys)
        self.assertIn('observation.sensors.torque_enabled',keys)
        self.assertNotIn('observation.sensors.current_raw',keys)

    def test_official_record_adapter_preserves_per_episode_tasks_without_hardware(self):
        import numpy as np
        from types import SimpleNamespace
        import worker
        cfg=load_config();sys.path.insert(0,cfg['shared_scripts'])
        import so101_record_supplement as rec
        official=rec.official
        outer=self
        class Leader:
            is_connected=False
            def __init__(self,*args):pass
            def connect(self):self.is_connected=True
            def disconnect(self):self.is_connected=False
            def get_action(self):return {n+'.pos':float(i) for i,n in enumerate(NAMES)}
        class Follower:
            name='so101_follower'
            is_connected=False
            cameras={}
            observation_features={n+'.pos':float for n in NAMES}
            action_features=observation_features
            def __init__(self,config,session):self.s=session;self.bus=None
            def connect(self):
                self.is_connected=True;self.s.sync_count=1
                self.s.motion=SimpleNamespace(health_records=[])
            def disconnect(self):self.is_connected=False
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
        with tempfile.TemporaryDirectory(dir=HERE/'state/tests') as folder:
            root=Path(folder)/'recorded';out=Path(folder)/'logs';out.mkdir()
            cfg.update(episodes=2,record_seconds=1,reset_seconds=1)
            options={'dataset_root':str(root),'episode_tasks':['把娃娃放进盒子','把布块放进盒子']}
            with patch.object(rec,'PreservedFollower',Follower),patch.object(rec,'PreservedLeader',Leader),\
                 patch.object(rec,'announce_phase'),patch.object(official,'record_loop',fake_loop),\
                 patch.object(official,'init_keyboard_listener',return_value=(Mock(),events)),\
                 patch.object(worker,'ports_for',return_value={'leader':'FAKE1','follower':'FAKE2'}),\
                 patch('sensors.FeedbackSampler',side_effect=sample_factory):
                result=worker.record_or_teleop(cfg,options,worker.Bridge(cfg),out,'record')
            data=inspect_dataset(root,1)
            self.assertEqual(result['episodes_after'],2)
            self.assertEqual(data['selected']['tasks'],['把布块放进盒子'])
            self.assertEqual(len(data['trajectory']['state'][0]),6)
            lines=[json.loads(line) for p in (root/'extras/platform').glob('*.jsonl') for line in p.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(lines),6)
            self.assertEqual(lines[-1]['task'],'把布块放进盒子')
            self.assertEqual(lines[-1]['frame'],2)


if __name__=='__main__':unittest.main(verbosity=2)
