"""Offline tests: success gate, upsert, inherited reviews, rollback and lossless merge."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import HERE, atomic_json, environment, load_config
environment()
import crop_collection as crops
import dataset_naming as naming
import review_store as reviews
from data_tools import inspect_dataset


class CropCollectionTests(unittest.TestCase):
    def setUp(self):
        (HERE/'state/tests').mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=HERE/'state/tests')
        self.base=Path(self.temp.name);self.home=self.base/'datasets';self.home.mkdir()
        self.cfg={**load_config(),'dataset_home':str(self.home)}
        self.patches=[patch.object(naming,'REGISTRY',self.base/'registry.json'),
                      patch.object(reviews,'ANNOTATIONS',self.base/'annotations'),
                      patch.object(crops,'ROBOT_HOME',self.base)]
        for p in self.patches:p.start()
        self.source=self.home/'raw'
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        from lerobot.configs.video import RGBEncoderConfig
        import numpy as np
        features={key:{'dtype':'float32','shape':(6,),'names':['j'+str(i) for i in range(6)]}
                  for key in ('observation.state','action','observation.sensors.temperature')}
        for key in ('top','wrist'):
            features['observation.images.'+key]={'dtype':'video','shape':(16,16,3),'names':['height','width','channels']}
        writer=LeRobotDataset.create('local/test_crops',root=self.source,fps=30,features=features,
            use_videos=True,streaming_encoding=True,encoder_threads=1,metadata_buffer_size=1,
            rgb_encoder=RGBEncoderConfig(vcodec='h264',g=30,crf=20,preset='fast'))
        for ep in range(3):
            for i in range(12):
                writer.add_frame({'task':'put doll in box','observation.state':np.ones(6,dtype=np.float32)*(ep+i),
                    'action':np.ones(6,dtype=np.float32)*(ep+i+.5),'observation.sensors.temperature':np.ones(6,dtype=np.float32)*30,
                    'observation.images.top':np.full((16,16,3),i*10,dtype=np.uint8),
                    'observation.images.wrist':np.full((16,16,3),ep*30,dtype=np.uint8)})
            writer.save_episode(parallel_encoding=False)
        writer.finalize()
        self.before={str(p.relative_to(self.source)):p.read_bytes() for p in self.source.rglob('*') if p.is_file()}

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        __import__('gc').collect()
        self.temp.cleanup()

    def test_only_successful_originals_can_start_crop(self):
        for status in ('unreviewed','failure','exclude'):
            reviews.save_review(self.source,0,status)
            self.assertFalse(crops.plan_crop(self.cfg,self.source,0)['can_crop'])
            with self.assertRaises(ValueError):crops.save_crop(self.cfg,self.source,0,0,.2)
        reviews.save_review(self.source,0,'success')
        self.assertTrue(crops.plan_crop(self.cfg,self.source,0)['can_crop'])

    def test_add_and_replace_keep_one_dataset_and_all_labels(self):
        for ep in (0,1):reviews.save_review(self.source,ep,'success',f'note {ep}')
        first=crops.save_crop(self.cfg,self.source,0,0,.3)
        second=crops.save_crop(self.cfg,self.source,1,0,.3)
        root=Path(first['destination'])
        self.assertEqual(root,Path(second['destination']))
        self.assertEqual(second['total_crops'],2)
        plan=crops.plan_crop(self.cfg,self.source,0)
        self.assertTrue(plan['replacing'])
        third=crops.save_crop(self.cfg,self.source,0,.1,.3)
        self.assertTrue(third['replaced']);self.assertEqual(third['total_crops'],2)
        self.assertEqual(inspect_dataset(root)['total_episodes'],2)
        self.assertEqual([e['source_episode'] for e in reviews.read_collection(root)['episodes']],[0,1])
        self.assertEqual(inspect_dataset(root,0)['selected']['frames'],6)
        self.assertEqual(inspect_dataset(root,1)['selected']['frames'],9)
        self.assertEqual(reviews.read_reviews(root)['0']['note'],'note 0')
        reviews.save_review(root,0,'failure','changed in cropped view')
        self.assertEqual(reviews.read_reviews(self.source)['0']['result'],'failure')
        self.assertEqual(reviews.read_reviews(root)['0']['result'],'failure')
        self.assertFalse(crops.plan_crop(self.cfg,self.source,0)['can_crop'])
        with self.assertRaises(ValueError):reviews.require_success(root,0)
        self.assertEqual({str(p.relative_to(self.source)):p.read_bytes() for p in self.source.rglob('*') if p.is_file()},self.before)
        self.assertEqual(len(list(self.home.glob('*/meta/info.json'))),2)  # raw + exactly one cropped dataset
        self.assertTrue(Path(third['previous_revision_backup']).is_dir())

    def test_failed_publish_restores_previous_collection(self):
        reviews.save_review(self.source,0,'success')
        first=crops.save_crop(self.cfg,self.source,0,0,.3)
        root=Path(first['destination']);manifest=reviews.read_collection(root)
        workspace=self.home/'.pending-crop-update-rollback';workspace.mkdir()
        staged=workspace/'assembled';staged.mkdir()
        original_rename=Path.rename
        def fail_new(path,target):
            if path==staged:raise OSError('simulated failed folder swap')
            return original_rename(path,target)
        with patch.object(Path,'rename',fail_new):
            with self.assertRaises(OSError):crops.publish(staged,root,manifest,workspace)
        self.assertEqual(reviews.read_collection(root),manifest)


if __name__=='__main__':unittest.main(verbosity=2)
