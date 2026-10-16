"""Self-contained portable contracts. No camera, servo, model or real USB inventory."""
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import common
from common import NAMES, HERE, ROBOT_HOME, atomic_json, capabilities, read_calibration, require_capability, validate_config
from server import Platform,Handler,ThreadingHTTPServer
import reset_profile
import software_limits
import joint_control


class Fixture(unittest.TestCase):
    def setUp(self):
        (ROBOT_HOME/'temp').mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='portable_test_',dir=ROBOT_HOME/'temp')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.local=patch('common._local_settings',return_value={});self.local.start();self.addCleanup(self.local.stop)
        self.state=patch('server.STATE_DIR',self.root/'state');self.state.start();self.addCleanup(self.state.stop)
        self.cal={n:{'id':i+1,'drive_mode':0,'homing_offset':0,'range_min':1000,'range_max':3000} for i,n in enumerate(NAMES)}
        self.follower=self.root/'config/自己的从臂.json';self.leader=self.root/'config/自己的主臂.json'
        atomic_json(self.follower,self.cal);atomic_json(self.leader,self.cal)
        self.cfg=validate_config({'dataset_home':str(self.root/'datasets'),'log_home':str(self.root/'logs'),
            'feedback_home':str(self.root/'feedback'),'python':sys.executable})

    def bound(self):
        return {**self.cfg,'follower_usb_serial':'TEST_FOLLOWER','leader_usb_serial':'TEST_LEADER',
            'calibration':str(self.follower),'leader_calibration':str(self.leader)}

    def app(self,cfg=None,offline=True):
        with patch('server.load_config',return_value=copy.deepcopy(cfg or self.cfg)):
            return Platform(offline=offline)


class ConfigurationTests(Fixture):
    def test_first_run_needs_no_model_calibration_or_binding(self):
        app=self.app()
        snap=app.snapshot()
        self.assertFalse(snap['active']);self.assertTrue(snap['offline'])
        self.assertTrue(snap['capabilities']['crop']['ready'])
        self.assertFalse(snap['capabilities']['act']['ready'])

    def test_preview_does_not_require_robot_or_checkpoint(self):require_capability(self.cfg,'preview')
    def test_crop_does_not_require_robot_or_checkpoint(self):require_capability(self.cfg,'crop')
    def test_monitor_only_requires_follower(self):require_capability({**self.cfg,'calibration':str(self.follower),'follower_usb_serial':'F'},'monitor')
    def test_record_requires_both_arms_but_not_model_or_start(self):require_capability(self.bound(),'record')
    def test_teleop_requires_both_arms_but_not_model_or_start(self):require_capability(self.bound(),'teleop')
    def test_missing_identity_rejected_before_hardware(self):
        with self.assertRaisesRegex(ValueError,'从臂'):require_capability(self.cfg,'control')
    def test_missing_leader_rejected(self):
        cfg=self.bound();cfg['leader_usb_serial']=''
        with self.assertRaisesRegex(ValueError,'主臂'):require_capability(cfg,'record')
    def test_duplicate_roles_rejected(self):
        with self.assertRaisesRegex(ValueError,'同一个'):validate_config({'leader_usb_serial':'same','follower_usb_serial':'same'})
    def test_duplicate_explicit_ports_rejected(self):
        with self.assertRaises(ValueError):validate_config({'leader_port':'com9','follower_port':'COM9'})
    def test_manual_port_case_is_normalized_without_guessing(self):
        cfg=validate_config({'follower_port':'com33','leader_port':'Auto'})
        self.assertEqual(cfg['follower_port'],'COM33');self.assertEqual(cfg['leader_port'],'auto')
    def test_placeholder_identity_rejected(self):
        with self.assertRaises(ValueError):validate_config({'follower_usb_serial':'SET_FOLLOWER_USB_SERIAL'})
    def test_invalid_camera_alias_rejected(self):
        with self.assertRaises(ValueError):validate_config({'top_camera':2,'wrist_camera':2})
    def test_custom_camera_indices(self):self.assertEqual(validate_config({'top_camera':3,'wrist_camera':7})['wrist_camera'],7)
    def test_unknown_config_rejected(self):
        with self.assertRaises(ValueError):validate_config({'arbitrary_code':'x'})
    def test_nonfinite_or_boolean_rejected(self):
        for value in (True,float('nan'),float('inf')):
            with self.subTest(value=value),self.assertRaises(ValueError):validate_config({'preview_hz':value})
    def test_distinct_output_directories_required(self):
        with self.assertRaisesRegex(ValueError,'重叠'):validate_config({**self.cfg,'log_home':self.cfg['dataset_home']})
    def test_nested_output_directories_rejected(self):
        with self.assertRaises(ValueError):validate_config({**self.cfg,'feedback_home':str(Path(self.cfg['dataset_home'])/'feedback')})
    def test_workspace_root_cannot_be_archive_target(self):
        with self.assertRaises(ValueError):common.robot_path(ROBOT_HOME)
    def test_output_escape_rejected(self):
        with self.assertRaises(ValueError):common.robot_path(ROBOT_HOME.parent/'outside')
    def test_readonly_inputs_can_be_outside_output_workspace(self):self.assertEqual(common.input_path(sys.executable),Path(sys.executable).resolve())
    def test_arbitrary_workspace_not_fixed_to_e_robot(self):
        custom=Path(self.temp.name)/'另一个 工作区'
        cfg=validate_config({'workspace_root':str(custom),'dataset_home':'datasets','log_home':'logs','feedback_home':'feedback'})
        self.assertEqual(cfg['dataset_home'],str(custom/'datasets'))
    def test_relative_resource_resolves_against_new_workspace(self):
        custom=Path(self.temp.name)/'new'
        cfg=validate_config({'workspace_root':str(custom),'dataset_home':'datasets','log_home':'logs','feedback_home':'feedback','calibration':'config/follower.json'})
        self.assertEqual(cfg['calibration'],str(custom/'config/follower.json'))
    @unittest.skipUnless(os.name=='nt','Windows drive semantics')
    def test_configuration_accepts_a_non_e_drive_without_io(self):
        cfg=validate_config({'workspace_root':'Z:/so101-test-workspace','dataset_home':'datasets','log_home':'logs','feedback_home':'feedback'})
        self.assertEqual(Path(cfg['workspace_root']).drive.upper(),'Z:')
    def test_external_adapter_copy_is_not_silently_loaded(self):
        with self.assertRaisesRegex(ValueError,'混用控制代码'):validate_config({'shared_scripts':str(self.root/'old_scripts')})
    def test_empty_list_cannot_be_saved_as_software_limits(self):
        with self.assertRaises(ValueError):validate_config({'manual_joint_limits':[]})
    def test_symlink_cannot_escape_managed_workspace(self):
        link=self.root/'outside_link'
        try:link.symlink_to(ROBOT_HOME.parent,target_is_directory=True)
        except OSError:self.skipTest('Symlink permission unavailable; no privileges requested')
        self.addCleanup(link.unlink)
        with self.assertRaises(ValueError):common.robot_path(link/'escape')
    def test_environment_cache_and_temp_are_workspace_scoped(self):
        with patch.dict(os.environ):
            env=common.environment(self.cfg)
            self.assertTrue(Path(env['TEMP']).is_relative_to(ROBOT_HOME));self.assertEqual(env['SO101_WORKSPACE'],str(ROBOT_HOME))
    def test_calibration_requires_six_distinct_ids(self):
        value=copy.deepcopy(self.cal);value[NAMES[1]]['id']=value[NAMES[0]]['id'];atomic_json(self.follower,value)
        with self.assertRaises(ValueError):read_calibration(self.follower)
    def test_calibration_missing_joint_rejected(self):
        value=copy.deepcopy(self.cal);del value['gripper'];atomic_json(self.follower,value)
        with self.assertRaises(ValueError):read_calibration(self.follower)
    def test_calibration_invalid_range_rejected(self):
        value=copy.deepcopy(self.cal);value['gripper']['range_min']=3500;atomic_json(self.follower,value)
        with self.assertRaises(ValueError):read_calibration(self.follower)
    def test_bad_calibration_path_is_feature_specific(self):
        cfg={**self.cfg,'calibration':str(self.root/'missing.json'),'follower_usb_serial':'F'}
        validate_config(cfg);self.assertFalse(capabilities(cfg)['control']['ready']);require_capability(cfg,'preview')
    def test_act_requires_model_files(self):
        with self.assertRaises(ValueError):require_capability(self.bound(),'act')
    def test_initial_task_pose_can_be_created_without_old_pose(self):
        cfg=self.bound();pose=reset_profile.save_pose_copy(cfg,dict.fromkeys(NAMES,0.))
        self.assertTrue(Path(pose['path']).is_relative_to(ROBOT_HOME));self.assertFalse(pose['collision_checked'])
        Path(pose['path']).unlink()
    def test_new_calibration_cannot_reuse_old_pose(self):
        cfg=self.bound();pose=reset_profile.save_pose_copy(cfg,dict.fromkeys(NAMES,0.))
        self.addCleanup(Path(pose['path']).unlink)
        wrong=copy.deepcopy(self.cal);wrong[NAMES[0]]['homing_offset']=5;atomic_json(self.follower,wrong)
        with self.assertRaises(ValueError):reset_profile.inspect_pose(cfg,pose['path'])
    def test_software_limits_cannot_expand_calibration(self):
        bounds=software_limits.calibrated(self.cal)
        proposed={n:{'min':v[0],'max':v[1]} for n,v in bounds.items()};proposed[NAMES[0]]['max']+=5
        with self.assertRaises(ValueError):software_limits.checked(proposed,self.cal)
    def test_custom_joint_profile_has_no_fixed_pose(self):
        value=joint_control.profile(self.bound());self.assertEqual(len(value['joints']),6)
        self.assertEqual(value['calibration_path'],str(self.follower))


class BindingTests(Fixture):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0,str(HERE/'runtime/shared_scripts'))
        import so101_profile
        cls.profile=so101_profile

    def test_custom_ids_and_changed_com_numbers(self):
        with patch.dict(os.environ):
            self.profile.configure(self.bound())
            ports=[types.SimpleNamespace(device='COM43',serial_number='TEST_FOLLOWER')]
            self.assertEqual(self.profile.resolve_port('follower',ports),'COM43')
    def test_wrong_role_does_not_fallback_to_other_port(self):
        with patch.dict(os.environ):
            self.profile.configure(self.bound())
            with self.assertRaises(RuntimeError):self.profile.resolve_port('follower',[types.SimpleNamespace(device='COM43',serial_number='TEST_LEADER')])
    def test_duplicate_usb_serial_rejected(self):
        with patch.dict(os.environ):
            self.profile.configure(self.bound())
            ports=[types.SimpleNamespace(device=p,serial_number='TEST_FOLLOWER') for p in ('COM2','COM3')]
            with self.assertRaises(RuntimeError):self.profile.resolve_port('follower',ports)
    def test_explicit_port_still_checks_identity(self):
        with patch.dict(os.environ):
            self.profile.configure(self.bound())
            with self.assertRaises(RuntimeError):self.profile.resolve_port('follower',[types.SimpleNamespace(device='COM43',serial_number='TEST_FOLLOWER')],'COM44')
    def test_blank_serial_never_matches_blank_device_serial(self):
        with patch.dict(os.environ):
            self.profile.configure(self.cfg)
            with self.assertRaises(ValueError):self.profile.resolve_port('follower',[types.SimpleNamespace(device='COM1',serial_number='')])
    def test_worker_resolver_uses_configured_ids(self):
        import worker
        listing=types.ModuleType('serial.tools.list_ports');listing.comports=lambda:[types.SimpleNamespace(device='COM55',serial_number='TEST_FOLLOWER')]
        with patch.dict(sys.modules,{'serial':types.ModuleType('serial'),'serial.tools':types.ModuleType('serial.tools'),'serial.tools.list_ports':listing}):
            self.assertEqual(worker.ports_for(self.bound()),{'follower':'COM55'})
    def test_bindings_need_explicit_confirmation(self):
        app=self.app()
        with self.assertRaisesRegex(ValueError,'明确确认'):app.save_config(self.bound())
    def test_save_binding_writes_only_test_state_and_does_not_spawn(self):
        app=self.app()
        original=self.follower.read_bytes()
        with patch('server.subprocess.Popen') as spawn:
            saved=app.save_config({**self.bound(),'_binding_confirmed':True});spawn.assert_not_called()
        self.assertFalse(saved['_restart_required']);self.assertEqual(self.follower.read_bytes(),original)
        self.assertTrue((self.root/'state/settings.json').is_file())
    def test_config_locked_during_preview_and_control(self):
        for attr in ('proc','preview_proc'):
            app=self.app();proc=Mock();proc.poll.return_value=None;setattr(app,attr,proc)
            with self.assertRaises(RuntimeError):app.save_config(self.cfg)
    def test_workspace_change_requires_restart_and_blocks_new_jobs(self):
        app=self.app();new=self.root/'changed'
        value={**self.cfg,'workspace_root':str(new),'dataset_home':str(new/'datasets'),'log_home':str(new/'logs'),'feedback_home':str(new/'feedback')}
        saved=app.save_config(value);self.assertTrue(saved['_restart_required'])
        with patch('server.subprocess.Popen') as spawn:
            with self.assertRaises(RuntimeError):app.start({'kind':'preview'})
            spawn.assert_not_called()
    def test_changing_calibration_clears_stale_limit_and_start_links(self):
        cfg=self.bound();cfg['reference_start_pose']='old-reference'
        app=self.app(cfg);other=self.root/'config/new.json';atomic_json(other,self.cal)
        saved=app.save_config({'calibration':str(other),'_binding_confirmed':True})
        self.assertEqual(saved['manual_joint_limits'],{});self.assertEqual(saved['reference_start_pose'],'')
    def test_new_device_gripper_exception_is_not_enabled_implicitly(self):self.assertEqual(self.cfg['gripper_overload_policy'],'stop')
    def test_offline_blocks_hardware_even_if_configured(self):
        app=self.app(self.bound())
        with patch('server.subprocess.Popen') as spawn:
            for kind in ('preview','monitor','control','record','teleop','act','replay','camera_scan'):
                with self.subTest(kind=kind),self.assertRaises(PermissionError):app.start({'kind':kind})
            spawn.assert_not_called()
    def test_online_missing_config_rejected_before_worker(self):
        app=self.app(offline=False)
        with patch('server.subprocess.Popen') as spawn:
            with self.assertRaises(ValueError):app.start({'kind':'control'})
            spawn.assert_not_called()


class HttpTests(Fixture):
    def setUp(self):
        super().setUp();self.platform=self.app()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.app=self.platform
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.opener=build_opener(ProxyHandler({}))

    def get(self,path):return self.opener.open(self.base+path,timeout=4)
    def post(self,path,value,token=True):
        headers={'Content-Type':'application/json','Origin':self.base,'X-SO101-Build':self.platform.deployment.build}
        if token:headers['X-SO101-Token']=self.platform.token
        return self.opener.open(Request(self.base+path,data=json.dumps(value).encode(),headers=headers),timeout=4)
    def test_fresh_page_and_portable_module_served(self):
        with self.get('/') as response:self.assertIn(b'portable.js',response.read())
        with self.get('/portable.js') as response:self.assertIn(b'initPortableSettings',response.read())
        with self.get('/capture_groups.js') as response:self.assertIn(b'captureGroupPlan',response.read())
    def test_collection_listing_does_not_start_any_device(self):
        with patch('server.subprocess.Popen') as spawn:
            with self.get('/api/capture-collections') as response:self.assertEqual(json.load(response),[])
            spawn.assert_not_called()
    def test_state_shows_missing_features_without_boot_failure(self):
        with self.get('/api/state') as response:value=json.load(response)
        self.assertFalse(value['resources_active']);self.assertIn('capabilities',value)
    def test_settings_can_be_saved_without_model_or_reference(self):
        with self.post('/api/config',self.cfg) as response:self.assertEqual(json.load(response)['checkpoint'],'')
    def test_config_save_requires_token(self):
        with self.assertRaises(HTTPError) as result:self.post('/api/config',self.cfg,token=False)
        self.assertEqual(result.exception.code,403)
    def test_inventory_endpoint_does_not_open_serial(self):
        with patch.object(self.platform,'inventory',return_value=[]) as inspect:
            with self.get('/api/ports') as response:self.assertEqual(json.load(response),[])
            inspect.assert_called_once_with(ports=True)
    def test_environment_endpoint_does_not_warm_model(self):
        with patch.object(self.platform,'inventory',return_value={'hardware_accessed':False}):
            with self.get('/api/environment') as response:self.assertFalse(json.load(response)['hardware_accessed'])
    def test_offline_job_rejected(self):
        with patch('server.subprocess.Popen') as spawn:
            with self.assertRaises(HTTPError):self.post('/api/jobs',{'kind':'control'})
            spawn.assert_not_called()
    def test_stop_remains_available_after_restart_needed(self):
        self.platform.restart_required=True
        with patch.object(self.platform,'command') as stop:
            with self.post('/api/command',{'command':'stop'}) as response:self.assertTrue(json.load(response)['accepted'])
            stop.assert_called_once()


if __name__=='__main__':unittest.main()
