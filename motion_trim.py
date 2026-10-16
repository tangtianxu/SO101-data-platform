"""Read-only six-joint motion boundaries and auditable offline crop plans."""
from bisect import bisect_left, bisect_right
from collections import deque
import json
import math
from pathlib import Path
from statistics import median

from common import NAMES, atomic_json, number, robot_path
from review_store import read_collection, read_reviews, require_success

DEFAULTS = {"arm_delta": .5, "gripper_delta": 1., "window_s": .2,
            "min_motion_s": .15, "pad_before_s": .3, "pad_after_s": .5,
            "trim_tail": True, "signal": "state"}


def settings(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("自动裁剪包含未知参数")
    out = {**DEFAULTS, **value}
    for key, lo, hi in (("arm_delta", .01, 30), ("gripper_delta", .01, 50),
                        ("window_s", .05, 2), ("min_motion_s", .05, 2),
                        ("pad_before_s", 0, 10), ("pad_after_s", 0, 10)):
        try:
            out[key] = number(out[key], lo, hi, key)
        except (TypeError, OverflowError):
            raise ValueError("自动裁剪参数必须是有效数值") from None
    if type(out["trim_tail"]) is not bool or out["signal"] not in ("state", "state_or_action"):
        raise ValueError("裁剪方向或运动信号无效")
    return out


def episode_ids(value, total, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError("请至少选择一条轨迹")
    if any(type(i) is not int or not 0 <= i < total for i in value) or len(set(value)) != len(value):
        raise ValueError("轨迹编号重复、无效或越界")
    return sorted(value)


def boundary(times, state, fps, params=None, action=None):
    """Return one frame-aligned interval; never remove a pause within the task."""
    p = settings(params)
    n = len(times)
    if not math.isfinite(float(fps)) or fps <= 0 or n < 3 or len(state) != n:
        raise ValueError("帧率、帧数或位置数据不完整")
    t = [float(v) for v in times]
    if any(not math.isfinite(v) or abs(v-i/fps) > .1/fps for i,v in enumerate(t)):
        raise ValueError("时间戳缺失、不连续或不是从零开始的逐帧时间")
    streams = [state] + ([action] if p["signal"] == "state_or_action" else [])
    active = [False] * n
    for rows in streams:
        if rows is None or len(rows) != n or any(len(row) != 6 for row in rows):
            raise ValueError("需要完整的六关节位置")
        values = [[float(v) for v in row] for row in rows]
        if any(not math.isfinite(v) for row in values for v in row):
            raise ValueError("关节位置包含缺失或非有限数值")
        smooth = [[median(values[k][j] for k in range(max(0,i-1),min(n,i+2)))
                   for j in range(6)] for i in range(n)]
        mins, maxs = [deque() for _ in range(6)], [deque() for _ in range(6)]
        left = 0
        for i in range(n):
            while t[i] - t[left] > p["window_s"] + 1e-8:
                left += 1
            for j in range(6):
                low, high = mins[j], maxs[j]
                while low and low[0] < left: low.popleft()
                while high and high[0] < left: high.popleft()
                while low and smooth[low[-1]][j] >= smooth[i][j]: low.pop()
                while high and smooth[high[-1]][j] <= smooth[i][j]: high.pop()
                low.append(i); high.append(i)
                threshold = p["gripper_delta"] if j == 5 else p["arm_delta"]
                active[i] |= smooth[high[0]][j] - smooth[low[0]][j] >= threshold
    runs, start = [], None
    for i, on in enumerate([*active, False]):
        if on and start is None: start = i
        elif not on and start is not None:
            if (i-start)/fps + 1e-8 >= p["min_motion_s"]:
                runs.append((start,i))
            start = None
    if not runs:
        return {"status":"skip", "reason":"未检出持续运动；不猜测边界", "frames":n, "duration":n/fps}
    # Trailing comparison windows can detect a start late. Include the whole window.
    onset = max(0., t[runs[0][0]] - p["window_s"])
    end_motion = min(n/fps, runs[-1][1]/fps)
    first = max(0, bisect_right(t,max(0.,onset-p["pad_before_s"]))-1)
    last = min(n,bisect_left(t,end_motion+p["pad_after_s"])+1) if p["trim_tail"] else n
    if last-first < 2:
        return {"status":"skip","reason":"建议保留区间不足两帧","frames":n,"duration":n/fps}
    return {"status":"ready","start":t[first],"end":t[last] if last<n else n/fps,
            "first_frame":first,"last_frame":last,"kept_frames":last-first,
            "frames":n,"duration":n/fps,"trimmed":first>0 or last<n,
            "motion_start":onset,"motion_end":end_motion,
            "reason":"裁去首尾静止段；内部停顿保留" if first>0 or last<n else "未发现可裁静止段，整条保留"}


def _file(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("来源文件不存在或路径越界")
    return path


def evidence(root, paths):
    from cropping import sha256
    result = {}
    for path in sorted(set(paths)):
        stat = path.stat()
        result[str(path.relative_to(root))] = {"size":stat.st_size,"mtime_ns":stat.st_mtime_ns,"sha256":sha256(path)}
    return result


def verify_evidence(root, expected, *, stop=None):
    from cropping import sha256
    for relative, record in expected.items():
        if stop and stop.is_set(): raise InterruptedError("已取消，原始数据和旧裁剪集不变")
        path = _file(root,relative)
        if path.stat().st_size != record["size"] or sha256(path) != record["sha256"]:
            raise ValueError("来源数值/索引已变化，请重新分析后裁剪")


def analyze(request, *, stop=None, progress=None):
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    root = robot_path(request["dataset_root"],existing=True)
    info = json.loads((root/"meta/info.json").read_text(encoding="utf-8"))
    p = settings(request["settings"])
    keys = ["observation.state"] + (["action"] if p["signal"] == "state_or_action" else [])
    for key in keys:
        feature = info["features"].get(key,{})
        names = feature.get("names",[])
        if feature.get("shape") != [6] or not isinstance(names,list) or [n.removesuffix(".pos") for n in names] != list(NAMES):
            raise ValueError("关节顺序或六维字段不匹配；请先确认前五关节单位为度、夹爪为百分比")
    if info.get("codebase_version") != "v3.0": raise ValueError("仅支持 LeRobot v3.0")
    metadata, paths = {}, [root/"meta/info.json"]
    for file in sorted((root/"meta/episodes").glob("chunk-*/*.parquet")):
        paths.append(file)
        for row in pq.read_table(file,columns=["episode_index","length","data/chunk_index","data/file_index"]).to_pylist():
            ep = int(row["episode_index"])
            if ep in metadata: raise ValueError("轨迹索引重复")
            metadata[ep] = row
    for ep in request["episodes"]:
        if ep not in metadata: raise ValueError("轨迹索引缺失")
        row = metadata[ep]
        paths.append(_file(root,info["data_path"].format(chunk_index=row["data/chunk_index"],file_index=row["data/file_index"])))
    original = evidence(root,paths)
    rows, cache_path, table = [], None, None
    reviews = read_reviews(root)
    for index, ep in enumerate(request["episodes"],1):
        if stop and stop.is_set(): raise InterruptedError("运动边界分析已取消")
        if reviews.get(str(ep),{}).get("result") != "success":
            rows.append({"episode":ep,"status":"skip","reason":"不再标记为成功"})
        else:
            row = metadata[ep]
            path = _file(root,info["data_path"].format(chunk_index=row["data/chunk_index"],file_index=row["data/file_index"]))
            if cache_path != path:
                table = pq.read_table(path,columns=["episode_index","timestamp",*keys]); cache_path = path
            data = table.filter(pc.equal(table["episode_index"],ep))
            try:
                if len(data) != row["length"]: raise ValueError("位置帧数与元数据不一致")
                result = boundary(data["timestamp"].to_pylist(),data["observation.state"].to_pylist(),info["fps"],p,
                                  data["action"].to_pylist() if "action" in keys else None)
                rows.append({"episode":ep,**result})
            except (ValueError,TypeError,OverflowError) as error:
                rows.append({"episode":ep,"status":"skip","reason":str(error)})
        if progress: progress(index,len(request["episodes"]))
    verify_evidence(root,original,stop=stop)
    return {"schema_version":1,"operation":"motion_trim_plan","dataset_root":str(root),"settings":p,
            "collection_plan":request["collection_plan"],"rows":rows,"evidence":original,
            "ready_count":sum(row["status"]=="ready" for row in rows)}


def read_preview(job, job_id):
    if not job or job.get("id") != job_id or not job.get("finished") or job.get("result",{}).get("outcome") != "motion_preview":
        raise ValueError("分析任务已变化或未完成，请重新分析")
    file = robot_path(Path(job["output"])/"motion_plan.json",existing=True)
    if file.stat().st_size > 32*1024*1024: raise ValueError("分析计划文件过大")
    plan = json.loads(file.read_text(encoding="utf-8"))
    if plan.get("schema_version") != 1 or plan.get("operation") != "motion_trim_plan":
        raise ValueError("分析计划无效")
    return plan


def prepare_job(cfg, payload, previous_job):
    from crop_collection import plan_crop
    from dataset_library import child_folder
    root = child_folder(cfg,payload.get("dataset_root",""))
    mode = payload.get("mode","manual")
    if mode not in ("manual","motion_preview","motion_apply"): raise ValueError("未知裁剪模式")
    if mode == "manual":
        ep = number(payload.get("episode",-1),0,100000,"episode",True)
        require_success(root,ep); plan = plan_crop(cfg,root,ep)
        if payload.get("destination") and robot_path(payload["destination"]) != Path(plan["destination"]):
            raise ValueError("裁剪统一保存到本来源的裁剪集合")
        start = number(payload.get("start",-1),0,7200,"裁剪起点")
        end = number(payload.get("end",-1),0,7200,"裁剪终点")
        if start >= end: raise ValueError("裁剪终点应大于起点")
        return {**payload,"dataset_root":str(root),"episode":ep,"start":start,"end":end,
                "destination":plan["destination"],"dataset_spec":plan["dataset_spec"],"crop_plan":plan}
    if read_collection(root) or (root/"crop_provenance.json").exists():
        raise ValueError("请返回原始数据集进行自动裁剪，不能再次裁剪副本")
    info = json.loads((root/"meta/info.json").read_text(encoding="utf-8"))
    if mode == "motion_preview":
        requested = episode_ids(payload.get("episodes"),info["total_episodes"],optional=True)
        reviews = read_reviews(root)
        ids = [i for i in (requested if requested is not None else range(info["total_episodes"]))
               if reviews.get(str(i),{}).get("result") == "success"]
        if not ids: raise ValueError("范围内没有标记成功的原始轨迹")
        plan = plan_crop(cfg,root,ids[0])
        return {"mode":mode,"dataset_root":str(root),"episodes":ids,
                "settings":settings(payload.get("settings")),"collection_plan":plan}
    plan = read_preview(previous_job,payload.get("preview_job_id"))
    if robot_path(plan["dataset_root"],existing=True) != root: raise ValueError("分析来源与当前数据集不一致")
    ids = episode_ids(payload.get("episodes"),info["total_episodes"])
    choices = {row["episode"]:row for row in plan["rows"] if row["status"]=="ready"}
    if any(i not in choices for i in ids): raise ValueError("只能裁剪本次分析中可用的轨迹")
    for ep in ids: require_success(root,ep)
    current = plan_crop(cfg,root,ids[0])
    expected = plan["collection_plan"]
    if current["exists"] and current["destination"] != expected["destination"]:
        raise ValueError("裁剪集合已变化，请重新分析")
    return {"mode":mode,"dataset_root":str(root),"plan":{**plan,"rows":[choices[i] for i in ids]},
            "dataset_spec":expected["dataset_spec"],"crop_plan":expected}


def materialize(payload, out):
    if payload.get("mode") not in ("motion_preview","motion_apply"): return payload
    file = Path(out)/"crop_request.json"
    atomic_json(file,payload)
    return {key:value for key,value in payload.items() if key in ("mode","dataset_root","dataset_spec")} | {"request_file":str(file)}


def run_job(cfg, options, bridge, out):
    from crop_collection import save_crop, save_crops
    if options.get("mode","manual") == "manual":
        bridge.state("CROPPING","离线裁剪，原始数据不变；正在准备编码器")
        return save_crop(cfg,options["dataset_root"],options["episode"],options["start"],options["end"],
                         stop=bridge.stop,progress=lambda done,total:bridge.emit("telemetry",crop_done=done,crop_total=total),
                         expected_plan=options.get("crop_plan"))
    file = robot_path(options["request_file"],existing=True)
    if file.parent != Path(out).resolve() or file.name != "crop_request.json":
        raise ValueError("裁剪任务文件不属于当前会话")
    request = json.loads(file.read_text(encoding="utf-8"))
    if options["mode"] == "motion_preview":
        bridge.state("MOTION_ANALYZING","只读分析六关节运动；不修改审核和数据")
        plan = analyze(request,stop=bridge.stop,
                       progress=lambda done,total:bridge.emit("telemetry",analysis_done=done,analysis_total=total))
        atomic_json(Path(out)/"motion_plan.json",plan)
        return {"outcome":"motion_preview","source":plan["dataset_root"],"ready_count":plan["ready_count"],
                "skipped_count":len(plan["rows"])-plan["ready_count"]}
    plan = request["plan"]; root = robot_path(plan["dataset_root"],existing=True)
    verify_evidence(root,plan["evidence"],stop=bridge.stop)
    requests = [{"episode":r["episode"],"start":r["start"],"end":r["end"],
                 "automatic_trim":{"method":"six_joint_motion_v1","settings":plan["settings"],
                                   "motion_start":r["motion_start"],"motion_end":r["motion_end"]}} for r in plan["rows"]]
    bridge.state("CROPPING","批量裁剪：只写成功轨迹；全部校验完成后统一提交")
    result = save_crops(cfg,root,requests,stop=bridge.stop,
        progress=lambda done,total:bridge.emit("telemetry",crop_done=done,crop_total=total),
        on_episode=lambda index,total,ep:bridge.state("CROPPING",f"批量裁剪 {index}/{total} · 原始第 {ep+1} 条",batch_index=index,batch_count=total),
        on_stage=bridge.state,
        before_publish=lambda:verify_evidence(root,plan["evidence"],stop=bridge.stop),
        expected_plan=request["crop_plan"])
    result.pop("entries",None)  # Full provenance stays in the collection, not every UI poll.
    return result
