"""Read signal evidence without manufacturing historical measurements."""
import csv
import json
from pathlib import Path

from common import NAMES, ROBOT_HOME
from sensors import LABELS


def description(key):
    short = key.removeprefix("observation.sensors.")
    return LABELS.get(short, (short, "原始单位"))


def sidecar_signals(root, episode, duration):
    samples = []
    for file in sorted((root / "extras/platform").glob("*.jsonl")):
        with file.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # An interrupted final line is not a measured zero.
                if row.get("episode") == episode and row.get("sample"):
                    sample = row["sample"]
                    samples.append((float(row["time"]), sample.get("perf_time"), sample["health"]))
    if samples:
        return health_series(samples, "extras/platform：原始反馈样本；保留真实采样间隔")
    # Only this known dataset is linked to the original recording logs. Never scan arbitrary disk paths.
    if root.resolve() != (ROBOT_HOME / "datasets/so101_supplement_20261015").resolve():
        return []
    for directory in sorted((ROBOT_HOME / "logs/supplement_20261015").glob("G[1-6]_20261015_*")):
        report_path, diag = directory / "report.json", directory / "teleoperation.csv"
        if not report_path.is_file() or not diag.is_file():
            continue
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not report.get("episodes_before", -1) <= episode < report.get("episodes_after", -1):
            continue
        start = None
        with diag.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                if row["phase"] == "RECORDING" and int(row["episode_index"]) == episode:
                    start = float(row["observation_read_perf"] or row["perf_time"])
                    break
        if start is None:
            continue
        for sample in report.get("health", []):
            t = sample.get("perf_time", -1) - start
            if 0 <= t <= duration:
                health = {"temperature": sample.get("temperature", {}), "status": sample.get("status", {}),
                          "torque_enabled": sample.get("torque", {})}
                samples.append((t, sample["perf_time"], health))
    return health_series(samples, "历史会话健康日志：约 2 Hz；按单调时钟对齐到本条起点，非逐帧测量")


def health_series(samples, source):
    result = []
    for key in LABELS:
        times, values, last = [], [], object()
        for t, stamp, health in sorted(samples):
            if stamp == last or key not in health:
                continue
            last = stamp
            value = health[key]
            if not all(n in value and isinstance(value[n], (int, float)) for n in NAMES):
                continue
            times.append(t)
            values.append([value[n] for n in NAMES])
        if times:
            label, unit = LABELS[key]
            result.append({"key": "observation.sensors."+key, "label": label, "unit": unit,
                "names": list(NAMES), "time": times, "values": values, "source": source})
    return result


def parquet_signals(info, table, stamps):
    result = []
    for key, spec in info["features"].items():
        if key in ("observation.state", "action", "timestamp", "episode_index", "frame_index", "index", "task_index"):
            continue
        if spec.get("dtype") not in ("float32", "float64", "int32", "int64", "bool") or key not in table.column_names:
            continue
        shape = spec.get("shape", [])
        if len(shape) != 1 or not 1 <= shape[0] <= 32:
            continue
        label, unit = description(key)
        values = table[key].to_pylist()
        result.append({"key": key, "label": label, "unit": unit, "names": spec.get("names", list(range(shape[0]))),
                       "time": stamps, "values": values, "source": "数据集 Parquet；反馈频率独立于视频，参考 feedback_age_s"})
    return result
