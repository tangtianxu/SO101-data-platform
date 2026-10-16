"""Frame-aligned, non-destructive LeRobot v3 cropping through the official writer."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import secrets

from common import atomic_json, robot_path
from data_tools import inspect_dataset

AUTO = {"timestamp", "frame_index", "episode_index", "index", "task_index"}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1048576):
            digest.update(chunk)
    return digest.hexdigest()


def video_frames(path, desired, fps):
    import av
    tolerance = .51 / fps
    with av.open(str(path), mode="r") as container:
        container.seek(max(0, int((desired[0] - 1 / fps) * 1000000)), backward=True)
        index = 0
        for frame in container.decode(video=0):
            if frame.time is None:
                raise ValueError("视频缺少时间戳，拒绝猜测帧对齐")
            when = float(frame.time)
            if when < desired[index] - tolerance:
                continue
            if abs(when - desired[index]) > tolerance:
                raise ValueError(f"视频帧无法对齐：目标 {desired[index]:.6f}s / 实际 {when:.6f}s")
            yield frame.to_ndarray(format="rgb24")
            index += 1
            if index == len(desired):
                return
        raise ValueError("视频提前结束，裁剪没有提交")


def crop_episode(source, destination, episode, start, end, stop=None, progress=None):
    import numpy as np
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    from lerobot.configs.video import RGBEncoderConfig
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    source, destination = robot_path(source, existing=True), robot_path(destination)
    if source == destination or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("裁剪输出必须是原始数据集之外的新目录")
    if destination.exists():
        raise FileExistsError("输出目录已存在，拒绝覆盖")
    if not all(math.isfinite(float(v)) for v in (start, end)):
        raise ValueError("裁剪时间必须为有限数值")
    catalog = inspect_dataset(source, episode)
    selected = catalog["selected"]
    if not 0 <= start < end <= selected["duration"] + 1e-5:
        raise ValueError("裁剪范围应满足 0 ≤ 起点 < 终点 ≤ 本条时长")
    info = json.loads((source / "meta/info.json").read_text(encoding="utf-8"))
    rows = []
    for file in sorted((source / "meta/episodes").glob("chunk-*/*.parquet")):
        table = pq.read_table(file)
        rows.extend(table.filter(pc.equal(table["episode_index"], episode)).to_pylist())
    if len(rows) != 1:
        raise ValueError("episode 元数据不唯一")
    row = rows[0]
    data_path = (source / info["data_path"].format(chunk_index=row["data/chunk_index"], file_index=row["data/file_index"])).resolve()
    if not data_path.is_relative_to(source):
        raise ValueError("数据路径越界")
    table = pq.read_table(data_path)
    table = table.filter(pc.equal(table["episode_index"], episode))
    stamps = np.array(table["timestamp"].to_pylist(), dtype=float)
    keep = np.flatnonzero((stamps >= start - 1e-7) & (stamps < end - 1e-7))
    if len(keep) < 2 or np.any(np.diff(keep) != 1):
        raise ValueError("至少保留两帧连续数据")
    first, last = int(keep[0]), int(keep[-1]) + 1
    features = {k: copy.deepcopy(v) for k, v in info["features"].items() if k not in AUTO}
    unsupported = [k for k, v in features.items() if v["dtype"] not in ("video", "float32", "float64", "int64", "int32", "bool")]
    if unsupported:
        raise ValueError("尚未支持的特征，拒绝静默丢弃：" + str(unsupported))
    paths = [source / "meta/info.json", data_path]
    streams = {}
    for key, video in selected["videos"].items():
        path = (source / video["path"]).resolve()
        if not path.is_relative_to(source):
            raise ValueError("视频路径越界")
        paths.append(path)
        desired = [float(video["start"] + t) for t in stamps[first:last]]
        streams[key] = video_frames(path, desired, info["fps"])
    original_hashes = {str(p.relative_to(source)): sha256(p) for p in paths}
    pending = destination.parent / (".pending-crop-" + secrets.token_hex(6))
    writer = None
    provenance = {"operation": "frame_aligned_crop", "source": str(source), "source_episode": int(episode),
        "requested_seconds": [float(start), float(end)], "source_frame_range_half_open": [first, last],
        "frames": last-first, "fps": info["fps"], "original_files_sha256": original_hashes,
        "source_time_origin": float(stamps[first]),
        "raw_trajectory_unchanged": True, "state_action_values_copied_without_filtering": True,
        "timestamp_rebased": True, "video_reencoded": "h264", "status": "pending"}
    try:
        writer = LeRobotDataset.create(repo_id="local/crop_" + __import__('hashlib').sha256(str(destination).encode()).hexdigest()[:16], root=pending, fps=info["fps"],
            features=features, robot_type=info.get("robot_type"), use_videos=True, streaming_encoding=True,
            encoder_threads=2, metadata_buffer_size=1,
            rgb_encoder=RGBEncoderConfig(vcodec="h264", g=30, crf=20, preset="fast"))
        values = table.slice(first, last-first).to_pylist()
        for index, item in enumerate(values):
            if stop and stop.is_set():
                raise InterruptedError("裁剪已取消；原始数据不变，未完成副本不提交")
            frame = {"task": selected["tasks"][0] if selected["tasks"] else "task"}
            for key, spec in features.items():
                frame[key] = next(streams[key]) if spec["dtype"] == "video" else np.asarray(item[key], dtype=spec["dtype"]).reshape(spec['shape'])
            writer.add_frame(frame)
            if progress and (index % 30 == 0 or index + 1 == len(values)):
                progress(index+1, len(values))
        writer.save_episode(parallel_encoding=False)
        writer.finalize()
        derived = inspect_dataset(pending, 0)
        if derived["total_episodes"] != 1 or derived["total_frames"] != last-first or not derived["frame_count_matches"]:
            raise ValueError("裁剪副本完整性校验失败")
        for key in ("action", "observation.state"):
            if not np.array_equal(np.asarray(derived["trajectory"]["action" if key == "action" else "state"]),
                                  np.asarray(table[key].to_pylist()[first:last])):
                raise ValueError("裁剪副本的动作/状态与源片段不一致")
        if any(sha256(source / relative) != expected for relative, expected in original_hashes.items()):
            raise ValueError("检测到源文件发生变化，副本不提交")
        provenance["status"] = "complete"
        atomic_json(pending / "crop_provenance.json", provenance)
        # Only rename this newly generated, checked staging directory. Never move the source.
        if destination.exists() or not pending.resolve().is_relative_to(destination.parent.resolve()):
            raise ValueError("输出目标变化，拒绝提交")
        pending.rename(destination)
        return {"outcome": "cropped", "destination": str(destination), **provenance}
    except BaseException:
        if pending.exists():
            provenance["status"] = "incomplete_not_for_training"
            atomic_json(pending / "crop_provenance.json", provenance)
        raise
    finally:
        if writer is not None:
            writer.finalize()
        for generator in streams.values():
            generator.close()
