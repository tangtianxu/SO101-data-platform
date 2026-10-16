"""Dependency-free motion boundaries, atomic reviews and job-plan contracts."""
import json
import math
import struct
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import ROBOT_HOME, NAMES, atomic_json, load_config
import motion_trim as motion
import review_store as reviews
import dataset_naming as naming


class BoundaryTests(unittest.TestCase):
    def trace(self, joint=0, spikes=False):
        times=[i/30 for i in range(300)]
        rows=[]
        for i,t in enumerate(times):
            value=0 if t<2 else min(20,(t-2)*10) if t<4 else 20 if t<6 else min(40,20+(t-6)*10)
            q=[0.]*6;q[joint]=value
            rows.append(q)
        if spikes: rows[15][0]=90.
        return times,rows

    def test_static_head_and_tail_trimmed_but_middle_pause_is_preserved(self):
        times,rows=self.trace()
        result=motion.boundary(times,rows,30)
        self.assertEqual(result["status"],"ready")
        self.assertLessEqual(result["start"],2)
        self.assertGreater(result["start"],1)
        self.assertGreaterEqual(result["end"],8)
        self.assertLess(result["end"],9.5)
        self.assertLess(result["start"],4);self.assertGreater(result["end"],6)
        self.assertEqual(result["kept_frames"],result["last_frame"]-result["first_frame"])

    def test_all_six_joints_can_trigger_motion_with_separate_gripper_units(self):
        for joint in range(6):
            times,rows=self.trace(joint)
            with self.subTest(joint=joint):self.assertEqual(motion.boundary(times,rows,30)["status"],"ready")

    def test_single_frame_spike_and_small_encoder_noise_do_not_trigger(self):
        times=[i/30 for i in range(180)]
        rows=[[.1*(-1)**i]*6 for i in range(180)];rows[20][0]=100
        self.assertEqual(motion.boundary(times,rows,30)["status"],"skip")

    def test_spike_before_real_motion_does_not_move_the_boundary(self):
        times,rows=self.trace(spikes=True)
        self.assertGreater(motion.boundary(times,rows,30)["start"],1)

    def test_only_trim_start_when_tail_disabled(self):
        times,rows=self.trace()
        result=motion.boundary(times,rows,30,{"trim_tail":False})
        self.assertEqual(result["end"],10.)

    def test_zero_padding_and_full_motion_remain_valid(self):
        times=[i/30 for i in range(90)]
        rows=[[i*2.]*6 for i in range(90)]
        result=motion.boundary(times,rows,30,{"pad_before_s":0,"pad_after_s":0})
        self.assertEqual(result["status"],"ready")
        self.assertEqual(result["start"],0);self.assertEqual(result["end"],3.)

    def test_target_only_motion_used_only_when_requested(self):
        times,actions=self.trace()
        states=[[0.]*6 for _ in times]
        self.assertEqual(motion.boundary(times,states,30)["status"],"skip")
        self.assertEqual(motion.boundary(times,states,30,{"signal":"state_or_action"},actions)["status"],"ready")

    def test_invalid_positions_or_timestamps_are_not_filled_or_guessed(self):
        times,rows=self.trace()
        for bad in (float("nan"),float("inf")):
            clone=[r[:] for r in rows];clone[10][0]=bad
            with self.assertRaises(ValueError):motion.boundary(times,clone,30)
        for stamp in (-1, .75, float("nan")):
            clone=times[:];clone[8]=stamp
            with self.assertRaises(ValueError):motion.boundary(clone,rows,30)

    def test_float32_timestamps_keep_exact_exclusive_frame_boundary(self):
        times=[struct.unpack("f",struct.pack("f",i/30))[0] for i in range(1800)]
        rows=[[min(40,max(0,i-1080)*2),0.,0.,0.,0.,0.] for i in range(1800)]
        result=motion.boundary(times,rows,30)
        self.assertEqual(result["status"],"ready")
        self.assertEqual(result["start"],times[result["first_frame"]])
        self.assertEqual(result["end"],times[result["last_frame"]])

    def test_invalid_parameters_fail_without_silent_defaults(self):
        for value in (False,[],{"unknown":1},{"arm_delta":0},{"window_s":True},
                      {"pad_before_s":float("nan")},{"signal":"random"},{"trim_tail":1}):
            with self.subTest(value=value),self.assertRaises(ValueError):motion.settings(value)


class ReviewAndPlanTests(unittest.TestCase):
    def setUp(self):
        temp=ROBOT_HOME/"temp";temp.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=temp);self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.home=self.base/"datasets";self.home.mkdir()
        self.root=self.home/"raw";self.root.mkdir()
        self.cfg={**load_config(),"dataset_home":str(self.home),"log_home":str(self.base/"logs")}
        atomic_json(self.root/"meta/info.json",{"codebase_version":"v3.0","total_episodes":4,"total_frames":120,"fps":30})
        self.raw=(self.root/"meta/info.json").read_bytes()
        for obj,key,value in ((reviews,"ANNOTATIONS",self.base/"annotations"),(naming,"REGISTRY",self.base/"registry.json")):
            item=patch.object(obj,key,value);item.start();self.addCleanup(item.stop)
        naming.store_metadata(self.root,naming.simple_defaults())
        reviews.save_review(self.root,0,"success","keep note 0")
        reviews.save_review(self.root,1,"failure","keep note 1")
        reviews.save_review(self.root,2,"success","keep note 2")

    def test_bulk_mark_preserves_individual_notes_and_unselected_rows(self):
        before=reviews.read_reviews(self.root)
        after=reviews.save_reviews(self.root,[0,1],"exclude")
        self.assertEqual(after["0"]["result"],"exclude");self.assertEqual(after["1"]["result"],"exclude")
        self.assertEqual(after["0"]["note"],"keep note 0");self.assertEqual(after["1"]["note"],"keep note 1")
        self.assertEqual(after["2"],before["2"]);self.assertEqual((self.root/"meta/info.json").read_bytes(),self.raw)

    def test_invalid_selection_is_all_or_nothing(self):
        before=reviews.annotation_path(self.root).read_bytes()
        for ids in ([],[0,4],[0,0],[True],[1.2],None):
            with self.subTest(ids=ids),self.assertRaises(ValueError):reviews.save_reviews(self.root,ids,"success")
        self.assertEqual(reviews.annotation_path(self.root).read_bytes(),before)

    def test_crop_view_bulk_mark_updates_the_same_original_records(self):
        child=self.home/"cropped";child.mkdir()
        atomic_json(child/reviews.COLLECTION_FILE,{"schema_version":1,"operation":"crop_collection",
            "source":str(self.root),"episodes":[{"episode_index":0,"source_episode":2},{"episode_index":1,"source_episode":0}]})
        after=reviews.save_reviews(child,[0],"failure")
        self.assertEqual(after["0"]["result"],"failure")
        self.assertEqual(reviews.read_reviews(self.root)["2"]["result"],"failure")
        self.assertEqual(reviews.read_reviews(self.root)["0"]["result"],"success")

    def test_preview_selects_success_only_and_does_not_create_data(self):
        request=motion.prepare_job(self.cfg,{"mode":"motion_preview","dataset_root":str(self.root)},None)
        self.assertEqual(request["episodes"],[0,2]);self.assertEqual(list(self.home.iterdir()),[self.root])
        chosen=motion.prepare_job(self.cfg,{"mode":"motion_preview","dataset_root":str(self.root),"episodes":[1,2,3]},None)
        self.assertEqual(chosen["episodes"],[2])

    def test_empty_success_scope_and_derived_sources_are_rejected(self):
        with self.assertRaises(ValueError):motion.prepare_job(self.cfg,{"mode":"motion_preview","dataset_root":str(self.root),"episodes":[1]},None)
        atomic_json(self.root/"crop_provenance.json",{})
        with self.assertRaises(ValueError):motion.prepare_job(self.cfg,{"mode":"motion_preview","dataset_root":str(self.root)},None)

    def test_recropping_preserves_custom_display_description_but_requires_new_training_review(self):
        from crop_collection import plan_crop
        child=self.home/"old-crop";child.mkdir()
        spec={**naming.simple_defaults(),"stage":"processed","review":"pending"}
        atomic_json(child/reviews.COLLECTION_FILE,{"schema_version":1,"operation":"crop_collection",
            "source":str(self.root),"dataset_spec":spec,"episodes":[{"episode_index":0,"source_episode":0}]})
        saved=naming.store_metadata(child,{**spec,"description":"位置变化清洗版","review":"train"},review_confirmed=True)
        result=plan_crop(self.cfg,self.root,0)
        self.assertEqual(result["destination"],str(child))
        self.assertEqual(result["dataset_spec"]["description"],"位置变化清洗版")
        self.assertEqual(result["dataset_spec"]["review"],"pending")
        self.assertEqual(naming.describe(child)["dataset_id"],saved["dataset_id"])

    def preview(self):
        request=motion.prepare_job(self.cfg,{"mode":"motion_preview","dataset_root":str(self.root)},None)
        out=self.base/"logs/preview";out.mkdir(parents=True)
        plan={"schema_version":1,"operation":"motion_trim_plan","dataset_root":str(self.root),
              "collection_plan":request["collection_plan"],"settings":motion.settings(),"evidence":{},
              "rows":[{"episode":0,"status":"ready","start":0.,"end":1.},{"episode":2,"status":"skip","reason":"static"}]}
        atomic_json(out/"motion_plan.json",plan)
        return {"id":"preview","output":str(out),"finished":True,"result":{"outcome":"motion_preview"}}

    def test_apply_requires_own_completed_plan_ready_rows_and_current_success(self):
        job=self.preview();payload={"mode":"motion_apply","dataset_root":str(self.root),"preview_job_id":"preview","episodes":[0]}
        result=motion.prepare_job(self.cfg,payload,job);self.assertEqual(result["plan"]["rows"][0]["episode"],0)
        with self.assertRaises(ValueError):motion.prepare_job(self.cfg,{**payload,"episodes":[2]},job)
        with self.assertRaises(ValueError):motion.prepare_job(self.cfg,{**payload,"preview_job_id":"stale"},job)
        reviews.save_review(self.root,0,"failure")
        with self.assertRaises(ValueError):motion.prepare_job(self.cfg,payload,job)

    def test_large_request_uses_a_file_not_unbounded_worker_stdin(self):
        out=self.base/"logs/large";out.mkdir(parents=True)
        payload={"mode":"motion_preview","dataset_root":str(self.root),"episodes":list(range(20000))}
        small=motion.materialize(payload,out)
        self.assertLess(len(json.dumps(small)),2000)
        self.assertEqual(len(json.loads(Path(small["request_file"]).read_text())["episodes"]),20000)

    def test_source_fingerprint_detects_changed_numeric_files(self):
        path=self.root/"data.parquet";path.write_bytes(b"original")
        proof=motion.evidence(self.root,[path]);motion.verify_evidence(self.root,proof)
        path.write_bytes(b"changed!")
        with self.assertRaises(ValueError):motion.verify_evidence(self.root,proof)

    def test_stop_cancels_verification(self):
        path=self.root/"data.parquet";path.write_bytes(b"unchanged")
        proof=motion.evidence(self.root,[path]);stop=threading.Event();stop.set()
        with self.assertRaises(InterruptedError):motion.verify_evidence(self.root,proof,stop=stop)

    def test_server_preview_transport_only_spawns_one_offline_worker_with_file_request(self):
        from server import Platform
        with patch("server.load_config",return_value=self.cfg):app=Platform(offline=True)
        process=Mock();process.pid=123;process.poll.return_value=0
        with patch("server.subprocess.Popen",return_value=process) as spawn,patch("server.threading.Thread"):
            app.start({"kind":"crop","options":{"mode":"motion_preview","dataset_root":str(self.root)}})
        spawn.assert_called_once()
        message=json.loads(process.stdin.write.call_args.args[0])
        self.assertEqual(message["kind"],"crop")
        self.assertNotIn("episodes",message["options"])
        request=json.loads(Path(message["options"]["request_file"]).read_text(encoding="utf-8"))
        self.assertEqual(request["episodes"],[0,2])
        self.assertFalse(any(self.home.glob("*/crop_collection.json")))


class BatchHttpTests(unittest.TestCase):
    def setUp(self):
        ReviewAndPlanTests.setUp(self)
        from server import Platform, Handler, ThreadingHTTPServer
        from urllib.request import build_opener, ProxyHandler
        with patch("server.load_config",return_value=self.cfg): self.app=Platform(offline=True)
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler);self.server.app=self.app
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.url="http://127.0.0.1:"+str(self.server.server_port)
        self.opener=build_opener(ProxyHandler({}))

    def post(self,value,token=True):
        from urllib.request import Request
        headers={"Content-Type":"application/json","Origin":self.url,"X-SO101-Build":self.app.deployment.build}
        if token:headers["X-SO101-Token"]=self.app.token
        # Authentication runs before body parsing; avoid an unread-body TCP reset on Windows.
        body=json.dumps(value).encode() if token else b""
        return self.opener.open(Request(self.url+"/api/annotations/batch",data=body,headers=headers),timeout=4)

    def test_http_batch_marks_need_token_and_do_not_spawn_devices(self):
        from urllib.error import HTTPError
        request={"root":str(self.root),"episodes":[0,1,2],"result":"exclude"}
        with self.assertRaises(HTTPError):self.post(request,token=False)
        with patch("server.subprocess.Popen") as spawn:
            with self.post(request) as response:
                result=json.load(response)
            spawn.assert_not_called()
        self.assertEqual(result["changed"],3);self.assertEqual(result["annotations"]["1"]["note"],"keep note 1")

    def test_http_batch_marks_are_blocked_during_crop_transaction(self):
        from urllib.error import HTTPError
        self.app.proc=Mock();self.app.proc.poll.return_value=None
        self.app.job={"kind":"crop","id":"test","phase":"CROPPING"}
        before=reviews.annotation_path(self.root).read_bytes()
        with self.assertRaises(HTTPError):self.post({"root":str(self.root),"episodes":[0],"result":"failure"})
        self.assertEqual(reviews.annotation_path(self.root).read_bytes(),before)

    def test_batch_ui_asset_is_available_offline(self):
        with self.opener.open(self.url+"/dataset_batch.js") as response:
            self.assertIn(b"installDatasetBatch",response.read())

    def test_crop_view_batch_mark_requires_current_mapping_revision(self):
        from urllib.error import HTTPError
        child=self.home/"cropped";child.mkdir()
        atomic_json(child/reviews.COLLECTION_FILE,{"schema_version":1,"operation":"crop_collection",
            "source":str(self.root),"episodes":[{"episode_index":0,"source_episode":2}]})
        request={"root":str(child),"episodes":[0],"result":"failure"}
        with self.assertRaises(HTTPError):self.post(request)
        request["crop_revision"]=reviews.review_revision(child)
        with self.post(request) as response:self.assertEqual(json.load(response)["changed"],1)
        self.assertEqual(reviews.read_reviews(self.root)["2"]["result"],"failure")


if __name__ == "__main__":unittest.main()
