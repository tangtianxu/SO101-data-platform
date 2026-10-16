"""Frame-boundary capture pause and recoverable review, never a second device owner."""
import json
import threading
from datetime import datetime
from pathlib import Path

from common import atomic_json
from review_store import annotation_path, read_reviews


class PauseControl:
    def __init__(self, session):
        self.session = session
        self.requested = threading.Event()
        self.resume = threading.Event()

    def request_pause(self, expected_phase):
        s = self.session
        with s.control_lock:
            if s.stop.is_set() or s.phase not in ('RECORDING', 'RESET') or expected_phase != s.phase:
                return False
            if self.requested.is_set():
                return False
            self.resume.clear()
            self.requested.set()
            s.events['exit_early'] = True
            return True

    def request_resume(self):
        with self.session.control_lock:
            if self.session.phase != 'PAUSED' or not self.requested.is_set() or self.session.stop.is_set():
                return False
            self.resume.set()
            return True

    def wait(self, hold, catchup):
        if not self.requested.is_set():
            return
        s = self.session
        # Only the device owner calls hold/catchup. Reader threads merely set events.
        s.motion.anchor_current()
        s.state('PAUSED')
        while not self.resume.is_set():
            if s.stop.is_set() or s.events['stop_recording']:
                raise KeyboardInterrupt
            hold()
            s.stop.wait(.1)
        if s.stop.is_set() or s.events['stop_recording']:
            raise KeyboardInterrupt
        s.state('RESUMING')
        catchup()
        with s.control_lock:
            self.requested.clear()
            self.resume.clear()


def mark_interrupted(root, episode, frames):
    root = Path(root)
    notes = read_reviews(root)
    old = notes.get(str(episode), {})
    notes[str(episode)] = {**old, 'result': 'exclude', 'interrupted': True,
        'note': (old.get('note', '')+'\n采集中暂停：仅保存暂停前片段，默认不用于训练。').strip()[:1000],
        'frames_at_pause': frames, 'time': datetime.now().isoformat()}
    atomic_json(annotation_path(root), notes)


def roots_for_job(job):
    roots = job.get('capture_roots') or [job.get('dataset_root')]
    return [Path(root).resolve() for root in dict.fromkeys(roots) if root]


def require_review_job(app, job_id, mutation=False):
    job = app.job
    if not job or job.get('id') != job_id or job.get('kind') != 'record':
        raise ValueError('采集会话已变化，请重新打开处理窗口')
    if app.active() and job.get('phase') != 'PAUSED':
        raise RuntimeError('请等待采集暂停确认，再处理数据')
    if mutation and app.deployment.changed(force=True):
        raise RuntimeError('程序已更新，请正常结束后重开')
    return job


def require_review_idle(app):
    if app.active() and app.job.get('kind')=='record' and app.job.get('phase')!='PAUSED':
        raise RuntimeError('请先暂停采集，再处理轨迹')


def review_catalog(app, job_id):
    job = require_review_job(app, job_id)
    result = []
    for root in roots_for_job(job):
        info = root/'meta/info.json'
        total = json.loads(info.read_text(encoding='utf-8'))['total_episodes'] if info.is_file() else 0
        notes = read_reviews(root) if root.exists() else {}
        result.append({'root': str(root), 'name': root.name, 'total': total, 'annotations': notes})
    return {'job_id': job_id, 'datasets': result}


def review_change(app, request):
    job = require_review_job(app, request.get('job_id'), mutation=True)
    root = Path(str(request.get('root', ''))).resolve()
    if root not in roots_for_job(job):
        raise ValueError('只能处理本次采集计划中的数据集')
    total = json.loads((root/'meta/info.json').read_text(encoding='utf-8'))['total_episodes']
    episode = request.get('episode')
    if type(episode) is not int or not 0 <= episode < total:
        raise ValueError('只能处理已保存的有效轨迹')
    action = request.get('action')
    notes = read_reviews(root)
    old = notes.get(str(episode), {'result': 'unreviewed', 'note': ''})
    row = dict(old)
    if action in ('note','edit'):
        note = request.get('note', '')
        if not isinstance(note, str) or len(note) > 1000:
            raise ValueError('备注不超过 1000 字')
        row['note'] = note
        if action=='edit' and not old.get('deleted'):
            value=request.get('result')
            if value not in ('success','failure','exclude','unreviewed'):raise ValueError('未知审核状态')
            row['result']=value
    elif action == 'delete':
        if not old.get('deleted'):
            row.update(deleted=True, before_delete_result=old['result'], result='exclude')
    elif action == 'restore':
        row.update(deleted=False, result=old.get('before_delete_result', old.get('result', 'unreviewed')))
        row.pop('before_delete_result', None)
    elif action == 'result':
        value = request.get('result')
        if value not in ('success', 'failure', 'exclude', 'unreviewed') or old.get('deleted'):
            raise ValueError('请先恢复已删除条目，再修改审核状态')
        row['result'] = value
    else:
        raise ValueError('未知处理操作')
    row['time'] = datetime.now().isoformat()
    notes[str(episode)] = row
    atomic_json(annotation_path(root), notes)
    app.catalog_cache.clear()
    return review_catalog(app, job['id'])
