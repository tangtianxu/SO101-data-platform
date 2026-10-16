"""Offline contract tests. No serial/camera/model connections are permitted."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import HERE, atomic_json, load_config, robot_path, validate_config
from server import Handler, Platform, ThreadingHTTPServer


class ConfigTests(unittest.TestCase):
    def test_config_uses_existing_e_drive(self):
        cfg=load_config()
        self.assertEqual(Path(cfg['python']).drive.upper(),'E:')
        self.assertFalse(cfg['rerun'])

    def test_no_camera_alias(self):
        with self.assertRaises(ValueError):validate_config({'top_camera':1,'wrist_camera':1})

    def test_no_silent_identity_change(self):
        with self.assertRaises(ValueError):validate_config({'follower_usb_serial':'other'})

    def test_no_c_drive_target(self):
        with self.assertRaises(ValueError):robot_path('C:/Users/USER/Desktop')

    def test_root_is_not_an_output(self):
        with self.assertRaises(ValueError):robot_path('E:/robot')

    def test_nan_and_bool_not_allowed(self):
        for value in (float('nan'),True,1000):
            with self.assertRaises(ValueError):validate_config({'preview_hz':value})

    def test_no_unknown_settings(self):
        with self.assertRaises(ValueError):validate_config({'pid':999})

    def test_no_inference_contract_expansion(self):
        with self.assertRaises(ValueError):validate_config({'act_seconds':20})

    def test_atomic_json_failure_preserves_original(self):
        (HERE/'state/tests').mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=HERE/'state/tests') as folder:
            file=Path(folder)/'test.json';atomic_json(file,{'original':True})
            with self.assertRaises(ValueError):atomic_json(file,{'bad':float('nan')})
            self.assertEqual(json.loads(file.read_text()),{'original':True})


class RuntimeTests(unittest.TestCase):
    def test_offline_blocks_all_hardware_before_spawn(self):
        app=Platform(offline=True)
        with patch('server.subprocess.Popen') as spawn:
            for kind in ('preview','camera_scan','monitor','control','record','teleop','act','replay'):
                with self.assertRaises(PermissionError):app.start({'kind':kind})
            spawn.assert_not_called()

    def test_import_worker_does_not_load_devices_or_model(self):
        import subprocess
        process=subprocess.run([sys.executable,'-s','-B','-X','utf8','-c',
            "import worker,sys; assert callable(worker.main); assert 'so101_guarded_real_act' not in sys.modules; assert 'cv2' not in sys.modules"],cwd=HERE,capture_output=True,text=True)
        self.assertEqual(process.returncode,0,process.stderr)

    def test_stale_session_command_rejected(self):
        app=Platform(offline=True);app.proc=Mock();app.proc.poll.return_value=None
        app.job={'id':'current'}
        with self.assertRaises(ValueError):app.command({'command':'stop','job_id':'old'})
        app.proc.stdin.write.assert_not_called()

    def test_unconfirmed_motion_rejected(self):
        app=Platform(offline=True)
        for command in ('scene','start','hold','release','jog','reset'):
            with self.assertRaises(ValueError):app.command({'command':command})

    def test_offline_blocks_confirmed_device_commands(self):
        app=Platform(offline=True)
        with self.assertRaises(PermissionError):app.command({'command':'hold','confirmed':True})


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=Platform(offline=True)
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);cls.server.app=cls.app
        cls.url='http://127.0.0.1:'+str(cls.server.server_port)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()

    def test_page_and_static_assets(self):
        for path in ('/','/app.js','/style.css'):
            with urlopen(self.url+path) as res:
                content=res.read();self.assertEqual(res.status,200);self.assertGreater(len(content),100)
                if path=='/':self.assertIn(self.app.token.encode(),content)

    def test_snapshot_is_offline_and_no_worker(self):
        with urlopen(self.url+'/api/state') as res:state=json.load(res)
        self.assertTrue(state['offline']);self.assertFalse(state['active'])

    def test_mutation_requires_origin_and_token(self):
        req=Request(self.url+'/api/jobs',data=b'{"kind":"control"}',headers={'Content-Type':'application/json'})
        with self.assertRaises(HTTPError) as failure:urlopen(req)
        self.assertEqual(failure.exception.code,403)

    def test_even_authorized_ui_cannot_bypass_offline_lock(self):
        req=Request(self.url+'/api/jobs',data=b'{"kind":"preview"}',headers={
            'Content-Type':'application/json','Origin':self.url,'X-SO101-Token':self.app.token})
        with self.assertRaises(HTTPError) as failure:urlopen(req)
        self.assertEqual(failure.exception.code,403)

    def test_path_traversal_not_served(self):
        with self.assertRaises(HTTPError):urlopen(self.url+'/../config.json')

    def test_ports_not_enumerated_in_offline(self):
        with urlopen(self.url+'/api/ports') as res:self.assertEqual(json.load(res),[])

    def test_reset_profile_is_available_offline_without_device_worker(self):
        with urlopen(self.url+'/api/reset-profile') as res:pose=json.load(res)
        self.assertEqual(len(pose['positions']),6)
        self.assertEqual(pose['effective_speeds'],[4.,3.2,2.4,1.6,.8,10.])
        self.assertFalse(pose['collision_checked'])
        self.assertFalse(self.app.active())

    def test_joint_profile_and_build_identity_require_no_device(self):
        with urlopen(self.url+'/api/joint-control-profile') as res:profile=json.load(res)
        self.assertEqual(len(profile['joints']),6);self.assertEqual(profile['joints'][-1]['unit'],'%')
        with urlopen(self.url+'/api/identity') as res:identity=json.load(res)
        self.assertEqual(identity['build'],self.app.deployment.build)
        with urlopen(self.url+'/joint_control.js?build='+identity['build']) as res:self.assertEqual(res.status,200)
        with self.assertRaises(HTTPError):urlopen(self.url+'/joint_control.js?build=old')

    def test_pinned_assets_do_not_mix_new_disk_content(self):
        expected=self.app.deployment.assets['app.js']
        with patch.object(Path,'read_bytes',return_value=b'wrong new asset'):
            with urlopen(self.url+'/app.js') as res:self.assertEqual(res.read(),expected)

    def test_wrong_build_cannot_unlock_hardware(self):
        req=Request(self.url+'/api/hardware-mode',data=b'{"offline":false,"confirmed":true}',headers={
            'Content-Type':'application/json','Origin':self.url,'X-SO101-Token':self.app.token,'X-SO101-Build':'wrong'})
        with self.assertRaises(HTTPError) as failure:urlopen(req)
        self.assertEqual(failure.exception.code,403);self.assertTrue(self.app.offline)


if __name__=='__main__':unittest.main(verbosity=2)
