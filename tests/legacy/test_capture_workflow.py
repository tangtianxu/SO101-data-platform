"""Simplified naming, actual create/append contracts, and policy provenance; no devices."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import HERE,NAMES,load_config
from dataset_library import create_dataset,list_datasets
import dataset_naming as naming
import feedback_store as feedback


class CaptureWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=HERE/'state/tests');self.base=Path(self.temp.name)
        self.cfg={**load_config(),'dataset_home':str(self.base/'datasets'),'log_home':str(self.base/'logs'),'feedback_home':str(self.base/'data_feedback')}
        Path(self.cfg['dataset_home']).mkdir();Path(self.cfg['log_home']).mkdir()
        self.registry=patch.object(naming,'REGISTRY',self.base/'registry.json');self.registry.start()
    def tearDown(self):self.registry.stop();self.temp.cleanup()

    def test_preview_reads_system_minute_contains_language_and_creates_nothing(self):
        plan=naming.capture_plan(self.cfg,{'instruction':'把娃娃放进盒子','description':'娃娃位置变化','started_at':'1900-01-01T00:00'})
        self.assertIn(time.strftime('%Y%m%d_%H%M'),plan['name'])
        self.assertIn('把娃娃放进盒子_娃娃位置变化',plan['name'])
        self.assertNotIn('_b00',plan['name']);self.assertFalse(Path(plan['root']).exists())
        self.assertEqual(list(Path(self.cfg['dataset_home']).iterdir()),[])

    def test_same_minute_collision_is_automatic_not_user_batch(self):
        one=naming.capture_plan(self.cfg,{});create_dataset(self.cfg,one['name']);naming.store_metadata(one['root'],one['spec'])
        two=naming.capture_plan(self.cfg,{})
        self.assertNotEqual(one['root'],two['root']);self.assertTrue(two['name'].endswith('_02'))

    def test_filename_sanitized_but_original_language_preserved(self):
        text='抓娃娃 / 放入盒子：小心*?'
        plan=naming.capture_plan(self.cfg,{'instruction':text,'description':'盒子<左移>'})
        self.assertEqual(plan['spec']['instruction'],text)
        self.assertFalse(any(c in plan['name'] for c in '\\/:*?<>|'))
        self.assertEqual(Path(plan['root']).parent,Path(self.cfg['dataset_home']))

    def test_parent_cannot_be_a_dataset_or_its_internals(self):
        one=naming.capture_plan(self.cfg,{});create_dataset(self.cfg,one['name']);naming.store_metadata(one['root'],one['spec'])
        for parent in (one['root'],'C:/Windows'):
            with self.assertRaises(ValueError):naming.capture_plan(self.cfg,{'parent':parent})

    def test_append_raw_downgrades_review_but_not_source_bytes(self):
        one=naming.capture_plan(self.cfg,{});create_dataset(self.cfg,one['name'])
        naming.store_metadata(one['root'],{**one['spec'],'review':'train'},review_confirmed=True)
        original=(Path(one['root'])/'.platform-plan.json').read_bytes()
        spec=naming.validate_capture(self.cfg,one['root'],None,True)
        self.assertEqual(spec['review'],'pending')
        self.assertEqual((Path(one['root'])/'.platform-plan.json').read_bytes(),original)

    def test_prepare_creates_before_spawning_and_uses_selected_parent(self):
        from server import Platform
        app=Platform(offline=False);app.config=self.cfg
        parent=self.base/'other_datasets';parent.mkdir()
        fake=Mock();fake.pid=123;fake.poll.return_value=0
        def spawn(*args,**kwargs):
            self.assertEqual(len(list(parent.iterdir())),1)
            self.assertTrue((next(parent.iterdir())/'.platform-plan.json').is_file())
            return fake
        with patch('server.subprocess.Popen',side_effect=spawn),patch('server.threading.Thread'):
            job=app.start({'kind':'record','options':{'capture_mode':'new','parent':str(parent),'task':'抓娃娃','group':'盒子位置变化','episodes':1}})
        self.assertTrue(Path(job['dataset_root']).is_dir())
        self.assertEqual(Path(job['dataset_root']).parent,parent)
        self.assertIn(job['dataset_root'],[v['root'] for v in list_datasets(self.cfg)])

    def test_history_result_never_approves_expert_data(self):
        root=feedback.allocate(self.cfg,'run1',self.base/'logs/run1',{'task':'抓娃娃','group':'娃娃位置变化'})
        self.assertEqual(Path(root).parent,Path(self.cfg['feedback_home']))
        with self.assertRaises(ValueError):feedback.label(self.cfg,root,True)
        feedback.update(root,state='finished',frames=3,complete=True)
        for success in (True,False,None):
            item=feedback.label(self.cfg,root,success,'人工检查')
            self.assertEqual(item['task_success'],success);self.assertFalse(item['expert_approved']);self.assertFalse(item['training_eligible'])
        self.assertEqual(len(feedback.history(self.cfg)),1)
        self.assertIn('calibration_snapshot',json.loads((Path(root)/'manifest.json').read_text(encoding='utf-8')))
        with self.assertRaises(ValueError):feedback.label(self.cfg,self.base,False)

    def test_policy_writer_preserves_sent_action_and_samples_without_hardware(self):
        import numpy as np
        from policy_capture import PolicyCapture
        from sensors import features
        root=feedback.allocate(self.cfg,'run2',self.base/'logs/run2',{})
        writer=Mock();sampler=Mock();sampler.latest={'perf_time':2.,'health':{}}
        sampler.frame.return_value={key:np.ones(spec['shape'],dtype=spec['dtype']) for key,spec in features().items()}
        capture=PolicyCapture(root,self.cfg,factory=lambda target:writer)
        with patch('sensors.FeedbackSampler',return_value=sampler):capture.prepare(Mock())
        for i in range(3):
            capture.capture(bus=None,state=np.arange(6),candidate=np.arange(6)+2,target=np.arange(6)+1,
                clipped=np.zeros(6),frames={n:np.zeros((480,640,3),dtype='uint8') for n in ('top','wrist')},
                stamps={'top':1.,'wrist':1.},observation_time=1.,send_time=2.+i/30)
        report=capture.finish({'outcome':'completed_timed_trial','trial_elapsed_s':.1})
        self.assertTrue(report['complete']);self.assertEqual(report['frames'],3);self.assertEqual(writer.add_frame.call_count,3)
        first=writer.add_frame.call_args_list[0].args[0]
        np.testing.assert_array_equal(first['action'],np.arange(6)+1)
        np.testing.assert_array_equal(first['observation.rollout.policy_requested'],np.arange(6)+2)
        self.assertIn('observation.sensors.velocity_raw',first);writer.save_episode.assert_called_once()
        rows=[json.loads(line) for line in (Path(root)/'steps.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(rows),3);self.assertIsNone(rows[0]['expert_action'])

    def test_recording_overflow_is_not_silently_dropped(self):
        import numpy as np
        from policy_capture import PolicyCapture
        root=feedback.allocate(self.cfg,'run3',self.base/'logs/run3',{})
        capture=PolicyCapture(root,self.cfg);capture.feedback=Mock();capture.feedback.frame.return_value={};capture.feedback.latest={}
        for i in range(8):capture.queue.put(({},{}))
        with self.assertRaises(BufferError):capture.capture(bus=None,state=np.zeros(6),candidate=np.zeros(6),target=np.zeros(6),clipped=np.zeros(6),frames={},stamps={'top':0,'wrist':0},observation_time=0,send_time=1)


if __name__=='__main__':unittest.main(verbosity=2)
