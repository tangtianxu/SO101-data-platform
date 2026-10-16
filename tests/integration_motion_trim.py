"""Optional real LeRobot/Arrow/two-camera file integration; no hardware."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import NAMES, ROBOT_HOME, atomic_json, load_config
import dataset_naming as naming
import crop_collection as crops
import review_store as reviews
import motion_trim as motion
from cropping import sha256

AVAILABLE=all(importlib.util.find_spec(name) is not None for name in ("numpy","lerobot","av","pyarrow"))


@unittest.skipUnless(AVAILABLE,"Run in the configured LeRobot backend environment")
class MotionIntegrationTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        from lerobot.configs.video import RGBEncoderConfig
        temp=ROBOT_HOME/"temp";temp.mkdir(parents=True,exist_ok=True)
        self.folder=tempfile.TemporaryDirectory(dir=temp);self.addCleanup(self.folder.cleanup)
        self.base=Path(self.folder.name);self.home=self.base/"datasets";self.home.mkdir()
        self.root=self.home/"raw";self.cfg={**load_config(),"dataset_home":str(self.home)}
        for obj,key,value in ((naming,"REGISTRY",self.base/"registry.json"),(reviews,"ANNOTATIONS",self.base/"annotations"),(crops,"ROBOT_HOME",self.base)):
            p=patch.object(obj,key,value);p.start();self.addCleanup(p.stop)
        schema={key:{"dtype":"float32","shape":(6,),"names":[n+".pos" for n in NAMES]} for key in ("observation.state","action")}
        schema["observation.sensors.temperature"]={"dtype":"float32","shape":(6,),"names":list(NAMES)}
        for camera in ("top","wrist"):
            schema["observation.images."+camera]={"dtype":"video","shape":(16,16,3),"names":["height","width","channels"]}
        writer=LeRobotDataset.create("local/motion_test",root=self.root,fps=10,features=schema,use_videos=True,
            streaming_encoding=True,encoder_threads=1,metadata_buffer_size=1,
            rgb_encoder=RGBEncoderConfig(vcodec="h264",g=10,crf=20,preset="fast"))
        try:
            for ep in range(4):
                for i in range(60):
                    q=np.zeros(6,dtype="float32")
                    q[0]=0 if ep==3 else min(40,max(0,i-10)*2)
                    writer.add_frame({"task":"put doll in box","observation.state":q,"action":q+.25,
                        "observation.sensors.temperature":np.ones(6,dtype="float32")*(30+ep),
                        "observation.images.top":np.full((16,16,3),i*3,dtype="uint8"),
                        "observation.images.wrist":np.full((16,16,3),ep*40,dtype="uint8")})
                writer.save_episode(parallel_encoding=False)
            writer.finalize()
        finally:writer.finalize()
        naming.store_metadata(self.root,naming.simple_defaults())
        for ep in range(4):reviews.save_review(self.root,ep,"failure" if ep==1 else "success","note "+str(ep))
        self.original=self.hashes(self.root)

    def hashes(self,root):
        return {str(p.relative_to(root)):sha256(p) for p in root.rglob("*") if p.is_file()}

    def bridge(self):
        return SimpleNamespace(stop=threading.Event(),state=lambda *args,**kw:None,emit=lambda *args,**kw:None)

    def test_real_preview_and_batch_apply_preserve_labels_video_and_reviews(self):
        from data_tools import inspect_dataset
        request=motion.prepare_job(self.cfg,{"mode":"motion_preview","dataset_root":str(self.root)},None)
        out=self.base/"preview";out.mkdir()
        result=motion.run_job(self.cfg,motion.materialize(request,out),self.bridge(),out)
        self.assertEqual(result["ready_count"],2);self.assertEqual(result["skipped_count"],1)
        self.assertEqual(list(self.home.iterdir()),[self.root])
        plan=json.loads((out/"motion_plan.json").read_text(encoding="utf-8"))
        self.assertEqual([r["episode"] for r in plan["rows"] if r["status"]=="ready"],[0,2])
        job={"id":"preview","output":str(out),"finished":True,"result":result}
        apply=motion.prepare_job(self.cfg,{"mode":"motion_apply","dataset_root":str(self.root),
            "preview_job_id":"preview","episodes":[0,2]},job)
        apply_out=self.base/"apply";apply_out.mkdir()
        result=motion.run_job(self.cfg,motion.materialize(apply,apply_out),self.bridge(),apply_out)
        destination=Path(result["destination"])
        self.assertEqual(result["processed_count"],2);self.assertEqual(inspect_dataset(destination)["total_episodes"],2)
        manifest=reviews.read_collection(destination)
        self.assertEqual([r["source_episode"] for r in manifest["episodes"]],[0,2])
        for row in manifest["episodes"]:
            self.assertEqual(row["automatic_trim"]["method"],"six_joint_motion_v1")
            self.assertTrue(row["source_frame_range_half_open"][0]>0)
            self.assertEqual(reviews.read_reviews(destination)[str(row["episode_index"])]["note"],"note "+str(row["source_episode"]))
        self.assertEqual(self.hashes(self.root),self.original)
        self.assertTrue(result["checks"]["numeric_labels_verified"]);self.assertTrue(result["checks"]["videos_copied_losslessly"])

    def test_multi_replace_interleaves_retained_episode_and_keeps_one_collection(self):
        from data_tools import inspect_dataset
        reviews.save_review(self.root,1,"success")
        first=crops.save_crops(self.cfg,self.root,[{"episode":ep,"start":0,"end":5} for ep in (0,1,2)])
        root=Path(first["destination"])
        old_video=reviews.read_collection(root)["episodes"][1]["video_sha256"]
        result=crops.save_crops(self.cfg,self.root,[{"episode":ep,"start":.5,"end":4} for ep in (0,2)])
        self.assertEqual(result["total_crops"],3);self.assertEqual(result["replaced_episodes"],[0,2])
        entries=reviews.read_collection(root)["episodes"]
        self.assertEqual([r["source_episode"] for r in entries],[0,1,2]);self.assertEqual(entries[1]["video_sha256"],old_video)
        self.assertEqual(inspect_dataset(root,0)["selected"]["frames"],35)
        self.assertEqual(inspect_dataset(root,1)["selected"]["frames"],50)
        self.assertEqual(inspect_dataset(root,2)["selected"]["frames"],35)
        self.assertTrue(Path(result["previous_revision_backup"]).is_dir())
        self.assertEqual(self.hashes(self.root),self.original)
        self.assertEqual(len(list(self.home.glob("*/meta/info.json"))),2)

    def test_cancel_between_new_clips_leaves_the_entire_previous_collection_unchanged(self):
        result=crops.save_crops(self.cfg,self.root,[{"episode":0,"start":0,"end":5}])
        root=Path(result["destination"]);before=self.hashes(root);stop=threading.Event()
        def episode(index,total,ep):
            if index==2:stop.set()
        with self.assertRaises(InterruptedError):
            crops.save_crops(self.cfg,self.root,[{"episode":ep,"start":.5,"end":4} for ep in (0,2)],stop=stop,on_episode=episode)
        self.assertEqual(self.hashes(root),before);self.assertEqual(self.hashes(self.root),self.original)


if __name__=="__main__":unittest.main()
