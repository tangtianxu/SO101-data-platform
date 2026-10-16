"""One authoritative episode review, shared by raw and cropped views."""
import hashlib
import json
from datetime import datetime
from pathlib import Path

from common import HERE, ROBOT_HOME, atomic_json, robot_path

ANNOTATIONS = ROBOT_HOME / 'platform_state/annotations'
COLLECTION_FILE = 'crop_collection.json'


def annotation_path(root):
    key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:20]
    return ANNOTATIONS / (key + '.json')


def read_collection(root):
    root = robot_path(root, existing=True)
    path = root / COLLECTION_FILE
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('schema_version') != 1 or value.get('operation') != 'crop_collection':
        raise ValueError('裁剪集合清单格式异常，不覆盖')
    source=robot_path(value['source'])
    if source==root or (source / COLLECTION_FILE).exists():
        raise ValueError('裁剪来源必须是独立的原始数据集，不能循环引用裁剪集合')
    rows = value['episodes']
    if not isinstance(rows,list) or any(type(r.get('source_episode')) is not int or r['source_episode']<0 for r in rows):
        raise ValueError('原始轨迹映射无效')
    if [r['episode_index'] for r in rows] != list(range(len(rows))) or len({r['source_episode'] for r in rows}) != len(rows):
        raise ValueError('裁剪集合的轨迹映射重复或不连续')
    return value


def read_reviews(root):
    root = robot_path(root, existing=True)
    collection = read_collection(root)
    if collection:
        notes = read_reviews(collection['source'])
        return {str(row['episode_index']): {**notes.get(str(row['source_episode']), {'result': 'unreviewed', 'note': ''}),
                    'inherited': True, 'source_root': collection['source'], 'source_episode': row['source_episode']}
                for row in collection['episodes']}
    path = annotation_path(root)
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def review_reference(root, episode):
    root = robot_path(root, existing=True)
    collection = read_collection(root)
    if collection:
        if not 0 <= episode < len(collection['episodes']):
            raise ValueError('裁剪轨迹不存在')
        return robot_path(collection['source'], existing=True), collection['episodes'][episode]['source_episode']
    info = json.loads((root / 'meta/info.json').read_text(encoding='utf-8'))
    if not 0 <= episode < info['total_episodes']:
        raise ValueError('原始轨迹不存在')
    return root, episode


def review_revision(root):
    root = robot_path(root,existing=True)
    file = root/COLLECTION_FILE
    return hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else None


def save_review(root, episode, result, note=''):
    if result not in ('success', 'failure', 'unreviewed', 'exclude'):
        raise ValueError('未知审核标记')
    source, source_episode = review_reference(root, episode)
    notes = read_reviews(source)
    old=notes.get(str(source_episode),{})
    if old.get('deleted') and result!='exclude':raise ValueError('请先恢复已删除轨迹，再修改审核状态')
    notes[str(source_episode)] = {**old,'result': result, 'note': str(note)[:1000], 'time': datetime.now().isoformat()}
    atomic_json(annotation_path(source), notes)
    return read_reviews(root)


def save_reviews(root, episodes, result):
    """Validate the whole selection, then atomically mark it; preserve individual notes."""
    if result not in ("success","failure","unreviewed","exclude"):
        raise ValueError("未知审核标记")
    root = robot_path(root,existing=True)
    collection = read_collection(root)
    total = len(collection["episodes"]) if collection else json.loads((root/"meta/info.json").read_text(encoding="utf-8"))["total_episodes"]
    if not isinstance(episodes,list) or not episodes or any(type(ep) is not int or not 0 <= ep < total for ep in episodes) or len(set(episodes)) != len(episodes):
        raise ValueError("请勾选有效、不重复的轨迹")
    source = robot_path(collection["source"],existing=True) if collection else root
    indices = [collection["episodes"][ep]["source_episode"] for ep in episodes] if collection else episodes
    source_total = json.loads((source/"meta/info.json").read_text(encoding="utf-8"))["total_episodes"]
    if any(not 0 <= ep < source_total for ep in indices):
        raise ValueError("原始轨迹映射已变化，不保存部分标记")
    notes = read_reviews(source)
    stamp = datetime.now().isoformat()
    if result!='exclude' and any(notes.get(str(ep),{}).get('deleted') for ep in indices):
        raise ValueError('请先恢复已删除轨迹，不保存部分标记')
    for ep in indices:
        notes[str(ep)] = {**notes.get(str(ep),{}),"result":result,
                          "note":notes.get(str(ep),{}).get("note",""),"time":stamp}
    atomic_json(annotation_path(source),notes)
    return read_reviews(root)


def archive_reviews(root,episodes,action):
    """One recoverable batch transaction, shared by raw/cropped views; no data rewrite."""
    if action not in ('delete','restore'):raise ValueError('未知删除／恢复操作')
    root=robot_path(root,existing=True);collection=read_collection(root)
    total=len(collection['episodes']) if collection else json.loads((root/'meta/info.json').read_text(encoding='utf-8'))['total_episodes']
    if not isinstance(episodes,list) or not episodes or any(type(ep) is not int or not 0<=ep<total for ep in episodes) or len(set(episodes))!=len(episodes):
        raise ValueError('请勾选有效、不重复的轨迹；未修改任何条目')
    source=robot_path(collection['source'],existing=True) if collection else root
    indices=[collection['episodes'][ep]['source_episode'] for ep in episodes] if collection else episodes
    source_total=json.loads((source/'meta/info.json').read_text(encoding='utf-8'))['total_episodes']
    if any(not 0<=ep<source_total for ep in indices):raise ValueError('原始轨迹映射已变化，未修改任何条目')
    notes=read_reviews(source);stamp=datetime.now().isoformat();changed=0
    valid=('success','failure','exclude','unreviewed')
    for ep in indices:
        old=notes.get(str(ep),{});row=dict(old)
        if action=='delete':
            if old.get('deleted'):continue
            previous=old.get('result','unreviewed')
            if previous not in valid:raise ValueError('审核状态异常，未修改任何条目')
            row.update(deleted=True,before_delete_result=previous,result='exclude')
        else:
            if not old.get('deleted'):continue
            previous=old.get('before_delete_result','exclude')
            if previous not in valid:raise ValueError('恢复状态异常，未修改任何条目')
            row.update(deleted=False,result=previous);row.pop('before_delete_result',None)
        row.update(note=old.get('note',''),time=stamp);notes[str(ep)]=row;changed+=1
    if changed:atomic_json(annotation_path(source),notes)
    return {'annotations':read_reviews(root),'changed':changed,'selected':len(episodes)}


def restore_review(root,episode):
    source,index=review_reference(root,episode)
    notes=read_reviews(source);row=notes.get(str(index),{})
    if row.get('deleted'):
        row={**row,'deleted':False,'result':row.get('before_delete_result','exclude'),'time':datetime.now().isoformat()}
        row.pop('before_delete_result',None);notes[str(index)]=row;atomic_json(annotation_path(source),notes)
    return read_reviews(root)


def require_success(root, episode):
    root = robot_path(root, existing=True)
    if (root / COLLECTION_FILE).exists() or (root / 'crop_provenance.json').exists():
        raise ValueError('请返回原始轨迹修改裁剪，不能对裁剪副本再次派生')
    review_reference(root, episode)
    if read_reviews(root).get(str(episode), {}).get('result') != 'success':
        raise ValueError('只有明确标记为成功的原始轨迹才允许裁剪')
