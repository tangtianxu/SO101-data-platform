"""Consolidate checked legacy crop folders; all old folders remain recoverable."""
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import HERE, atomic_json, environment, load_config, robot_path
environment()
from crop_collection import (assemble, collection_lock, plan_crop, prepare_manifest, publish,
                             remove_workspace, validate_collection, write_signal_snapshot)
from cropping import sha256
from dataset_library import archive_dataset
from dataset_naming import REGISTRY, store_metadata
from review_store import COLLECTION_FILE, annotation_path, read_collection, read_reviews


def tree_hashes(root):
    return {str(p.relative_to(root)): sha256(p) for p in sorted(Path(root).rglob('*')) if p.is_file()}


def migrate(cfg, source):
    source=robot_path(source, existing=True)
    home=robot_path(cfg['dataset_home'], existing=True)
    candidates=[]
    for folder in home.iterdir():
        path=folder/'crop_provenance.json'
        if folder.is_dir() and not folder.name.startswith('.') and path.is_file():
            record=json.loads(path.read_text(encoding='utf-8'))
            if robot_path(record['source'])==source:
                if record.get('status')!='complete' or record.get('operation')!='frame_aligned_crop':
                    raise ValueError('发现非完整的历史裁剪，需人工核对：'+str(folder))
                info=json.loads((folder/'meta/info.json').read_text(encoding='utf-8'))
                if info['total_episodes']!=1 or info['total_frames']!=record['frames']:
                    raise ValueError('历史裁剪元数据不符：'+str(folder))
                candidates.append({'root':folder,'record':record,'saved_ns':path.stat().st_mtime_ns})
    if not candidates:
        raise ValueError('没有需要迁移的旧裁剪文件夹')
    grouped={}
    for item in candidates:
        grouped.setdefault(item['record']['source_episode'],[]).append(item)
    reviews=read_reviews(source)
    chosen=[max(items,key=lambda item:(item['saved_ns'],str(item['root']))) for ep,items in sorted(grouped.items())
            if reviews.get(str(ep),{}).get('result')=='success']
    if not chosen:
        raise ValueError('没有已标记成功的裁剪轨迹')
    plan=plan_crop(cfg, source, chosen[0]['record']['source_episode'])
    destination=Path(plan['destination'])
    if destination.exists():
        raise ValueError('已有裁剪集合，不重复迁移；检查先前迁移回执')
    report_dir=HERE/'state/crop_migrations'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    report_dir.mkdir(parents=True)
    shutil.copy2(REGISTRY,report_dir/'registry_before.json')
    annotation=annotation_path(source)
    shutil.copy2(annotation,report_dir/'annotations_before.json')
    before=tree_hashes(source)
    review_hash=sha256(annotation)
    report={'source':str(source),'destination':str(destination),'state':'preparing',
            'legacy_folders':len(candidates),'unique_successful_episodes':len(chosen),
            'duplicates':[{'source_episode':ep,'display_number':ep+1,'folders':[str(i['root']) for i in items],
                'kept':str(max(items,key=lambda i:(i['saved_ns'],str(i['root'])))['root'])}
                for ep,items in sorted(grouped.items()) if len(items)>1],
            'source_sha256_before':before,'archive_manifests':[],'hardware_accessed':False}
    report_path=report_dir/'report.json'
    atomic_json(report_path,report)
    with collection_lock(home,source):
        workspace=home/('.pending-crop-update-migration-'+report_dir.name)
        workspace.mkdir()
        entries=[]
        from data_tools import inspect_dataset
        for item in chosen:
            entry=dict(item['record'])
            selected=inspect_dataset(item['root'],0)['selected']
            entry['video_sha256']={key:sha256(item['root']/value['path']) for key,value in selected['videos'].items()}
            entry['migrated_from']=str(item['root'])
            entries.append(entry)
        staged=workspace/'assembled'
        assemble([item['root'] for item in chosen],staged)
        manifest=prepare_manifest(source,entries,plan['dataset_spec'])
        atomic_json(staged/COLLECTION_FILE,manifest)
        write_signal_snapshot(staged,manifest)
        report['checks']=validate_collection(staged,manifest)
        if tree_hashes(source)!=before or sha256(annotation)!=review_hash:
            raise ValueError('迁移期间原始数据或审核标记变化，不提交')
        publish(staged,destination,manifest,workspace)
        store_metadata(destination,plan['dataset_spec'],source='crop_collection_migration',
                       lineage={'operation':'crop_collection','source':str(source)})
        report['state']='published_archiving_legacy'
        atomic_json(report_path,report)
        for item in candidates:
            # Exact verified derived folders only; raw/training roots are never candidates.
            record=archive_dataset(cfg,item['root'],item['root'].name)
            report['archive_manifests'].append(record)
            atomic_json(report_path,report)
        report['raw_files_unchanged']=tree_hashes(source)==before
        report['reviews_unchanged']=sha256(annotation)==review_hash
        report['all_crops_inherit_success']=all(v['result']=='success' for v in read_reviews(destination).values())
        if not all(report[k] for k in ('raw_files_unchanged','reviews_unchanged','all_crops_inherit_success')):
            raise ValueError('迁移后的原始数据或审核一致性验证失败')
        report['state']='complete'
        atomic_json(report_path,report)
        remove_workspace(workspace)
    return {'report':str(report_path),'destination':str(destination),'episodes':len(chosen),
            'duplicates_removed':sum(len(v)-1 for v in grouped.values()),'recoverable_archived_folders':len(candidates),
            'checks':report['checks'],'raw_files_unchanged':report['raw_files_unchanged']}


if __name__=='__main__':
    cfg=load_config()
    print(json.dumps(migrate(cfg,Path(cfg['dataset_home'])/'so101_supplement_20261015'),ensure_ascii=False),flush=True)
