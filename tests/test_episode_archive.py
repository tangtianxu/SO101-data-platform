"""Recoverable batch deletion: synthetic metadata only, no hardware or user data."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import ROBOT_HOME,atomic_json
import review_store as reviews


class EpisodeArchiveTests(unittest.TestCase):
    def setUp(self):
        temp=ROBOT_HOME/'temp';temp.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=temp);self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        self.root=self.base/'raw';atomic_json(self.root/'meta/info.json',{'total_episodes':3})
        (self.root/'data').mkdir();(self.root/'data/original.bin').write_bytes(b'original numeric and video placeholder')
        p=patch.object(reviews,'ANNOTATIONS',self.base/'notes');p.start();self.addCleanup(p.stop)
        reviews.save_review(self.root,0,'success','note 0');reviews.save_review(self.root,1,'failure','note 1')
        notes=reviews.read_reviews(self.root);notes['1']['interrupted']=True;atomic_json(reviews.annotation_path(self.root),notes)
        self.hashes=self.hashes_of_source()
    def hashes_of_source(self):return {str(p.relative_to(self.root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file()}
    def test_bulk_delete_and_restore_preserve_individual_status_notes_provenance_and_all_source_bytes(self):
        result=reviews.archive_reviews(self.root,[0,1,2],'delete');self.assertEqual(result['changed'],3)
        self.assertTrue(all(row['deleted'] and row['result']=='exclude' for row in result['annotations'].values()))
        self.assertEqual(result['annotations']['1']['before_delete_result'],'failure');self.assertTrue(result['annotations']['1']['interrupted'])
        result=reviews.archive_reviews(self.root,[0,1,2],'restore');self.assertEqual(result['changed'],3)
        self.assertEqual([result['annotations'][str(i)]['result'] for i in range(3)],['success','failure','unreviewed'])
        self.assertEqual(result['annotations']['0']['note'],'note 0');self.assertEqual(result['annotations']['1']['note'],'note 1')
        self.assertTrue(all(not row['deleted'] and 'before_delete_result' not in row for row in result['annotations'].values()))
        self.assertEqual(self.hashes_of_source(),self.hashes)
    def test_delete_and_restore_are_idempotent_and_mixed_rows_only_count_actual_changes(self):
        reviews.archive_reviews(self.root,[0],'delete');before=reviews.annotation_path(self.root).read_bytes()
        self.assertEqual(reviews.archive_reviews(self.root,[0],'delete')['changed'],0);self.assertEqual(reviews.annotation_path(self.root).read_bytes(),before)
        self.assertEqual(reviews.archive_reviews(self.root,[0,1],'delete')['changed'],1)
        self.assertEqual(reviews.archive_reviews(self.root,[0,2],'restore')['changed'],1)
        before=reviews.annotation_path(self.root).read_bytes();self.assertEqual(reviews.archive_reviews(self.root,[0,2],'restore')['changed'],0)
        self.assertEqual(reviews.annotation_path(self.root).read_bytes(),before)
    def test_invalid_or_duplicate_selection_never_saves_a_partial_batch(self):
        before=reviews.annotation_path(self.root).read_bytes()
        for ids in (None,[],[0,3],[0,0],[True],[1.2],['0']):
            with self.subTest(ids=ids),self.assertRaises(ValueError):reviews.archive_reviews(self.root,ids,'delete')
        self.assertEqual(reviews.annotation_path(self.root).read_bytes(),before)
    def test_invalid_saved_status_or_archive_action_never_saves_a_partial_batch(self):
        notes=reviews.read_reviews(self.root);notes['1']['result']='invalid';atomic_json(reviews.annotation_path(self.root),notes)
        before=reviews.annotation_path(self.root).read_bytes()
        with self.assertRaises(ValueError):reviews.archive_reviews(self.root,[0,1],'delete')
        with self.assertRaises(ValueError):reviews.archive_reviews(self.root,[0],'erase')
        self.assertEqual(reviews.annotation_path(self.root).read_bytes(),before)
    def test_one_atomic_save_per_batch_and_io_failure_leaves_previous_notes_intact(self):
        with patch.object(reviews,'atomic_json',wraps=atomic_json) as save:
            reviews.archive_reviews(self.root,[0,1,2],'delete');save.assert_called_once()
        before=reviews.annotation_path(self.root).read_bytes()
        with patch.object(reviews,'atomic_json',side_effect=OSError('disk error')):
            with self.assertRaises(OSError):reviews.archive_reviews(self.root,[0,1,2],'restore')
        self.assertEqual(reviews.annotation_path(self.root).read_bytes(),before)
    def test_crop_view_deletion_maps_once_to_originals_and_both_views_restore_consistently(self):
        child=self.base/'crop';atomic_json(child/reviews.COLLECTION_FILE,{'schema_version':1,'operation':'crop_collection','source':str(self.root),
            'episodes':[{'episode_index':0,'source_episode':2},{'episode_index':1,'source_episode':0}]})
        result=reviews.archive_reviews(child,[0,1],'delete');self.assertEqual(result['changed'],2)
        original=reviews.read_reviews(self.root);self.assertTrue(original['2']['deleted']);self.assertTrue(original['0']['deleted']);self.assertNotIn('deleted',original['1'])
        self.assertTrue(result['annotations']['1']['inherited']);self.assertEqual(result['annotations']['1']['source_episode'],0)
        reviews.archive_reviews(self.root,[0,2],'restore');self.assertTrue(all(not row['deleted'] for row in reviews.read_reviews(child).values()))
        self.assertEqual(self.hashes_of_source(),self.hashes)
    def test_changed_source_mapping_is_rejected_before_any_annotation_write(self):
        child=self.base/'crop';atomic_json(child/reviews.COLLECTION_FILE,{'schema_version':1,'operation':'crop_collection','source':str(self.root),
            'episodes':[{'episode_index':0,'source_episode':99}]})
        before=reviews.annotation_path(self.root).read_bytes()
        with self.assertRaises(ValueError):reviews.archive_reviews(child,[0],'delete')
        self.assertEqual(reviews.annotation_path(self.root).read_bytes(),before)


class ArchiveHttpTests(unittest.TestCase):
    hashes_of_source=EpisodeArchiveTests.hashes_of_source
    def setUp(self):
        EpisodeArchiveTests.setUp(self)
        from common import load_config
        import dataset_naming as naming
        from server import Platform,Handler,ThreadingHTTPServer
        from urllib.request import build_opener,ProxyHandler
        import threading
        p=patch.object(naming,'REGISTRY',self.base/'registry.json');p.start();self.addCleanup(p.stop)
        cfg={**load_config(),'dataset_home':str(self.base),'log_home':str(self.base/'logs')}
        with patch('server.load_config',return_value=cfg):self.app=Platform(offline=True)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.app=self.app
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.url='http://127.0.0.1:'+str(self.server.server_port);self.opener=build_opener(ProxyHandler({}))
    def post(self,payload,token=True):
        from urllib.request import Request
        headers={'Content-Type':'application/json','Origin':self.url,'X-SO101-Build':self.app.deployment.build}
        if token:headers['X-SO101-Token']=self.app.token
        return self.opener.open(Request(self.url+'/api/episodes/archive',data=json.dumps(payload).encode() if token else b'',headers=headers),timeout=4)
    def test_authenticated_batch_delete_and_restore_never_spawn_device_processes(self):
        from urllib.error import HTTPError
        request={'root':str(self.root),'episodes':[0,1,2],'action':'delete','crop_revision':None}
        with self.assertRaises(HTTPError):self.post(request,token=False)
        with patch('server.subprocess.Popen') as spawn:
            with self.post(request) as response:self.assertEqual(json.load(response)['changed'],3)
            with self.post({**request,'action':'restore'}) as response:self.assertEqual(json.load(response)['annotations']['1']['result'],'failure')
            spawn.assert_not_called()
        self.assertEqual(self.hashes_of_source(),self.hashes)
    def test_running_capture_and_crop_block_the_whole_batch_but_paused_capture_allows_it(self):
        from urllib.error import HTTPError
        from unittest.mock import Mock
        self.app.proc=Mock();self.app.proc.poll.return_value=None
        request={'root':str(self.root),'episodes':[0,1],'action':'delete'};before=reviews.annotation_path(self.root).read_bytes()
        for kind,phase in [('record','RECORDING'),('record','RESET'),('crop','CROPPING'),('act','RUNNING'),('control','CONTROL')]:
            self.app.job={'id':'fake','kind':kind,'phase':phase}
            with self.subTest(kind=kind),self.assertRaises(HTTPError):self.post(request)
            self.assertEqual(reviews.annotation_path(self.root).read_bytes(),before)
        self.app.job={'id':'fake','kind':'record','phase':'PAUSED'}
        with self.post(request) as response:self.assertEqual(json.load(response)['changed'],2)
    def test_crop_mapping_revision_and_outside_workspace_roots_are_checked_before_any_write(self):
        from urllib.error import HTTPError
        child=self.base/'crop';atomic_json(child/reviews.COLLECTION_FILE,{'schema_version':1,'operation':'crop_collection','source':str(self.root),
            'episodes':[{'episode_index':0,'source_episode':2}]})
        before=reviews.annotation_path(self.root).read_bytes();request={'root':str(child),'episodes':[0],'action':'delete'}
        with self.assertRaises(HTTPError):self.post(request)
        with self.assertRaises(HTTPError):self.post({**request,'root':str(self.base.parent)})
        self.assertEqual(reviews.annotation_path(self.root).read_bytes(),before)
        with self.post({**request,'crop_revision':reviews.review_revision(child)}) as response:self.assertEqual(json.load(response)['changed'],1)
        self.assertTrue(reviews.read_reviews(self.root)['2']['deleted'])


if __name__=='__main__':unittest.main()
