"""Read-only planning and temporary group metadata only; no devices or source data."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import ROBOT_HOME,load_config
import dataset_naming as naming
from dataset_library import list_datasets
import capture_groups as groups


class CaptureGroupTests(unittest.TestCase):
    def setUp(self):
        temp=ROBOT_HOME/'temp';temp.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=temp);self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        self.cfg={**load_config(),'dataset_home':str(self.base/'datasets')};Path(self.cfg['dataset_home']).mkdir()
        p=patch.object(naming,'REGISTRY',self.base/'registry.json');p.start();self.addCleanup(p.stop)

    def plan(self,rows,**extra):
        cfg=self.cfg.copy();payload={'capture_mode':'new','task':'默认指令','episodes':5,'instruction_groups':rows,**extra}
        return groups.prepare_record(cfg,payload),cfg,payload

    def test_blank_is_unchanged_single_dataset_and_no_subfolders(self):
        plan,cfg,payload=self.plan([{'instruction':'  ','episodes':5}])
        self.assertIsNone(plan);self.assertEqual(cfg['episodes'],5);self.assertEqual(payload['instruction_groups'],[])
        self.assertNotIn('capture_targets',payload);self.assertFalse(Path(payload['dataset_root']).exists())

    def test_repeated_instruction_merged_and_order_preserved(self):
        self.assertEqual(groups.normalize_groups([{'instruction':'A','episodes':2},{'instruction':'B','episodes':3},{'instruction':' A ','episodes':4}]),
            [{'instruction':'A','episodes':6},{'instruction':'B','episodes':3}])

    def test_grouped_capture_uses_group_tasks_even_when_default_is_blank(self):
        plan,cfg,_=self.plan([{'instruction':'A','episodes':2}],task='')
        self.assertEqual(cfg['task'],'A');self.assertEqual(plan['targets'][0]['instruction'],'A')

    def test_invalid_counts_and_legacy_episode_overrides_fail_before_writing(self):
        for count in (0,31,True,1.2,None):
            with self.subTest(count=count),self.assertRaises(ValueError):self.plan([{'instruction':'A','episodes':count}])
        with self.assertRaises(ValueError):self.plan([{'instruction':'A','episodes':20},{'instruction':'B','episodes':20}])
        with self.assertRaises(ValueError):self.plan([],episode_tasks=['A'])
        self.assertEqual(list(Path(self.cfg['dataset_home']).iterdir()),[])

    def test_new_group_plan_is_read_only_until_commit_and_visible_as_children(self):
        plan,cfg,payload=self.plan([{'instruction':'抓娃娃','episodes':2},{'instruction':'抓布块','episodes':3}])
        self.assertEqual(cfg['episodes'],5);self.assertFalse(Path(plan['root']).exists())
        groups.commit_groups(plan);root=Path(plan['root'])
        self.assertTrue((root/groups.MARKER).is_file());self.assertFalse((root/'meta/info.json').exists())
        for target in plan['targets']:
            child=Path(target['root']);self.assertEqual(child.parent,root);self.assertTrue((child/'.platform-plan.json').is_file())
            self.assertEqual(naming.describe(child)['simple_spec']['instruction'],target['instruction'])
        items=list_datasets(self.cfg);self.assertEqual(len(items),2)
        self.assertTrue(all(i['capture_group']['collection_root']==str(root) for i in items))
        self.assertEqual(len(groups.collections(self.cfg)),1)

    def test_append_routes_same_instruction_to_existing_folder_and_adds_only_new_group(self):
        plan,_,_=self.plan([{'instruction':'A','episodes':2}]);groups.commit_groups(plan)
        cfg=self.cfg.copy();payload={'capture_mode':'append','dataset_root':plan['root'],'append_confirmed':True,'task':'默认',
            'instruction_groups':[{'instruction':'A','episodes':3},{'instruction':'B','episodes':2}]}
        next_plan=groups.prepare_record(cfg,payload)
        self.assertEqual(next_plan['targets'][0]['root'],plan['targets'][0]['root'])
        self.assertEqual(next_plan['targets'][1]['id'],'g002');groups.commit_groups(next_plan)
        self.assertEqual(len(groups.read_collection(plan['root'])['groups']),2)

    def test_append_collection_without_groups_routes_default_task_to_one_child(self):
        plan,_,_=self.plan([{'instruction':'A','episodes':2}]);groups.commit_groups(plan)
        payload={'capture_mode':'append','dataset_root':plan['root'],'append_confirmed':True,'task':'A','episodes':3}
        next_plan=groups.prepare_record(self.cfg.copy(),payload)
        self.assertEqual(next_plan['targets'][0]['root'],plan['targets'][0]['root']);self.assertEqual(next_plan['targets'][0]['episodes'],3)

    def test_append_one_group_cannot_silently_change_language(self):
        plan,_,_=self.plan([{'instruction':'A','episodes':2}]);groups.commit_groups(plan);child=plan['targets'][0]['root']
        payload={'capture_mode':'append','dataset_root':child,'append_confirmed':True,'task':'B'}
        with self.assertRaisesRegex(ValueError,'沿用'):groups.prepare_record(self.cfg.copy(),payload)
        payload['task']='A';self.assertIsNone(groups.prepare_record(self.cfg.copy(),payload))
        self.assertNotIn('capture_targets',payload)

    def test_existing_plain_dataset_cannot_be_reorganized_by_append(self):
        _,cfg,payload=self.plan([]);root=Path(payload['dataset_root']);root.mkdir()
        (root/'.platform-plan.json').write_text('{}',encoding='utf-8');naming.store_metadata(root,payload['dataset_spec'])
        request={'capture_mode':'append','dataset_root':str(root),'append_confirmed':True,'instruction_groups':[{'instruction':'A','episodes':1}]}
        with self.assertRaisesRegex(ValueError,'普通数据集'):groups.prepare_record(self.cfg.copy(),request)
        self.assertEqual([p.name for p in root.iterdir()],['.platform-plan.json'])

    def test_sanitized_names_do_not_merge_distinct_instructions(self):
        plan,_,_=self.plan([{'instruction':'A/B','episodes':1},{'instruction':'A?B','episodes':1}])
        self.assertNotEqual(plan['targets'][0]['root'],plan['targets'][1]['root'])
        self.assertEqual([t['instruction'] for t in plan['targets']],['A/B','A?B'])
        self.assertTrue(all(Path(t['root']).parent==Path(plan['root']) for t in plan['targets']))
        groups.commit_groups(plan)  # Distinct sanitized instructions must also have distinct registry aliases.

    def test_collection_children_stay_discoverable_without_registry_and_nested_capture_is_rejected(self):
        plan,_,_=self.plan([{'instruction':'A','episodes':1}]);groups.commit_groups(plan)
        with self.assertRaisesRegex(ValueError,'不能嵌套'):self.plan([],parent=plan['root'])
        with patch('dataset_library.registry',return_value={'datasets':{}}),patch('capture_groups.registry',return_value={'datasets':{}}):
            items=list_datasets(self.cfg)
        self.assertEqual([i['root'] for i in items],[plan['targets'][0]['root']])

    def test_manifest_path_traversal_is_rejected(self):
        plan,_,_=self.plan([{'instruction':'A','episodes':1}]);groups.commit_groups(plan)
        root=Path(plan['root']);data=groups.read_collection(root);data['groups'][0]['folder']='../outside'
        (root/groups.MARKER).write_text(json.dumps(data),encoding='utf-8')
        with self.assertRaises(ValueError):groups.read_collection(root)

    def test_commit_rejects_new_root_replaced_between_preview_and_start(self):
        plan,_,_=self.plan([{'instruction':'A','episodes':1}]);root=Path(plan['root']);root.mkdir()
        evidence=root/'keep.txt';evidence.write_text('unchanged',encoding='utf-8')
        with self.assertRaises(FileExistsError):groups.commit_groups(plan)
        self.assertEqual(evidence.read_text(encoding='utf-8'),'unchanged');self.assertFalse((root/groups.MARKER).exists())

    def test_server_allocates_group_folders_before_one_worker_and_passes_authoritative_plan(self):
        from server import Platform
        cfg=self.cfg.copy()
        with patch('server.load_config',return_value=cfg):app=Platform(offline=False)
        fake=Mock();fake.pid=123;fake.poll.return_value=0
        options={'capture_mode':'new','task':'默认','instruction_groups':[{'instruction':'A','episodes':2},{'instruction':'B','episodes':3}]}
        def spawn(*args,**kwargs):
            self.assertEqual(len(list_datasets(self.cfg)),2);return fake
        with patch('server.require_capability',create=True),patch('server.subprocess.Popen',side_effect=spawn),patch('server.threading.Thread'):
            job=app.start({'kind':'record','options':options})
        request=json.loads(fake.stdin.write.call_args.args[0])
        self.assertEqual(request['config']['episodes'],5);self.assertEqual(len(request['options']['capture_targets']),2)
        self.assertEqual(request['options']['capture_collection'],job['dataset_root'])

    def test_recording_lease_reuses_connection_and_defers_cleanup_to_owner(self):
        device=Mock();device.is_connected=False
        device.connect.side_effect=lambda:setattr(device,'is_connected',True)
        lease=groups.RecordingLease(device)
        listener=groups.ListenerLease(device)
        for _ in range(3):lease.connect();lease.disconnect();listener.stop()
        device.connect.assert_called_once();device.disconnect.assert_not_called();device.stop.assert_not_called()
        device.disconnect();device.stop();self.assertEqual(device.disconnect.call_count,1)


if __name__=='__main__':unittest.main()
