"""Bounded background LeRobot writer; sensor reads stay on the ACT bus owner thread."""
import json
from pathlib import Path
import queue
import threading
import time
import hashlib

from common import NAMES
from feedback_store import update


def make_dataset(root):
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from lerobot.configs.video import RGBEncoderConfig
    from sensors import features
    spec={'observation.state':{'dtype':'float32','shape':(6,),'names':list(NAMES)},
        'action':{'dtype':'float32','shape':(6,),'names':list(NAMES)},**features()}
    for key in ('policy_requested','clipped'):
        spec['observation.rollout.'+key]={'dtype':'float32','shape':(6,),'names':list(NAMES)}
    for key in ('observation_perf_s','send_perf_s','elapsed_s','top_capture_perf_s','wrist_capture_perf_s'):
        spec['observation.rollout.'+key]={'dtype':'float64','shape':(1,),'names':[key]}
    for camera in ('top','wrist'):
        spec['observation.images.'+camera]={'dtype':'video','shape':(480,640,3),'names':['height','width','channels']}
    return LeRobotDataset.create(repo_id='local/act_'+hashlib.sha256(str(root).encode()).hexdigest()[:16],
        root=root,fps=30,features=spec,robot_type='so101_follower',use_videos=True,streaming_encoding=True,
        encoder_threads=2,metadata_buffer_size=1,rgb_encoder=RGBEncoderConfig(vcodec='h264',g=30,crf=20,preset='fast'))


class PolicyCapture:
    def __init__(self,root,cfg,factory=make_dataset):
        self.root=Path(root);self.cfg=cfg;self.factory=factory
        self.queue=queue.Queue(maxsize=8);self.ready=threading.Event();self.error=None
        self.thread=None;self.feedback=None;self.frames=0;self.written=0;self.first_sent=None

    def prepare(self,bus):
        from sensors import FeedbackSampler
        self.feedback=FeedbackSampler(bus,self.cfg['sensor_hz'])
        # Verify actual register feedback on the sole bus owner, not the writer thread.
        self.feedback.sample(force=True)
        self.thread=threading.Thread(target=self._write,name='act-feedback-writer',daemon=True);self.thread.start()
        if not self.ready.wait(15):raise TimeoutError('ACT 回流写入器准备超时，未启动策略任务')
        if self.error:raise RuntimeError('ACT 回流写入器准备失败：'+str(self.error))
        update(self.root,state='recording')

    def capture(self,*,bus,state,candidate,target,clipped,frames,stamps,observation_time,send_time):
        import numpy as np
        if self.error:raise RuntimeError('ACT 数据回流写入失败：'+str(self.error))
        sensor=self.feedback.frame()  # Same thread and COM owner; samples have their own clock.
        if self.first_sent is None:self.first_sent=send_time
        frame={'task':self.cfg['task'],'observation.state':np.asarray(state,dtype='float32').copy(),
               'action':np.asarray(target,dtype='float32').copy(),**sensor}
        for key,value in (('policy_requested',candidate),('clipped',clipped)):
            frame['observation.rollout.'+key]=np.asarray(value,dtype='float32').copy()
        times={'observation_perf_s':observation_time,'send_perf_s':send_time,'elapsed_s':send_time-self.first_sent,
            'top_capture_perf_s':stamps['top'],'wrist_capture_perf_s':stamps['wrist']}
        for key,value in times.items():frame['observation.rollout.'+key]=np.asarray([value],dtype='float64')
        for key,image in frames.items():frame['observation.images.'+key]=image
        raw={'frame':self.frames,'task':self.cfg['task'],'action_source':'policy','expert_action':None,
             'state':frame['observation.state'].tolist(),'candidate':np.asarray(candidate).tolist(),
             'sent_action':frame['action'].tolist(),'clipped':np.asarray(clipped,dtype=int).tolist(),
             'times':times,'sample':self.feedback.latest}
        try:self.queue.put_nowait((frame,raw))
        except queue.Full:raise BufferError('ACT 回流编码跟不上，正常停止本次任务；保留已写入数据，不静默丢帧') from None
        self.frames+=1

    def _write(self):
        writer=None
        try:
            writer=self.factory(self.root/'dataset');self.ready.set()
            with (self.root/'steps.jsonl').open('x',encoding='utf-8',buffering=1) as log:
                while True:
                    item=self.queue.get()
                    if item is None:break
                    frame,raw=item;writer.add_frame(frame)
                    log.write(json.dumps(raw,ensure_ascii=False,allow_nan=False)+'\n');self.written+=1
            if self.written:writer.save_episode(parallel_encoding=False)
        except BaseException as error:self.error=error;self.ready.set()
        finally:
            if writer:
                try:writer.finalize()
                except BaseException as error:self.error=self.error or error

    def finish(self,report):
        if self.thread and self.thread.is_alive():
            update(self.root,state='finalizing')
            deadline=time.monotonic()+30
            while self.thread.is_alive():
                try:self.queue.put(None,timeout=.1);break
                except queue.Full:
                    if time.monotonic()>=deadline:self.error=TimeoutError('回流队列收尾超时');break
            self.thread.join(max(0,deadline-time.monotonic()))
            if self.thread.is_alive():self.error=TimeoutError('回流编码器收尾超时，数据标记为不完整')
        complete=bool(self.frames and self.frames==self.written and self.error is None)
        return update(self.root,state='finished' if complete else 'incomplete',complete=complete,frames=self.written,
            queued_frames=self.frames,capture_error=str(self.error) if self.error else None,
            runtime_outcome=report.get('outcome','error'),end_reason=report.get('outcome','error'),
            task_success=None,expert_approved=False,training_eligible=False,
            truncated=report.get('outcome') in ('completed_timed_trial','user_stopped'),
            sensor_target_hz=self.cfg['sensor_hz'],nominal_fps=30,actual_span_s=report.get('trial_elapsed_s'),
            ended=time.time())


class CaptureController:
    def __init__(self,bridge,capture):self.bridge=bridge;self.capture=capture
    def __getattr__(self,name):return getattr(self.bridge,name)
    def prepare_policy_capture(self,bus):self.capture.prepare(bus)
    def capture_policy_step(self,**values):self.capture.capture(**values)
    def emit(self,kind,**values):
        if kind=='telemetry' and self.capture.feedback and self.capture.feedback.latest:
            values['health']=self.capture.feedback.latest['health']
        self.bridge.emit(kind,**values)
    policy_video_recording=True
