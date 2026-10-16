"""Synthetic sessions and metadata only: no camera, USB, model or real dataset."""
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import ROBOT_HOME,atomic_json
import capture_pause as cp
import review_store as reviews


class PauseTests(unittest.TestCase):
    def session(self,phase='RECORDING'):
        s=SimpleNamespace(phase=phase,stop=threading.Event(),control_lock=threading.RLock(),
            events={'exit_early':False,'stop_recording':False},motion=Mock())
        s.state=lambda value:setattr(s,'phase',value)
        return s,cp.PauseControl(s)

    def test_pause_only_sets_frame_boundary_flag_and_rejects_stale_or_duplicate_requests(self):
        s,c=self.session();self.assertFalse(c.request_pause('RESET'));self.assertFalse(c.request_resume())
        self.assertTrue(c.request_pause('RECORDING'));self.assertTrue(s.events['exit_early'])
        self.assertFalse(c.request_pause('RECORDING'));s.motion.assert_not_called()
        self.assertEqual(s.phase,'RECORDING');self.assertFalse(s.stop.is_set())

    def test_pause_holds_on_owner_thread_and_resumes_only_after_explicit_request(self):
        for phase in ('RECORDING','RESET'):
            s,c=self.session(phase);c.request_pause(phase);order=[]
            def hold():
                self.assertEqual(s.phase,'PAUSED');order.append('hold');self.assertTrue(c.request_resume())
            def catchup():self.assertEqual(s.phase,'RESUMING');order.append('catchup')
            c.wait(hold,catchup);s.motion.anchor_current.assert_called_once()
            self.assertEqual(order,['hold','catchup']);self.assertFalse(c.requested.is_set())

    def test_stop_while_paused_never_runs_catchup(self):
        s,c=self.session();c.request_pause('RECORDING');catchup=Mock()
        with self.assertRaises(KeyboardInterrupt):c.wait(s.stop.set,catchup)
        catchup.assert_not_called()

    def test_health_failure_during_hold_propagates_instead_of_resuming(self):
        s,c=self.session();c.request_pause('RECORDING');catchup=Mock()
        with self.assertRaisesRegex(RuntimeError,'health'):c.wait(Mock(side_effect=RuntimeError('health')),catchup)
        catchup.assert_not_called()

    def test_wait_without_pause_does_not_read_or_write_devices(self):
        s,c=self.session();hold=Mock();catchup=Mock();c.wait(hold,catchup)
        s.motion.anchor_current.assert_not_called();hold.assert_not_called();catchup.assert_not_called()

    def test_worker_pause_uses_same_owner_for_health_hold_and_three_stable_catchup_steps(self):
        from common import NAMES
        from worker import wait_capture_pause
        s,c=self.session();s.pause_control=c;s.frames=12;s.last_obs=dict.fromkeys(NAMES,0.)
        s.motion.last_target=[0.]*6;s.robot=SimpleNamespace(feedback=SimpleNamespace(latest={'health':{}}),limits=None,send_action=Mock())
        def observation():
            if s.phase=='PAUSED':self.assertTrue(c.request_resume())
            return {n+'.pos':0. for n in NAMES}
        s.robot.get_observation=observation;s.leader=SimpleNamespace(get_action=lambda:{})
        rec=SimpleNamespace(clip_target=lambda *_:(None,[0.]*6,None),catchup_step=lambda a,b,dt:b)
        bridge=SimpleNamespace(emit=Mock());c.request_pause('RECORDING');wait_capture_pause(s,rec,bridge)
        s.motion.anchor_current.assert_called_once();s.motion.send.assert_called_once();self.assertEqual(s.robot.send_action.call_count,3)
        self.assertEqual(s.frames,12);self.assertEqual(s.phase,'RESUMING')


class CaptureReviewTests(unittest.TestCase):
    def setUp(self):
        temp=ROBOT_HOME/'temp';temp.mkdir(exist_ok=True,parents=True)
        self.temp=tempfile.TemporaryDirectory(dir=temp);self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        p=patch.object(reviews,'ANNOTATIONS',self.base/'annotations');p.start();self.addCleanup(p.stop)
        self.root=self.base/'raw';(self.root/'meta').mkdir(parents=True)
        atomic_json(self.root/'meta/info.json',{'total_episodes':3});self.original=(self.root/'meta/info.json').read_bytes()
        self.app=SimpleNamespace(job={'id':'job','kind':'record','phase':'PAUSED','capture_roots':[str(self.root)]},
            active=lambda:True,deployment=SimpleNamespace(changed=lambda **kw:False),catalog_cache={})
    def edit(self,action,episode=1,**extra):
        return cp.review_change(self.app,{'job_id':'job','root':str(self.root),'episode':episode,'action':action,**extra})
    def test_interrupted_clip_is_excluded_and_annotation_edit_preserves_provenance(self):
        cp.mark_interrupted(self.root,1,12);self.edit('edit',result='exclude',note='接电话中断')
        row=reviews.read_reviews(self.root)['1'];self.assertTrue(row['interrupted']);self.assertEqual(row['frames_at_pause'],12)
        self.assertEqual(row['note'],'接电话中断');self.assertEqual((self.root/'meta/info.json').read_bytes(),self.original)
    def test_delete_and_restore_are_idempotent_and_preserve_previous_status_note_and_source(self):
        self.edit('edit',result='success',note='正常')
        self.edit('delete');self.edit('delete');row=reviews.read_reviews(self.root)['1']
        self.assertTrue(row['deleted']);self.assertEqual(row['result'],'exclude');self.assertEqual(row['before_delete_result'],'success')
        with self.assertRaises(ValueError):reviews.save_review(self.root,1,'success','no')
        with self.assertRaises(ValueError):reviews.save_reviews(self.root,[0,1],'success')
        self.edit('restore');self.edit('restore');row=reviews.read_reviews(self.root)['1']
        self.assertFalse(row['deleted']);self.assertEqual(row['result'],'success');self.assertEqual(row['note'],'正常')
        self.assertEqual((self.root/'meta/info.json').read_bytes(),self.original)
    def test_library_restore_works_without_the_original_capture_job(self):
        self.edit('delete');notes=reviews.restore_review(self.root,1)
        self.assertFalse(notes['1']['deleted']);self.assertEqual(notes['1']['result'],'unreviewed')
    def test_processing_rejects_running_or_stale_sessions_other_roots_and_invalid_indices(self):
        self.app.job['phase']='RECORDING'
        with self.assertRaises(RuntimeError):self.edit('delete')
        self.app.job['phase']='PAUSED'
        for value in (-1,3,True,'1'):
            with self.assertRaises(ValueError):self.edit('delete',value)
        for extra in ({'job_id':'old'},{'root':str(self.base/'other')}):
            with self.assertRaises(ValueError):cp.review_change(self.app,{'job_id':'job','root':str(self.root),'episode':0,'action':'delete',**extra})
        self.assertFalse(reviews.annotation_path(self.root).exists())
    def test_finished_capture_can_still_be_reviewed_but_new_source_blocks_mutation(self):
        self.app.active=lambda:False;self.app.job['phase']='FINISHED';self.edit('note',note='结束后备注')
        self.app.deployment.changed=lambda **kw:True
        with self.assertRaises(RuntimeError):self.edit('delete')
    def test_running_capture_blocks_all_annotation_routes(self):
        self.app.job['phase']='RESET'
        with self.assertRaises(RuntimeError):cp.require_review_idle(self.app)
        self.app.job['phase']='PAUSED';cp.require_review_idle(self.app)


class CommandTests(unittest.TestCase):
    def app(self,phase='RECORDING',kind='record'):
        from server import Platform
        app=Platform.__new__(Platform);app.offline=False;app.deployment=Mock();app.deployment.changed.return_value=False
        app.lock=threading.RLock();app.write_lock=threading.Lock();app.log=Mock();app.proc=Mock()
        app.active=lambda:True;app.job={'id':'job','kind':kind,'phase':phase}
        return app
    def test_pause_and_resume_are_bound_to_current_record_job_and_phase(self):
        app=self.app();app.command({'command':'pause_capture','job_id':'job','phase':'RECORDING'})
        self.assertEqual(app.job['phase'],'PAUSING');app.proc.stdin.flush.assert_called_once()
        app.job['phase']='PAUSED'
        with self.assertRaises(ValueError):app.command({'command':'resume_capture','job_id':'job'})
        app.command({'command':'resume_capture','job_id':'job','confirmed':True});self.assertEqual(app.job['phase'],'RESUMING')
    def test_pause_rejects_other_tasks_stale_phase_and_session_without_writing(self):
        for phase,kind,job,expected in [('RESET','record','job','RECORDING'),('RECORDING','teleop','job','RECORDING'),('RECORDING','record','old','RECORDING')]:
            app=self.app(phase,kind)
            with self.assertRaises(ValueError):app.command({'command':'pause_capture','job_id':job,'phase':expected})
            app.proc.stdin.write.assert_not_called()
    def test_offline_and_changed_source_block_resume(self):
        app=self.app('PAUSED');app.offline=True
        with self.assertRaises(PermissionError):app.command({'command':'resume_capture','job_id':'job','confirmed':True})
        app.offline=False;app.deployment.changed.return_value=True
        with self.assertRaises(RuntimeError):app.command({'command':'resume_capture','job_id':'job','confirmed':True})
        app.proc.stdin.write.assert_not_called()


if __name__=='__main__':unittest.main()
