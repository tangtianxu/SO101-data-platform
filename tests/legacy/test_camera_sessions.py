"""Fake processes and HTTP only: never import or connect to camera/servo APIs."""
import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import HERE
from server import Handler,Platform,ThreadingHTTPServer


def process(pid):
    proc=Mock(pid=pid)
    proc.poll.return_value=None
    proc.stdin=Mock()
    return proc


class CameraSessions(unittest.TestCase):
    def setUp(self):
        (HERE/'state/tests').mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=HERE/'state/tests')
        self.app=Platform(offline=False)
        self.app.config['log_home']=self.temp.name
        self.spawn=patch('server.subprocess.Popen',side_effect=lambda *a,**kw:process(7000+self.spawn_mock.call_count))
        self.spawn_mock=self.spawn.start()
        self.thread=patch('server.threading.Thread')
        self.thread.start()

    def tearDown(self):
        self.thread.stop();self.spawn.stop();self.temp.cleanup()

    def start_pair(self,kind='monitor',camera_first=True):
        for name in (('preview',kind) if camera_first else (kind,'preview')):
            self.app.start({'kind':name})
        return self.app.proc,self.app.preview_proc

    def test_preview_and_monitor_or_control_work_in_both_orders(self):
        for kind in ('monitor','control'):
            for camera_first in (True,False):
                with self.subTest(kind=kind,camera_first=camera_first):
                    self.app=Platform(offline=False);self.app.config['log_home']=self.temp.name
                    self.start_pair(kind,camera_first)
                    snap=self.app.snapshot()
                    self.assertTrue(snap['active'] and snap['preview_active'] and snap['resources_active'])
                    self.assertEqual(snap['job']['kind'],kind)
                    self.assertEqual(snap['preview_job']['kind'],'preview')
                    self.assertEqual(snap['camera_owner_id'],snap['preview_job']['id'])
                    self.assertNotEqual(self.app.proc,self.app.preview_proc)

    def test_camera_start_does_not_clear_robot_health(self):
        self.app.start({'kind':'control'})
        self.app.telemetry={'state':[1,2,3,4,5,6],'health':{'temperature':{'elbow_flex':31}}}
        before=self.app.telemetry.copy()
        self.app.start({'kind':'preview'})
        self.assertEqual(self.app.telemetry,before)

    def test_monitor_start_does_not_clear_camera_frames(self):
        camera=self.app.start({'kind':'preview'})
        self.app.frames={'top':(b'frame',time.time())};self.app.frame_owner=camera['id']
        self.app.start({'kind':'monitor'})
        self.assertEqual(self.app.frames['top'][0],b'frame')
        self.assertEqual(self.app.frame_owner,camera['id'])

    def test_stop_camera_does_not_touch_robot_stdin(self):
        robot,camera=self.start_pair('control')
        robot.stdin.reset_mock();camera.stdin.reset_mock()
        self.app.command({'command':'stop_preview','job_id':self.app.preview_job['id']})
        robot.stdin.write.assert_not_called()
        self.assertEqual(json.loads(camera.stdin.write.call_args.args[0])['command'],'stop')
        self.assertTrue(self.app.active())

    def test_disconnect_robot_does_not_close_cameras(self):
        robot,camera=self.start_pair()
        robot.stdin.reset_mock();camera.stdin.reset_mock()
        self.app.command({'command':'stop_device','job_id':self.app.job['id']})
        camera.stdin.write.assert_not_called()
        self.assertEqual(json.loads(robot.stdin.write.call_args.args[0])['command'],'stop')
        self.assertTrue(self.app.preview_active())

    def test_global_stop_stops_both_without_torque_writes(self):
        robot,camera=self.start_pair('control')
        robot.stdin.reset_mock();camera.stdin.reset_mock()
        self.app.command({'command':'stop','job_id':self.app.job['id']})
        for proc in (robot,camera):
            message=json.loads(proc.stdin.write.call_args.args[0])
            self.assertEqual(set(message),{'command','job_id'})
            self.assertEqual(message['command'],'stop')

    def test_preview_only_global_stop(self):
        job=self.app.start({'kind':'preview'})
        self.assertFalse(self.app.active());self.assertTrue(self.app.resources_active())
        self.app.command({'command':'stop','job_id':job['id']})
        self.assertTrue(self.app.preview_job['stopping'])

    def test_stale_preview_stop_rejected(self):
        self.start_pair()
        self.app.preview_proc.stdin.reset_mock()
        with self.assertRaises(ValueError):self.app.command({'command':'stop_preview','job_id':'old'})
        self.app.preview_proc.stdin.write.assert_not_called()

    def test_no_double_camera_owner_or_second_robot_session(self):
        self.app.start({'kind':'preview'})
        for kind in ('preview','camera_scan','record','teleop','act','replay','crop'):
            with self.assertRaises(RuntimeError):self.app.start({'kind':kind})
        self.app.start({'kind':'control'})
        with self.assertRaises(RuntimeError):self.app.start({'kind':'monitor'})
        self.assertEqual(self.spawn_mock.call_count,2)

    def test_task_owned_camera_cannot_be_opened_twice(self):
        for kind in ('act','record','teleop','replay','crop'):
            self.app.proc=process(99);self.app.job={'id':'task','kind':kind}
            with self.assertRaises(RuntimeError):self.app.start({'kind':'preview'})
        self.spawn_mock.assert_not_called()

    def test_camera_failure_isolated_from_robot_telemetry(self):
        robot,camera=self.start_pair('control')
        self.app.telemetry={'state':[1,2,3,4,5,6]}
        camera.stdout=io.StringIO('SO101_EVENT '+json.dumps({'type':'error','message':'camera failed'})+'\n'+
            'SO101_EVENT '+json.dumps({'type':'done','outcome':'error'})+'\n')
        def finish():camera.poll.return_value=1;return 1
        camera.wait.side_effect=finish
        self.app._read(camera,self.app.preview_job,Path(self.app.preview_job['output']))
        self.assertTrue(self.app.active());self.assertFalse(self.app.preview_active())
        self.assertEqual(self.app.telemetry,{'state':[1,2,3,4,5,6]})
        self.assertEqual(self.app.preview_job['phase'],'ERROR')

    def test_frames_from_preview_are_not_written_to_health(self):
        robot,camera=self.start_pair()
        self.app.telemetry={'state':[0]*6}
        camera.stdout=io.StringIO('SO101_EVENT '+json.dumps({'type':'frame','camera':'top','data':base64.b64encode(b'jpeg-data').decode()})+'\n'+
            'SO101_EVENT '+json.dumps({'type':'done','outcome':'preview_stopped'})+'\n')
        def finish():camera.poll.return_value=0;return 0
        camera.wait.side_effect=finish
        self.app._read(camera,self.app.preview_job,Path(self.app.preview_job['output']))
        self.assertEqual(self.app.frames['top'][0],b'jpeg-data')
        self.assertEqual(self.app.frame_owner,self.app.preview_job['id'])
        self.assertEqual(self.app.telemetry,{'state':[0]*6})


class CameraHTTP(unittest.TestCase):
    def setUp(self):
        self.app=Platform(offline=True)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.app=self.app
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.app.preview_proc=None
        self.server.shutdown();self.server.server_close();self.thread.join()

    def post(self,path,payload):
        return urlopen(Request(self.url+path,data=json.dumps(payload).encode(),headers={
            'Content-Type':'application/json','Origin':self.url,'X-SO101-Token':self.app.token,
            'X-SO101-Build':self.app.deployment.build}))

    def test_csp_allows_blob_images_only_not_remote_or_inline_code(self):
        with urlopen(self.url) as response:
            csp=response.headers['Content-Security-Policy']
            self.assertIn("img-src 'self' data: blob:",csp)
            self.assertIn("script-src 'self';",csp)
            self.assertNotIn('unsafe-inline',csp)

    def test_no_stale_camera_session_image(self):
        self.app.frames['top']=(b'jpeg-test',time.time());self.app.frame_owner='current'
        with urlopen(self.url+'/frame/top?session=old') as response:self.assertEqual(response.status,204)
        with urlopen(self.url+'/frame/top?session=current') as response:
            self.assertEqual(response.read(),b'jpeg-test');self.assertEqual(response.headers['Content-Type'],'image/jpeg')

    def test_camera_only_blocks_shutdown_config_and_offline_mode_change(self):
        self.app.preview_proc=process(555);self.app.preview_job={'id':'cam','kind':'preview'}
        for path,payload in (('/api/shutdown',{}),('/api/config',{}),('/api/hardware-mode',{'offline':False,'confirmed':True})):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as error:self.post(path,payload)
                self.assertEqual(error.exception.code,409)


if __name__=='__main__':unittest.main(verbosity=2)
