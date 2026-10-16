"""LeRobot v3 read-only adapter; PyArrow is loaded only in a short-lived process."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import NAMES, robot_path
from signal_tools import parquet_signals, sidecar_signals


def inspect_dataset(root, episode=None):
    import pyarrow.parquet as pq
    root = robot_path(root, existing=True)
    info = json.loads((root / "meta/info.json").read_text(encoding="utf-8"))
    if info.get("codebase_version") != "v3.0":
        raise ValueError("第一版查看器只接入本机现有 LeRobot v3.0，不猜测旧格式")
    rows = []
    for file in sorted((root / "meta/episodes").glob("chunk-*/*.parquet")):
        schema = pq.read_schema(file)
        columns = [n for n in schema.names if not n.startswith("stats/")]
        rows.extend(pq.read_table(file, columns=columns).to_pylist())
    rows.sort(key=lambda r: r["episode_index"])
    video_keys = [k for k, v in info["features"].items() if v.get("dtype") == "video"]
    entries = []
    for row in rows:
        item = {"index": int(row["episode_index"]), "frames": int(row["length"]),
                "duration": row["length"] / info["fps"], "tasks": row.get("tasks", []), "videos": {}}
        for key in video_keys:
            prefix = "videos/" + key
            relative = info["video_path"].format(video_key=key,
                chunk_index=row[prefix + "/chunk_index"], file_index=row[prefix + "/file_index"])
            path = (root / relative).resolve()
            if not path.is_relative_to(root):
                raise ValueError("视频路径越界")
            item["videos"][key] = {"path": relative, "start": row[prefix + "/from_timestamp"],
                                   "end": row[prefix + "/to_timestamp"], "exists": path.is_file()}
        entries.append(item)
    result = {"root": str(root), "name": root.name, "version": info["codebase_version"],
              "fps": info["fps"], "total_episodes": info["total_episodes"],
              "total_frames": info["total_frames"], "indexed_episodes": len(rows),
              "cameras": video_keys, "episodes": entries, "features": info["features"]}
    if episode is not None:
        matching = [r for r in rows if r["episode_index"] == episode]
        if len(matching) != 1:
            raise ValueError("Episode 不存在或索引不唯一")
        row = matching[0]
        relative = info["data_path"].format(chunk_index=row["data/chunk_index"], file_index=row["data/file_index"])
        file = (root / relative).resolve()
        if not file.is_relative_to(root):
            raise ValueError("数据路径越界")
        numeric = [k for k, spec in info["features"].items() if spec.get("dtype") not in ("video", "image", "string")]
        schema = pq.read_schema(file)
        columns = list(dict.fromkeys(["episode_index", "timestamp", "observation.state", "action"] + [k for k in numeric if k in schema.names]))
        table = pq.read_table(file, columns=columns)
        table = table.filter(__import__("pyarrow.compute", fromlist=["equal"]).equal(table["episode_index"], episode))
        result["trajectory"] = {"names": list(NAMES), "time": table["timestamp"].to_pylist(),
                                "state": table["observation.state"].to_pylist(), "action": table["action"].to_pylist()}
        result["selected"] = next(e for e in entries if e["index"] == episode)
        result["frame_count_matches"] = len(table) == row["length"]
        result["signals"] = parquet_signals(info, table, result["trajectory"]["time"])
        existing = {item["key"] for item in result["signals"]}
        result["signals"].extend(item for item in sidecar_signals(root, episode, result["selected"]["duration"]) if item["key"] not in existing)
        crop_signals=root/'extras/crop_signals.json'
        if crop_signals.is_file():
            existing={item['key'] for item in result['signals']}
            result['signals'].extend(item for item in json.loads(crop_signals.read_text(encoding='utf-8')).get(str(episode),[]) if item['key'] not in existing)
        result["signal_note"] = "原始 state/action 不变；raw 电流/负载不是标定接触力，Torque_Enable 只是开关。历史未记录的字段显示缺失，不补零。"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--episode", type=int)
    args = parser.parse_args()
    print(json.dumps(inspect_dataset(args.root, args.episode), ensure_ascii=False, allow_nan=False))
