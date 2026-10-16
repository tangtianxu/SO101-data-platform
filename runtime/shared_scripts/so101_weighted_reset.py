"""Explicit staged/weighted reset for the currently approved real-arm setup.

This is not a collision planner. The operator must clear/check the route.
Only an initial shoulder lift and monotonically weighted joint restoration
are supported. No reverse-path replay, model inference or servo limit changes.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import threading
import time
from pathlib import Path

import numpy as np
import so101_guarded_real_act as act
import so101_profile as profile

NAMES = act.NAMES
SPEEDS = np.array([4.0, 3.2, 2.4, 1.6, 0.8, 10.0])
WEIGHTS = np.array([1.0, 0.8, 0.6, 0.4, 0.2])
SOURCE = profile.resource("reset_source", "logs/select_a_reset_source")


def weighted_targets(current, target, speeds=SPEEDS):
    current, target = np.asarray(current, dtype=float), np.asarray(target, dtype=float)
    if current.shape != (6,) or target.shape != (6,) or not np.isfinite([current, target]).all():
        raise ValueError("Reset endpoints must be six finite values")
    speeds = np.asarray(speeds, dtype=float)
    if speeds.shape != (6,) or not np.isfinite(speeds).all() or np.any(speeds <= 0):
        raise ValueError("Reset speeds must be six finite, positive values")
    delta = target-current
    duration = float(np.max(abs(delta)/speeds))
    count = max(math.ceil(duration/act.PERIOD), 1)
    for index in range(count):
        elapsed = (index+1)*act.PERIOD
        yield current+np.sign(delta)*np.minimum(abs(delta), speeds*elapsed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("lift", "restore"), required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--checked-lift-report", type=Path,
                        help="Use the measured lifted pose after the operator separately confirms physical clearance")
    parser.add_argument("--empty-gripper-confirmed", action="store_true",
                        help="Required for fast-gripper restoration: operator confirms an empty, unobstructed gripper")
    args = parser.parse_args()
    if args.execute and args.phase == "restore" and not args.empty_gripper_confirmed:
        raise ValueError("Fast gripper movement requires an empty, unobstructed gripper confirmation")
    calibration = json.loads(act.shadow.CALIBRATION.read_text(encoding="utf-8"))
    prior = json.loads((SOURCE/"report.json").read_text(encoding="utf-8"))
    if prior["calibration"] != calibration or prior["usb_serial"] != act.EXPECTED_FOLLOWER_SERIAL:
        raise RuntimeError("Reference robot coordinates changed")
    target = np.array([prior["initial_measured"][n] for n in NAMES])
    limits = act.limits_from_calibration(calibration)
    if np.any(target < limits[:, 0]) or np.any(target > limits[:, 1]):
        raise RuntimeError("Reference start is out of unchanged bounds")
    print("WEIGHTED_RESET_PLAN " + json.dumps({"phase": args.phase, "weights": WEIGHTS.tolist(),
          "speeds_deg_s_and_gripper_percent_s": SPEEDS.tolist(), "start_target": dict(zip(NAMES, target.tolist())),
          "no_reverse_replay": True, "collision_clearance_not_certified": True,
          "requires_operator_clear_route": True, "fast_gripper_only_when_empty_and_clear": True,
          "exit_holds_until_operator_confirms_support_for_separate_release": True}), flush=True)
    if not args.execute:
        return
    if args.output is None or args.output.exists():
        raise ValueError("Use a new workspace output directory")
    profile.output_path(args.output)
    port = profile.resolve_port("follower", act.shadow.list_ports.comports())
    args.output.mkdir(parents=True)
    bus = act.shadow.FeetechMotorsBus(port, {
        n: act.shadow.Motor(calibration[n]["id"], "sts3215", act.shadow.MotorNormMode.RANGE_0_100
                           if n == "gripper" else act.shadow.MotorNormMode.DEGREES) for n in NAMES
    }, calibration={n: act.shadow.MotorCalibration(**calibration[n]) for n in NAMES})
    act.guard_bus_writes(bus)
    stop = threading.Event()
    motion = act.Motion(bus, calibration, stop)
    listener = None
    report = {"operation": "operator-authorized staged weighted reset", "phase": args.phase,
              "source_trial": str(SOURCE), "arm_weights": dict(zip(NAMES[:5], WEIGHTS.tolist())),
              "speeds": dict(zip(NAMES, SPEEDS.tolist())), "boundary_changes": False,
              "collision_clearance_not_certified": True, "exit_releases_torque": False,
              "post_reset_torque_policy": "Release separately after operator confirms support; do not release automatically while unsupported",
              "empty_gripper_confirmed": args.empty_gripper_confirmed,
              "outcome": "preflight_failed"}
    try:
        from pynput import keyboard
        listener = keyboard.Listener(on_press=lambda key: stop.set() if key == keyboard.Key.esc else None)
        listener.start()
        bus.connect(handshake=False)
        report["preflight"], _ = act.preflight(bus, calibration)
        current = bus.sync_read("Present_Position", num_retry=1)
        current = np.array([current[n] for n in NAMES])
        last_raw = prior["final_feedback"]["position_raw"]
        endpoint = np.array([(last_raw[n]-(calibration[n]["range_min"]+calibration[n]["range_max"])/2)*360/4095
                             if n != "gripper" else (last_raw[n]-calibration[n]["range_min"])*100/
                             (calibration[n]["range_max"]-calibration[n]["range_min"]) for n in NAMES])
        if args.phase == "lift":
            if np.any(abs(current-endpoint) > np.array([5.0]*5+[20.0])):
                raise RuntimeError("Current pose moved away from the checked box-side pose")
            destination = current.copy()
            # At this inspected box-side pose, -6 degrees shoulder_lift raises
            # the end effector in the official FK model. Not a generic rule.
            if target[1] >= current[1]:
                raise RuntimeError("Checked shoulder direction no longer applies")
            destination[1] -= 6.0
            speeds = np.array([4.0]*5+[10.0])
        else:
            if args.checked_lift_report is None:
                raise RuntimeError("Restore requires the separately checked lift report")
            checked = json.loads(args.checked_lift_report.read_text(encoding="utf-8"))
            if checked.get("phase") != "lift" or checked.get("source_trial") != str(SOURCE):
                raise RuntimeError("Checked lift report belongs to a different operation")
            raw_lifted = checked["final_feedback"]["position_raw"]
            lift_endpoint = np.array([(raw_lifted[n]-(calibration[n]["range_min"]+calibration[n]["range_max"])/2)*360/4095
                                      if n != "gripper" else (raw_lifted[n]-calibration[n]["range_min"])*100/
                                      (calibration[n]["range_max"]-calibration[n]["range_min"]) for n in NAMES])
            if np.any(abs(current-lift_endpoint) > np.array([2.0]*5+[5.0])):
                raise RuntimeError("Current pose is not the separately checked lifted pose")
            report["operator_checked_lift_report"] = str(args.checked_lift_report)
            destination, speeds = target, SPEEDS
        if np.any(destination < limits[:, 0]) or np.any(destination > limits[:, 1]):
            raise RuntimeError("Reset destination outside current bounds")
        report["initial_measured"] = dict(zip(NAMES, current.tolist()))
        report["destination"] = dict(zip(NAMES, destination.tolist()))
        report["planned_duration_s"] = float(np.max(abs(destination-current)/speeds))
        motion.start_hold()
        start = time.perf_counter()
        last_notice = start
        frames = 0
        print(f"WEIGHTED_RESET_START {args.phase}; estimated={report['planned_duration_s']:.1f}s; Esc stops", flush=True)
        with (args.output/"reset.csv").open("x", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["step", "elapsed_s", *[f"goal_{n}" for n in NAMES], *[f"state_{n}" for n in NAMES]])
            for goal in weighted_targets(current, destination, speeds):
                tick = time.perf_counter()
                actual = bus.sync_read("Present_Position", num_retry=1)
                motion.send(goal, source_time=tick)
                elapsed = time.perf_counter()-start
                writer.writerow([frames, elapsed, *goal, *[actual[n] for n in NAMES]])
                frames += 1
                if time.perf_counter()-last_notice >= 10:
                    print(f"WEIGHTED_RESET {args.phase} {elapsed:.1f}/{report['planned_duration_s']:.1f}s", flush=True)
                    last_notice = time.perf_counter()
            stable = 0
            verify_deadline = time.perf_counter()+15
            while stable < 3:
                tick = time.perf_counter()
                measured = bus.sync_read("Present_Position", num_retry=1)
                errors = abs(np.array([measured[n] for n in NAMES])-destination)
                stable = stable+1 if np.all(errors <= act.ALIGN_TOLERANCE) else 0
                motion.send(destination, source_time=tick)
                if time.perf_counter() > verify_deadline:
                    raise TimeoutError("Reset did not settle")
            report["alignment_errors"] = dict(zip(NAMES, errors.tolist()))
            report["elapsed_s"], report["reset_frames"] = time.perf_counter()-start, frames
        report["outcome"] = "completed"
    except KeyboardInterrupt:
        report["outcome"] = "user_stopped"
    except BaseException as error:
        report["outcome"] = "failed_after_enable" if motion.enabled else "preflight_failed"
        report["failure"] = repr(error)
        print(f"WEIGHTED_RESET_STOPPED {error}", flush=True)
    finally:
        try:
            report["final_feedback"] = motion.hold_on_exit()
        except Exception as error:
            report["hold_failure"] = repr(error)
        if listener is not None:
            listener.stop()
        if bus.is_connected:
            bus.disconnect(disable_torque=False)
        report["goal_register_writes"] = motion.goal_writes
        report["health_records"] = motion.health_records
        with (args.output/"report.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        print(f"WEIGHTED_RESET_FINISHED {report['outcome']}; torque retained; report={args.output/'report.json'}", flush=True)
    if report["outcome"] not in ("completed", "user_stopped"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
