"""Naming does not move datasets or rewrite training references/measurements."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import HERE,atomic_json,environment,load_config
environment()
import dataset_naming as naming
from dataset_library import create_dataset,list_datasets
from server import Platform


class NamingTests(unittest.TestCase):
    def setUp(self):
        (HERE/'state/tests').mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=HERE/'state/tests');self.base=Path(self.temp.name)
        self.home=self.base/'datasets';self.home.mkdir()
        self.cfg={**load_config(),'dataset_home':str(self.home)}
        self.mock=patch.object(naming,'REGISTRY',self.base/'registry.json');self.mock.start()
        self.spec={**naming.defaults(),'date':'20261015'}

    def tearDown(self):self.mock.stop();self.temp.cleanup()

    def test_canonical_name_parse_roundtrip(self):
        expected='so101_real_doll_pick_place_demo_raw_20261015_b001_v001'
        self.assertEqual(naming.canonical_name(self.spec),expected)
        self.assertEqual(naming.parse_name(expected),self.spec)

    def test_reject_bad_dates_codes_and_unknown_options(self):
        for patch_spec in ({'task':'../bad'},{'task':'中文'},{'date':'20260230'},{'batch':0},{'version':1000},
                           {'domain':'other'},{'note':'a'*201},{'purpose':'smoke','review':'train'}):
            with self.assertRaises(ValueError):naming.validate_spec({**self.spec,**patch_spec})

    def test_notes_are_not_part_of_filesystem_name(self):
        self.assertEqual(naming.canonical_name(self.spec),naming.canonical_name({**self.spec,'note':'娃娃，桌上左侧 / 位置变化'}))

    def test_processed_is_not_automatically_training_approved(self):
        spec={**self.spec,'stage':'processed'};self.assertEqual(naming.parse_name(naming.canonical_name(spec))['review'],'pending')
        with self.assertRaises(ValueError):naming.store_metadata(self.home/'legacy',{**spec,'review':'train'})

    def test_labeling_keeps_path_bytes_uid_and_source_untouched(self):
        root=self.home/'legacy_folder';root.mkdir();(root/'original.bin').write_bytes(b'unchanged')
        first=naming.store_metadata(root,self.spec)
        second=naming.store_metadata(root,{**self.spec,'note':'新备注','review':'train'},review_confirmed=True)
        self.assertEqual(first['dataset_id'],second['dataset_id'])
        self.assertEqual((root/'original.bin').read_bytes(),b'unchanged');self.assertTrue(root.exists())
        self.assertEqual(second['spec']['review'],'train')

    def test_old_alias_reserves_batch_even_without_renaming_folder(self):
        root=self.home/'legacy_folder';root.mkdir();naming.store_metadata(root,self.spec)
        self.assertTrue(naming.preview_name(self.cfg,self.spec)['name_reserved'])
        self.assertEqual(naming.suggest_name(self.cfg,self.spec)['spec']['batch'],2)
        self.assertFalse(naming.preview_name(self.cfg,self.spec,ignore_root=root)['exists'])

    def test_duplicate_logical_names_rejected(self):
        for name in ('one','two'):(self.home/name).mkdir()
        naming.store_metadata(self.home/'one',self.spec)
        with self.assertRaises(ValueError):naming.store_metadata(self.home/'two',self.spec)

    def test_new_capture_rejects_arbitrary_or_processed_training_names(self):
        canonical=Path(naming.preview_name(self.cfg,self.spec)['root'])
        self.assertEqual(naming.validate_capture(self.cfg,canonical,self.spec,False),self.spec)
        with self.assertRaises(ValueError):naming.validate_capture(self.cfg,self.home/'random',self.spec,False)
        with self.assertRaises(ValueError):naming.validate_capture(self.cfg,canonical,{**self.spec,'stage':'processed'},False)

    def test_training_and_processed_datasets_not_capture_append_targets(self):
        root=self.home/'old';root.mkdir()
        naming.store_metadata(root,{**self.spec,'stage':'processed','review':'train'},review_confirmed=True)
        with self.assertRaises(ValueError):naming.validate_capture(self.cfg,root,None,True)

    def test_management_accepts_options_not_free_names_and_cannot_train_empty_folder(self):
        app=Platform(offline=True);app.config=self.cfg
        with self.assertRaises(ValueError):app.manage_dataset({'action':'create','name':'arbitrary'})
        created=app.manage_dataset({'action':'create','spec':self.spec})
        self.assertEqual(Path(created['root']).name,naming.canonical_name(self.spec))
        listed=list_datasets(self.cfg);self.assertTrue(listed[0]['naming']['append_allowed'])
        with self.assertRaises(ValueError):app.manage_dataset({'action':'label','root':created['root'],
            'spec':{**self.spec,'review':'train'},'confirmed':True,'review_confirmed':True})


if __name__=='__main__':unittest.main(verbosity=2)
