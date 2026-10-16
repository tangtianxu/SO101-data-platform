"""Optional in-process ACT/camera reuse. Never stores a motor bus or writes motors."""
import gc
from pathlib import Path
import threading
import time
import sys


class ResidentResources:
    def __init__(self):
        self.contract=None
        self.model=None
        self.warmed=False
        self.cameras={}
        self.busy=False
        self.lock=threading.RLock()
        self.last_preview=-float('inf')

    @staticmethod
    def checkpoint_signature(path):
        root=Path(path).resolve()
        files=[p for p in root.iterdir() if p.is_file() and p.suffix in ('.json','.safetensors')]
        return str(root),tuple(sorted((p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in files))

    def begin(self,contract):
        with self.lock:
            if self.busy:raise RuntimeError('常驻运行资源已有使用者；不并发执行 ACT')
            if self.contract is not None and self.contract!=contract:
                raise RuntimeError('权重、标定或相机配置变化，请先释放常驻资源再重新准备')
            self.contract=contract
            self.busy=True

    def get_model(self,loader):
        if not self.busy:raise RuntimeError('资源不在本次会话内')
        reused=self.model is not None
        if not reused:self.model=loader()
        return self.model,reused

    def get_camera(self,name,factory):
        if not self.busy:raise RuntimeError('资源不在本次会话内')
        camera=self.cameras.get(name)
        if camera is not None:
            if not camera.is_connected or camera.thread is None or not camera.thread.is_alive():
                raise RuntimeError(name+' 常驻相机采集已中断，需重新准备')
            return camera,True
        camera=factory()
        # Track even a partial connect so the error path attempts cleanup.
        self.cameras[name]=camera
        camera.connect()
        return camera,False

    def finish(self,keep):
        with self.lock:
            self.busy=False
            if keep:return []
            return self.close()

    def preview(self,period=.1):
        with self.lock:
            if self.busy or time.perf_counter()-self.last_preview<period:return None
            frames={}
            for name,camera in self.cameras.items():
                if not camera.is_connected or camera.thread is None or not camera.thread.is_alive():return None
                with camera.frame_lock:
                    if camera.latest_frame is None or camera.latest_timestamp is None:return None
                    if time.perf_counter()-camera.latest_timestamp>.5:return None
                    frames[name]=camera.latest_frame.copy()
            self.last_preview=time.perf_counter()
            return frames or None

    def close(self):
        with self.lock:
            if self.busy:raise RuntimeError('请先停止当前测试，再释放相机和模型；不强杀设备线程')
            errors=[]
            remaining={}
            for name,camera in self.cameras.items():
                try:
                    if camera.is_connected:camera.disconnect()
                except Exception as error:
                    errors.append(f'{name}: {error!r}')
                    remaining[name]=camera
            self.cameras=remaining
            self.model=None
            self.warmed=False
            if not remaining:self.contract=None
            gc.collect()
            torch=sys.modules.get('torch')
            if torch is not None and torch.cuda.is_initialized():torch.cuda.empty_cache()
            return errors
