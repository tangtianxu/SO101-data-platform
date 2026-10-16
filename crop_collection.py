"""Transactional one-crop-per-source-episode datasets using official LeRobot tools.

The visible output is a normal, portable multi-episode LeRobot v3 dataset.
Existing videos are copied losslessly; only a newly requested crop is encoded.
"""
from contextlib import contextmanager
from datetime import datetime
import copy
import json
from pathlib import Path
import secrets
import shutil

from common import ROBOT_HOME, atomic_json, robot_path
from dataset_naming import canonical_name, describe, simple_defaults, validate_spec
from review_store import COLLECTION_FILE, read_collection, read_reviews, require_success


def plan_crop(cfg, source, episode):
    source = robot_path(source, existing=True)
    if read_collection(source) or (source / 'crop_provenance.json').exists():
        collection = read_collection(source)
        return {'can_crop': False, 'reason': '请返回原始轨迹调整裁剪；保存后替换原来的裁剪结果',
                'source': collection['source'] if collection else None, 'is_collection': True}
    info = json.loads((source / 'meta/info.json').read_text(encoding='utf-8'))
    if not 0 <= episode < info['total_episodes']:
        raise ValueError('原始轨迹不存在')
    home = robot_path(cfg['dataset_home'], existing=True)
    if home == source or home.is_relative_to(source):
        raise ValueError('裁剪集合的父目录不能位于原始数据集内部')
    candidates = []
    for folder in home.iterdir():
        if folder.is_dir() and not folder.name.startswith('.') and (folder / COLLECTION_FILE).is_file():
            manifest = read_collection(folder)
            if robot_path(manifest['source']) == source:
                candidates.append((folder, manifest))
    if len(candidates) > 1:
        raise ValueError('同一原始数据集已有多个裁剪集合，请先合并，不再新建')
    if candidates:
        destination, collection = candidates[0]
        # Preserve the operator's display identity; changed cuts need a new training review.
        current = describe(destination).get("simple_spec") or collection["dataset_spec"]
        spec = {**current,"stage":"processed","review":"pending"}
    else:
        parent = describe(source).get('simple_spec') or simple_defaults(cfg['task'])
        spec = {**parent, 'stage': 'processed', 'review': 'pending', 'description': '成功轨迹裁剪',
                'sequence': 1, 'started_at': simple_defaults()['started_at']}
        destination = home / canonical_name(spec)
        collection = None
        if destination.exists():
            raise ValueError('裁剪集合目录被其他数据占用，不覆盖未知目录')
    previous = next((row for row in collection['episodes'] if row['source_episode'] == episode), None) if collection else None
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('裁剪集合必须与原始数据目录分离')
    success = read_reviews(source).get(str(episode), {}).get('result') == 'success'
    return {'source': str(source), 'destination': str(destination), 'dataset_spec': spec,
            'can_crop': success, 'reason': '' if success else '请先把原始轨迹明确标记为成功，才能裁剪',
            'exists': destination.exists(), 'replacing': previous is not None, 'previous': previous,
            'total_crops': len(collection['episodes']) if collection else 0, 'is_collection': False}


@contextmanager
def collection_lock(home, source):
    import hashlib
    key = hashlib.sha256(str(robot_path(source)).encode()).hexdigest()[:16]
    path = robot_path(home) / ('.crop-lock-' + key)
    try:
        stream = path.open('x', encoding='utf-8')
    except FileExistsError:
        raise RuntimeError('此数据集已有裁剪事务，等待结束；异常退出后先检查锁文件') from None
    try:
        stream.write(datetime.now().isoformat()); stream.close()
        yield
    finally:
        path.unlink(missing_ok=True)


def remove_workspace(path):
    """Only remove our exact, resolved temporary output; never a dataset root."""
    path = Path(path)
    resolved = robot_path(path, existing=True)
    if resolved != path.absolute() or not resolved.name.startswith('.pending-crop-update-'):
        raise ValueError('拒绝清理非本事务的暂存目录')
    if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in [resolved, *resolved.rglob('*')]):
        raise ValueError('暂存目录含链接，拒绝递归清理')
    shutil.rmtree(resolved)


def assemble(roots, destination):
    from lerobot.datasets.aggregate import aggregate_datasets
    aggregate_datasets(['local/crop_part_' + str(i) for i in range(len(roots))],
                       'local/so101_cropped_collection', roots=[Path(p) for p in roots],
                       aggr_root=Path(destination), concatenate_videos=False, concatenate_data=False)


def prepare_manifest(source, entries, spec):
    ordered = sorted((copy.deepcopy(e) for e in entries), key=lambda r: r['source_episode'])
    if len({e['source_episode'] for e in ordered}) != len(ordered):
        raise ValueError('每条原始轨迹只允许保留一个裁剪结果')
    for index, entry in enumerate(ordered):
        entry['episode_index'] = index
    return {'schema_version': 1, 'operation': 'crop_collection', 'source': str(robot_path(source)),
            'dataset_spec': spec, 'updated': datetime.now().isoformat(), 'episodes': ordered,
            'original_data_unchanged': True, 'one_crop_per_source_episode': True}


def validate_collection(root, manifest, *, verify_sources=True):
    """Check all numeric labels, indexing, video bytes and source evidence before publishing."""
    import numpy as np
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    from cropping import AUTO, sha256
    from data_tools import inspect_dataset
    root = Path(root)
    catalog = inspect_dataset(root)
    entries = manifest['episodes']
    if catalog['total_episodes'] != len(entries) or catalog['indexed_episodes'] != len(entries):
        raise ValueError('裁剪集合条数不一致')
    if catalog['total_frames'] != sum(e['frames'] for e in entries):
        raise ValueError('裁剪集合帧数不一致')
    source = robot_path(manifest['source'], existing=True)
    source_info = json.loads((source / 'meta/info.json').read_text(encoding='utf-8'))
    source_rows = {}
    for path in (source / 'meta/episodes').glob('chunk-*/*.parquet'):
        source_rows.update({int(row['episode_index']): row for row in pq.read_table(path).to_pylist()})
    checked = {}
    for index, entry in enumerate(entries):
        first, last = entry['source_frame_range_half_open']
        row = source_rows[entry['source_episode']]
        relative = source_info['data_path'].format(chunk_index=row['data/chunk_index'], file_index=row['data/file_index'])
        path = (source / relative).resolve()
        if not path.is_relative_to(source):
            raise ValueError('源数据路径越界')
        table = pq.read_table(path)
        table = table.filter(pc.equal(table['episode_index'], entry['source_episode'])).slice(first, last-first)
        actual = inspect_dataset(root, index)
        if not actual['frame_count_matches'] or actual['selected']['frames'] != len(table):
            raise ValueError('裁剪轨迹帧数或索引不一致')
        target_info = json.loads((root / 'meta/info.json').read_text(encoding='utf-8'))
        target_rows = []
        for target_path in (root / 'meta/episodes').glob('chunk-*/*.parquet'):
            target_rows.extend(pq.read_table(target_path).to_pylist())
        target_row = next(r for r in target_rows if r['episode_index'] == index)
        target_path = root / target_info['data_path'].format(chunk_index=target_row['data/chunk_index'], file_index=target_row['data/file_index'])
        target_table = pq.read_table(target_path)
        target_table = target_table.filter(pc.equal(target_table['episode_index'], index))
        for key, feature in source_info['features'].items():
            if key not in AUTO and feature['dtype'] not in ('video', 'image', 'string'):
                if not np.array_equal(np.asarray(table[key].to_pylist()), np.asarray(target_table[key].to_pylist()), equal_nan=True):
                    raise ValueError('裁剪后的数值标签与原始数据不一致：' + key)
        if verify_sources:
            for relative, expected in entry['original_files_sha256'].items():
                path = (source / relative).resolve()
                if not path.is_relative_to(source):
                    raise ValueError('来源证据路径越界')
                if path not in checked:
                    checked[path] = sha256(path)
                if checked[path] != expected:
                    raise ValueError('原始数据已变化，拒绝提交裁剪集合')
        for key, expected in entry.get('video_sha256', {}).items():
            if sha256(root / actual['selected']['videos'][key]['path']) != expected:
                raise ValueError('集合合并不应重新编码已有视频')
    return {'episodes': len(entries), 'frames': catalog['total_frames'], 'numeric_labels_verified': True,
            'original_hashes_verified': verify_sources, 'videos_copied_losslessly': True}


def write_signal_snapshot(root, manifest):
    from signal_tools import sidecar_signals, read_platform_samples
    signals = {}
    source = robot_path(manifest['source'], existing=True)
    samples = read_platform_samples(source,[entry["source_episode"] for entry in manifest["episodes"]])
    for entry in manifest['episodes']:
        origin = entry.get('source_time_origin', entry['source_frame_range_half_open'][0] / entry['fps'])
        duration = entry['frames'] / entry['fps']
        rows = []
        for signal in sidecar_signals(source, entry['source_episode'], origin + duration,platform_samples=samples[entry["source_episode"]]):
            keep = [i for i, t in enumerate(signal['time']) if origin <= t < origin + duration]
            if keep:
                rows.append({**signal, 'time': [signal['time'][i] - origin for i in keep],
                             'values': [signal['values'][i] for i in keep],
                             'source': signal['source'] + '；按原始裁剪时间对齐，不插值'})
        signals[str(entry['episode_index'])] = rows
    atomic_json(Path(root) / 'extras/crop_signals.json', signals)


def publish(staged, destination, manifest, workspace, stop=None):
    if stop and stop.is_set():
        raise InterruptedError('裁剪已取消；原有结果未替换')
    destination = robot_path(destination)
    staged, workspace = robot_path(staged, existing=True), robot_path(workspace, existing=True)
    if staged.parent != workspace or not workspace.name.startswith('.pending-crop-update-') or workspace.parent != destination.parent:
        raise ValueError('裁剪事务暂存路径不符')
    old = read_collection(destination) if destination.exists() else None
    if old and robot_path(old['source']) != robot_path(manifest['source']):
        raise ValueError('不能替换其他来源的数据集')
    if destination.exists() and old is None:
        raise ValueError('输出目录不是本来源的裁剪集合')
    backup = workspace / 'previous'
    # Readers see a short rename transaction. Any failure restores the previous complete folder.
    if old:
        destination.rename(backup)
    try:
        staged.rename(destination)
    except BaseException:
        if backup.exists():
            backup.rename(destination)
        raise
    return backup if backup.exists() else None


def save_crop(cfg, source, episode, start, end, *, stop=None, progress=None, expected_plan=None):
    result = save_crops(cfg,source,[{"episode":episode,"start":start,"end":end}],
                        stop=stop,progress=progress,expected_plan=expected_plan)
    entry = result.pop("entries")[0]
    return {**entry,**result,"source_episode":episode}


def save_crops(cfg, source, requests, *, stop=None, progress=None, expected_plan=None,
               on_episode=None, on_stage=None, before_publish=None):
    """Build all requested clips, then publish one validated collection atomically."""
    from cropping import crop_episode, sha256
    from data_tools import inspect_dataset
    source = robot_path(source,existing=True)
    if not isinstance(requests,list) or not requests:
        raise ValueError("请选择至少一条裁剪轨迹")
    ids = [r.get("episode") for r in requests]
    if any(type(ep) is not int for ep in ids) or len(set(ids)) != len(ids):
        raise ValueError("批量裁剪的轨迹编号重复或无效")
    requests = sorted(requests,key=lambda r:r["episode"])
    for ep in ids: require_success(source,ep)
    home = robot_path(cfg["dataset_home"],existing=True)
    with collection_lock(home,source):
        plan = plan_crop(cfg,source,requests[0]["episode"])
        if expected_plan is not None:
            expected_root = robot_path(expected_plan["destination"])
            expected_spec = validate_spec(expected_plan["dataset_spec"])
            if robot_path(expected_plan["source"]) != source or expected_root.parent != home:
                raise ValueError("裁剪计划来源或父目录不一致")
            if plan["exists"] and expected_root != Path(plan["destination"]):
                raise ValueError("裁剪集合在启动期间发生变化，请刷新后重试")
            if not plan["exists"] and (expected_root.name != canonical_name(expected_spec) or expected_spec["stage"] != "processed" or expected_spec["review"] != "pending"):
                raise ValueError("裁剪集合命名不符合启动时计划")
            plan = {**plan,"destination":str(expected_root),"dataset_spec":expected_spec}
        destination = Path(plan["destination"])
        previous = read_collection(destination) if destination.exists() else None
        retained = [r for r in previous["episodes"] if r["source_episode"] not in ids] if previous else []
        replaced_ids = sorted(set(ids) & {r["source_episode"] for r in previous["episodes"]}) if previous else []
        workspace = home / (".pending-crop-update-" + secrets.token_hex(8))
        workspace.mkdir()
        committed = False
        try:
            new_entries, parts = [], {}
            for index, request in enumerate(requests,1):
                if stop and stop.is_set(): raise InterruptedError("批量裁剪已取消，旧结果未替换")
                ep = request["episode"]
                require_success(source,ep)
                if on_episode: on_episode(index,len(requests),ep)
                new = workspace / ("new_" + str(ep))
                entry = crop_episode(source,new,ep,request["start"],request["end"],stop=stop,progress=progress)
                entry.pop("destination",None)
                if request.get("automatic_trim"): entry["automatic_trim"] = copy.deepcopy(request["automatic_trim"])
                item = inspect_dataset(new,0)["selected"]
                entry["video_sha256"] = {key:sha256(new/value["path"]) for key,value in item["videos"].items()}
                new_entries.append(entry); parts[ep] = new
            # Split old retained ranges once, then interleave them with new clips in source order.
            ordered = sorted([*retained,*new_entries],key=lambda r:r["source_episode"])
            runs, order, pending = {}, [], []
            def flush():
                if pending:
                    name = "retained_" + str(len(runs))
                    runs[name] = [row["episode_index"] for row in pending]
                    order.append(workspace/"retained"/name); pending.clear()
            for entry in ordered:
                ep = entry["source_episode"]
                if ep in parts:
                    flush(); order.append(parts[ep])
                else: pending.append(entry)
            flush()
            if runs:
                from lerobot.datasets.dataset_tools import split_dataset
                from lerobot.datasets.lerobot_dataset import LeRobotDataset
                dataset = LeRobotDataset("local/crop_collection",root=destination,video_backend="pyav")
                split_dataset(dataset,runs,output_dir=workspace/"retained")
                del dataset
                __import__("gc").collect()
            if stop and stop.is_set(): raise InterruptedError("批量裁剪已取消，旧结果未替换")
            staged = workspace/"assembled"
            if on_stage: on_stage("CROP_MERGING","全部片段已生成，正在合并为同一个裁剪集；尚未提交")
            assemble(order,staged)
            manifest = prepare_manifest(source,ordered,plan["dataset_spec"])
            atomic_json(staged/COLLECTION_FILE,manifest)
            write_signal_snapshot(staged,manifest)
            if on_stage: on_stage("CROP_VERIFYING","正在校验视频、数值标签和原始文件；尚未提交")
            checks = validate_collection(staged,manifest)
            for ep in ids: require_success(source,ep)
            if before_publish: before_publish()
            if on_stage: on_stage("CROP_PUBLISHING","校验通过，正在统一提交裁剪集")
            backup = publish(staged,destination,manifest,workspace,stop)
            archived = None
            if backup and replaced_ids:
                archive = ROBOT_HOME/"dataset_archive/crop_revisions"
                archive.mkdir(parents=True,exist_ok=True)
                archived = archive/(datetime.now().strftime("%Y%m%d_%H%M%S")+"_"+secrets.token_hex(4))
                backup.rename(archived)
                atomic_json(archived/"revision_backup.json",{"restore_to":str(destination),"source":str(source),
                    "replaced_source_episodes":replaced_ids,
                    "replaced_source_episode":replaced_ids[0] if len(replaced_ids)==1 else None,"recoverable":True})
            committed = True
            return {"outcome":"cropped","destination":str(destination),"replaced":bool(replaced_ids),
                    "processed_count":len(new_entries),"processed_episodes":sorted(ids),"replaced_episodes":replaced_ids,
                    "total_crops":len(manifest["episodes"]),"checks":checks,"entries":new_entries,
                    "previous_revision_backup":str(archived) if archived else None}
        finally:
            if committed: remove_workspace(workspace)
