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


def save_review(root, episode, result, note=''):
    if result not in ('success', 'failure', 'unreviewed', 'exclude'):
        raise ValueError('未知审核标记')
    source, source_episode = review_reference(root, episode)
    notes = read_reviews(source)
    notes[str(source_episode)] = {'result': result, 'note': str(note)[:1000], 'time': datetime.now().isoformat()}
    atomic_json(annotation_path(source), notes)
    return read_reviews(root)


def require_success(root, episode):
    root = robot_path(root, existing=True)
    if (root / COLLECTION_FILE).exists() or (root / 'crop_provenance.json').exists():
        raise ValueError('请返回原始轨迹修改裁剪，不能对裁剪副本再次派生')
    review_reference(root, episode)
    if read_reviews(root).get(str(episode), {}).get('result') != 'success':
        raise ValueError('只有明确标记为成功的原始轨迹才允许裁剪')
