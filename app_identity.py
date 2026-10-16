"""One source tree, pinned assets, visible build identity; no user-profile storage."""
from datetime import datetime,timezone,timedelta
import hashlib
import os
from pathlib import Path
import time
import uuid

from common import HERE

VERSION='0.2.3'
APP_ID='so101.data-platform.local'
ENTRY=Path('E:/robot/SO101数采平台.cmd')


def source_files(root=HERE):
    root=Path(root)
    files=list(root.glob('*.py'))+list(root.glob('*.cmd'))+[root/'config.json']
    files+=list((root/'dist').glob('*.html'))+list((root/'dist').glob('*.js'))+list((root/'dist').glob('*.css'))
    files+=list((root/'scripts').glob('*.ps1'))
    if root.resolve()==HERE.resolve():
        files += [Path('E:/robot/scripts')/name for name in ('so101_guarded_real_act.py','so101_act_shadow.py',
            'so101_guarded_real_replay.py','so101_weighted_reset.py','so101_record_supplement.py')]
        if ENTRY.is_file():files.append(ENTRY)
    return sorted({p.resolve() for p in files if p.is_file()},key=lambda p:str(p).lower())


class Deployment:
    def __init__(self,root=HERE):
        self.root=Path(root).resolve();self.files=source_files(self.root)
        self.signatures={p:(p.stat().st_mtime_ns,p.stat().st_size) for p in self.files}
        manifest={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.files}
        digest=hashlib.sha256()
        for name,value in manifest.items():digest.update((name+'\0'+value+'\n').encode('utf-8'))
        self.build=digest.hexdigest()[:12];self.instance=uuid.uuid4().hex
        self.started=datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
        self.assets={p.name:p.read_bytes() for p in (self.root/'dist').iterdir() if p.is_file()} if (self.root/'dist').is_dir() else {}
        self.last_check=0.;self.invalidated=False

    def changed(self,force=False):
        if not force and time.monotonic()-self.last_check<.5:return self.invalidated
        self.last_check=time.monotonic()
        current=source_files(self.root)
        if current!=self.files:self.invalidated=True
        else:
            try:
                if any((p.stat().st_mtime_ns,p.stat().st_size)!=signature for p,signature in self.signatures.items()):self.invalidated=True
            except OSError:self.invalidated=True
        return self.invalidated

    def identity(self):
        return {'app_id':APP_ID,'version':VERSION,'build':self.build,'instance':self.instance,'pid':os.getpid(),
            'started':self.started,'project':str(self.root),'state_dir':str(self.root/'state'),
            'formal_entry':str(ENTRY),'source_changed':self.changed()}


if __name__=='__main__':
    import json
    print(json.dumps(Deployment().identity(),ensure_ascii=False))
