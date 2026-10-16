"""Metadata-only dataset order tests; no source datasets, device or model access."""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import ROBOT_HOME
import dataset_library as library


class DatasetListingTests(unittest.TestCase):
    def setUp(self):
        temp=ROBOT_HOME/'temp';temp.mkdir(exist_ok=True,parents=True)
        self.temp=tempfile.TemporaryDirectory(dir=temp);self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.naming={}
        for target,value in [('dataset_library.protected_roots',{}),('dataset_library.registry',{'datasets':{}})]:
            mock=patch(target,return_value=value);mock.start();self.addCleanup(mock.stop)
        mock=patch('dataset_library.describe',side_effect=lambda folder,*_:self.naming.get(folder.name,{}))
        mock.start();self.addCleanup(mock.stop)

    def add(self,name,start=None,plan=None):
        folder=self.home/name;folder.mkdir();(folder/'meta').mkdir()
        (folder/'meta/info.json').write_text(json.dumps({'total_episodes':2}),encoding='utf-8')
        if start:self.naming[name]={'simple_spec':{'started_at':start}}
        if plan is not None:(folder/'.platform-plan.json').write_text(json.dumps({'created':plan}),encoding='utf-8')
        return folder

    def items(self):return library.list_datasets({'dataset_home':str(self.home)})

    def test_start_time_descending_not_folder_name(self):
        self.add('z_old','2026-10-14T12:30:00+08:00');self.add('a_new','2026-10-16T09:00:00+08:00')
        items=self.items();self.assertEqual([i['name'] for i in items],['a_new','z_old'])
        self.assertTrue(all(i['time_source']=='collection_start' for i in items))

    def test_minute_and_legacy_date_only_supported(self):
        self.add('same_day','2026-10-15');self.add('later','2026-10-15T19:44')
        self.assertEqual([i['name'] for i in self.items()],['later','same_day'])

    def test_iso_timezones_are_compared_as_instants(self):
        self.add('older','2026-10-16T02:00:00Z');self.add('newer','2026-10-16T11:00:00+08:00')
        self.assertEqual([i['name'] for i in self.items()],['newer','older'])

    def test_modifying_label_or_file_mtime_does_not_promote_old_dataset(self):
        folder=self.add('old','2026-10-14T09:00');self.add('new','2026-10-15T09:00')
        before=self.items();os.utime(folder,(datetime(2030,1,1).timestamp(),)*2)
        self.naming['old']['note']='edited today'
        self.assertEqual([i['sort_time'] for i in self.items()],[i['sort_time'] for i in before])
        self.assertEqual([i['name'] for i in self.items()],['new','old'])

    def test_unknown_dataset_uses_plan_time_and_invalid_plan_falls_back(self):
        planned=self.add('planned',plan='2026-10-16T10:10:00+08:00')
        self.add('invalid',plan=17)
        items={i['name']:i for i in self.items()}
        self.assertEqual(items['planned']['time_source'],'plan_created')
        self.assertEqual(items['planned']['sort_time'],datetime.fromisoformat('2026-10-16T10:10:00+08:00').timestamp())
        self.assertEqual(items['invalid']['time_source'],'filesystem_created')
        self.assertTrue(planned.is_dir())

    def test_more_than_100_datasets_remain_visible(self):
        for i in range(105):self.add(f'dataset_{i:03}',f'2026-10-15T{i//60:02}:{i%60:02}')
        items=self.items();self.assertEqual(len(items),105);self.assertEqual(items[0]['name'],'dataset_104')

    def test_equal_timestamps_have_deterministic_order(self):
        self.add('b','2026-10-15T09:00');self.add('a','2026-10-15T09:00')
        self.assertEqual([i['name'] for i in self.items()],['b','a'])


if __name__=='__main__':unittest.main()
