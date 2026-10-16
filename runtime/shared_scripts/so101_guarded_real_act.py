"""Bounded real ACT trial with original weights/processors and unchanged servo limits.

Default mode only prints the plan. --execute is required for physical motion.
Only Goal_Position and Torque_Enable registers can be written. No leader port,
calibration/PID writes, automatic home return on exit or torque release are used.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import threading
import time
from pathlib import Path

os.environ.setdefault("HF_HOME", "E:/robot/hf-cache")
os.environ.setdefault("TORCH_HOME", "E:/robot/torch-cache")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TEMP", "E:/robot/temp")
os.environ.setdefault("TMP", "E:/robot/temp")

import cv2
import numpy as np
import torch
import so101_act_shadow as shadow
from so101_guarded_real_replay import EXPECTED_FOLLOWER_SERIAL

NAMES = shadow.NAMES
PERIOD = 1 / 30
ALIGN_SPEED = np.array([2.0] * 5 + [5.0])
ALIGN_TOLERANCE = np.array([1.0] * 5 + [3.0])
# This is a STOP check, not an action speed limiter. Approved normal predictions
# remain unchanged except for the separately approved existing-bound clipping.
MAX_COMMAND_JUMP = np.array([5.0] * 5 + [20.0])
# 525 retained demonstration frames / 30 Hz. Includes placement, release and a
# short stable interval, not an exact annotated success instant or human reset.
MAX_TRIAL_DURATION = 17.5
TRIAL_DURATION_BASIS = {
    "dataset": "E:/robot/datasets/so101_pick_place_clean_no_hand_20260925",
    "definition": "maximum_retained_successful_demonstration_duration",
    "episode_scope": "all_10_clean_demonstrations_including_held_out_episodes",
    "fps": 30, "maximum_frames": 525, "longest_episode_indices": [2, 3],
    "endpoint": "placement_gripper_release_and_short_stable_interval_before_human_reset",
    "exact_success_instant_annotated": False,
    "cleaning_manifest_sha256": "8427DD8E64004AB1A547A80B9A413E6CAF6BA60F09ADAAEC9BA1B0FECAF8A91F",
}
REFERENCE_START = Path("E:/robot/config/reference_start_pose.json")
TEMPERATURE_LIMIT_C = 60
TEMPERATURE_CONFIRMATION_S = 1.0


class TemperatureRecovered(Exception):
    """Internal signal: discard the prediction made before a thermal pause."""


def servo_status_faults(status, allow_gripper_overload=False):
    """Exempt only isolated gripper status 32; preserve every other fault."""
    faults = {name: value for name, value in status.items() if value != 0}
    if (allow_gripper_overload and faults == {"gripper": 32}
            and all(status.get(name) == 0 for name in NAMES if name != "gripper")):
        return {}
    return faults


def log_gripper_overload(status, logger):
    if status.get("gripper") == 32 and logger is not None:
        logger("gripper_overload", status=status.copy(), behavior="log_only",
               interpretation="contact_or_stall_candidate_not_task_success")


def confirm_temperature(bus, temperature, confirmation_s, stop=None, clock=time,
                        records=None, require_torque=False, deadline=None,
                        allow_gripper_overload=False, status_logger=None):
    """No model writes. Confirm sustained heat; two cool samples allow recovery."""
    if all(t < TEMPERATURE_LIMIT_C for t in temperature.values()):
        return temperature
    if confirmation_s <= 0:
        raise RuntimeError("Servo temperature reached 60 C; next goal not sent")
    hot_since = clock.perf_counter()
    cool_count = 0
    print("ACT_TEMPERATURE_RECHECK holding; " + json.dumps(temperature), flush=True)
    while True:
        next_read = clock.perf_counter()+0.1
        while clock.perf_counter() < next_read:
            if stop is not None and stop.is_set():
                raise KeyboardInterrupt
            clock.sleep(min(0.01, max(0, next_read-clock.perf_counter())))
        if stop is not None and stop.is_set():
            raise KeyboardInterrupt
        now = clock.perf_counter()
        if deadline is not None and now >= deadline:
            raise TimeoutError("Trial ended before temperature could be confirmed")
        status = bus.sync_read("Status", normalize=False, num_retry=1)
        temperature = bus.sync_read("Present_Temperature", normalize=False, num_retry=1)
        torque = bus.sync_read("Torque_Enable", normalize=False, num_retry=1)
        if records is not None:
            records.append({"perf_time": now, "status": status, "temperature": temperature,
                            "torque": torque, "temperature_recheck": True})
        if servo_status_faults(status, allow_gripper_overload) or any(t not in (0, 1) for t in torque.values()) or (require_torque and any(t != 1 for t in torque.values())):
            raise RuntimeError("Servo alarm/torque fault during temperature recheck")
        if allow_gripper_overload:
            log_gripper_overload(status, status_logger)
        if any(t >= TEMPERATURE_LIMIT_C for t in temperature.values()):
            cool_count = 0
            if hot_since is None:
                hot_since = now
            if now-hot_since >= confirmation_s:
                raise RuntimeError(f"Servo temperature remained >=60 C for {confirmation_s:g}s; next goal not sent")
        else:
            hot_since = None
            cool_count += 1
            if cool_count >= 2:
                print("ACT_TEMPERATURE_RECOVERED use fresh observation", flush=True)
                return temperature


def limits_from_calibration(calibration):
    return np.array([
        [-(calibration[n]["range_max"]-calibration[n]["range_min"])*180/4095,
         (calibration[n]["range_max"]-calibration[n]["range_min"])*180/4095]
        if n != "gripper" else [0, 100] for n in NAMES
    ], dtype=float)


def bounded_target(candidate, limits):
    candidate = np.asarray(candidate, dtype=float)
    if candidate.shape != (6,) or not np.isfinite(candidate).all():
        raise FloatingPointError("ACT target must contain six finite values")
    target = np.clip(candidate, limits[:, 0], limits[:, 1])
    return target, candidate != target


def make_raw_goal(bus, target, calibration):
    values = bus._unnormalize({calibration[n]["id"]: float(target[i]) for i, n in enumerate(NAMES)})
    raw, rounding_adjustments = {}, 0
    for n in NAMES:
        cal = calibration[n]
        count = values[cal["id"]]
        bounded = min(max(count, cal["range_min"]), cal["range_max"])
        if abs(count-bounded) > 1:
            raise RuntimeError("Encoder target exceeds the approved boundary despite degree clipping")
        raw[n] = bounded
        rounding_adjustments += int(count != bounded)
    return raw, rounding_adjustments


def guard_bus_writes(bus):
    single, multiple = bus.write, bus.sync_write

    def single_write(register, *args, **kwargs):
        if register not in ("Goal_Position", "Torque_Enable"):
            raise RuntimeError(f"Forbidden motor register write: {register}")
        return single(register, *args, **kwargs)

    def multiple_write(register, *args, **kwargs):
        if register not in ("Goal_Position", "Torque_Enable"):
            raise RuntimeError(f"Forbidden motor register write: {register}")
        return multiple(register, *args, **kwargs)

    bus.write, bus.sync_write = single_write, multiple_write


def preflight(bus, calibration, temperature_confirmation_s=0, stop=None,
              allow_gripper_overload=False, status_logger=None):
    for register, key in (("Homing_Offset", "homing_offset"), ("Min_Position_Limit", "range_min"),
                          ("Max_Position_Limit", "range_max")):
        values = bus.sync_read(register, normalize=False, num_retry=1)
        if any(values[n] != calibration[n][key] for n in NAMES):
            raise RuntimeError(f"Live calibration mismatch at {register}")
    health = {r: bus.sync_read(r, normalize=False, num_retry=1)
              for r in ("Operating_Mode", "Status", "Torque_Enable", "Present_Temperature")}
    if any(health["Operating_Mode"].values()) or servo_status_faults(health["Status"], allow_gripper_overload):
        raise RuntimeError("Follower must report position mode and no non-exempt servo errors")
    if any(t not in (0, 1) for t in health["Torque_Enable"].values()):
        raise RuntimeError("Unknown torque state")
    if allow_gripper_overload:
        log_gripper_overload(health["Status"], status_logger)
    health["Present_Temperature"] = confirm_temperature(
        bus, health["Present_Temperature"], temperature_confirmation_s, stop=stop,
        allow_gripper_overload=allow_gripper_overload, status_logger=status_logger)
    raw = bus.sync_read("Present_Position", normalize=False, num_retry=1)
    violations=[f'{n}: raw={raw[n]}, allowed=[{calibration[n]["range_min"]}, {calibration[n]["range_max"]}]'
                for n in NAMES if not calibration[n]["range_min"] <= raw[n] <= calibration[n]["range_max"]]
    if violations:
        raise RuntimeError("软件预检：当前位置超出本地标定区间（舵机限制寄存器已核对一致，不是舵机主动越界报警）。"
                           + '; '.join(violations) + '；未自动放宽限位或修改标定。')
    return health, raw


def load_restore_path(trial_directory, calibration):
    source = json.loads((trial_directory/"report.json").read_text(encoding="utf-8"))
    if source.get("calibration") != calibration or source.get("usb_serial") != EXPECTED_FOLLOWER_SERIAL:
        raise RuntimeError("Previous trial coordinates or robot identity do not match")
    with (trial_directory/"act_trial.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ValueError("Previous trial has no measured executed trajectory")
    path = np.array([[float(row[f"state_{n}"]) for n in NAMES] for row in rows][::-1], dtype=float)
    initial = np.array([source["initial_measured"][n] for n in NAMES], dtype=float)
    path = np.vstack([path, initial])
    limits = limits_from_calibration(calibration)
    if not np.isfinite(path).all() or np.any(path < limits[:, 0]-1e-5) or np.any(path > limits[:, 1]+1e-5):
        raise RuntimeError("Previous measured reverse path exceeds current bounds")
    return path


def restore_start(motion, path, output):
    """Explicitly requested slow reverse of a previously executed measured path."""
    measured = motion.bus.sync_read("Present_Position", num_retry=1)
    current = np.array([measured[n] for n in NAMES])
    if np.any(abs(current-path[0]) > np.array([5.0]*5+[20.0])):
        raise RuntimeError("Current pose moved away from the previous path endpoint; restore not started")
    points = np.vstack([current, path])
    durations = np.maximum(np.ceil(np.max(abs(np.diff(points, axis=0))/ALIGN_SPEED, axis=1)/PERIOD), 1).astype(int)
    planned = float(durations.sum()*PERIOD)
    print(f"ACT_RESTORE_START measured reverse path, body=2deg/s, estimated={planned:.1f}s; Esc stops", flush=True)
    started = motion.clock.perf_counter()
    last_notice = started
    frame = 0
    with output.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["step", "elapsed_s", "segment", *[f"goal_{n}" for n in NAMES], *[f"state_{n}" for n in NAMES]])
        for segment, count in enumerate(durations):
            for index in range(count):
                target = points[segment]+(points[segment+1]-points[segment])*(index+1)/count
                tick = motion.clock.perf_counter()
                actual = motion.bus.sync_read("Present_Position", num_retry=1)
                motion.send(target, source_time=tick)
                elapsed = motion.clock.perf_counter()-started
                writer.writerow([frame, elapsed, segment, *target, *[actual[n] for n in NAMES]])
                frame += 1
                if motion.clock.perf_counter()-last_notice >= 10:
                    print(f"ACT_RESTORE {elapsed:.1f}/{planned:.1f}s", flush=True)
                    last_notice = motion.clock.perf_counter()
        stable = 0
        verify_deadline = motion.clock.perf_counter()+15
        while stable < 3:
            tick = motion.clock.perf_counter()
            measured = motion.bus.sync_read("Present_Position", num_retry=1)
            errors = abs(np.array([measured[n] for n in NAMES])-path[-1])
            stable = stable+1 if np.all(errors <= ALIGN_TOLERANCE) else 0
            motion.send(path[-1], source_time=tick)
            if motion.clock.perf_counter() > verify_deadline:
                raise TimeoutError("Explicit start restoration did not settle")
    elapsed = motion.clock.perf_counter()-started
    print("ACT_RESTORE_COMPLETE " + json.dumps(dict(zip(NAMES, errors.tolist()))), flush=True)
    return {"method": "Slow reverse of prior measured executed trajectory, followed by its pre-alignment start",
            "planned_s": planned, "elapsed_s": elapsed, "sent_frames": frame,
            "target_start": dict(zip(NAMES, path[-1].tolist())), "final_error": dict(zip(NAMES, errors.tolist()))}


class Motion:
    def __init__(self, bus, calibration, stop, clock=time, soft_interlocks=True,
                 temperature_confirmation_s=0, allow_gripper_overload=False):
        self.bus, self.calibration, self.stop, self.clock = bus, calibration, stop, clock
        self.last_send = None
        self.last_health = None
        self.goal_writes = 0
        self.rounding_adjustments = 0
        self.enabled = False
        self.last_target = None
        self.health_records = []
        self.soft_interlocks = soft_interlocks
        self.temperature_confirmation_s = temperature_confirmation_s
        self.allow_gripper_overload = allow_gripper_overload
        self.warning_records = []
        self.last_diagnostics = {}

    def warn(self, kind, **values):
        self.warning_records.append({"perf_time": self.clock.perf_counter(), "kind": kind, **values})
        if sum(r["kind"] == kind for r in self.warning_records) <= 3:
            print("ACT_DIAGNOSTIC_ONLY " + json.dumps({"kind": kind, **values}), flush=True)

    def anchor_current(self):
        raw = self.bus.sync_read("Present_Position", normalize=False, num_retry=1)
        if any(not self.calibration[n]["range_min"] <= raw[n] <= self.calibration[n]["range_max"] for n in NAMES):
            raise RuntimeError("Cannot hold outside existing encoder bounds")
        if self.stop.is_set():
            raise KeyboardInterrupt
        self.bus.sync_write("Goal_Position", raw, normalize=False)
        self.goal_writes += 1
        self.last_target = np.array([
            (raw[n]-self.calibration[n]["range_min"])*100/(self.calibration[n]["range_max"]-self.calibration[n]["range_min"])
            if n == "gripper" else (raw[n]-(self.calibration[n]["range_min"]+self.calibration[n]["range_max"])/2)*360/4095
            for n in NAMES])
        self.last_send = self.clock.perf_counter()

    def wait_until(self, deadline):
        while True:
            if self.stop.is_set():
                raise KeyboardInterrupt
            remaining = deadline-self.clock.perf_counter()
            if remaining <= 0:
                return
            self.clock.sleep(min(remaining, 0.01))

    def start_hold(self):
        if self.stop.is_set():
            raise KeyboardInterrupt
        raw = self.bus.sync_read("Present_Position", normalize=False, num_retry=1)
        if any(not self.calibration[n]["range_min"] <= raw[n] <= self.calibration[n]["range_max"] for n in NAMES):
            raise RuntimeError("Cannot enable torque outside current bounds")
        # Never enable torque against the stale previous goal.
        self.bus.sync_write("Goal_Position", raw, normalize=False)
        self.goal_writes += 1
        self.enabled = True
        self.bus.sync_write("Torque_Enable", {n: 1 for n in NAMES}, normalize=False)
        if any(t != 1 for t in self.bus.sync_read("Torque_Enable", normalize=False, num_retry=1).values()):
            raise RuntimeError("Could not enable all follower motors")
        measured = self.bus.sync_read("Present_Position", num_retry=1)
        self.last_target = np.array([measured[n] for n in NAMES])
        self.last_send = self.clock.perf_counter()

    def send(self, target, source_time=None, trial_deadline=None, jump_check=False):
        raw, adjustments = make_raw_goal(self.bus, target, self.calibration)
        delta = np.asarray(target)-self.last_target
        if jump_check and np.any(abs(delta) > MAX_COMMAND_JUMP):
            if self.soft_interlocks:
                raise RuntimeError("Abrupt ACT command change; goal not sent: " + json.dumps(dict(zip(NAMES, delta.tolist()))))
            self.warn("command_jump", delta=dict(zip(NAMES, delta.tolist())))
        if self.last_send is not None:
            self.wait_until(self.last_send+PERIOD)
        now = self.clock.perf_counter()
        if trial_deadline is not None and now >= trial_deadline:
            return None
        if self.last_health is None or now-self.last_health >= 0.5:
            status = self.bus.sync_read("Status", normalize=False, num_retry=1)
            temperature = self.bus.sync_read("Present_Temperature", normalize=False, num_retry=1)
            torque = self.bus.sync_read("Torque_Enable", normalize=False, num_retry=1)
            self.health_records.append({"perf_time": now, "status": status, "temperature": temperature, "torque": torque})
            if servo_status_faults(status, self.allow_gripper_overload) or any(t != 1 for t in torque.values()):
                raise RuntimeError("Servo health/torque check failed; next goal not sent")
            if self.allow_gripper_overload:
                log_gripper_overload(status, self.warn)
            if any(t >= TEMPERATURE_LIMIT_C for t in temperature.values()):
                if self.temperature_confirmation_s > 0:
                    self.anchor_current()
                    self.warn("temperature_pause", temperature=temperature)
                confirm_temperature(self.bus, temperature, self.temperature_confirmation_s,
                                    stop=self.stop, clock=self.clock, records=self.health_records,
                                    require_torque=True, deadline=trial_deadline,
                                    allow_gripper_overload=self.allow_gripper_overload, status_logger=self.warn)
                self.last_health = self.clock.perf_counter()
                if source_time is not None:
                    raise TemperatureRecovered
            self.last_health = self.clock.perf_counter()
        if self.stop.is_set():
            raise KeyboardInterrupt
        now = self.clock.perf_counter()
        if trial_deadline is not None and now >= trial_deadline:
            return None
        source_age_ms = (now-source_time)*1000 if source_time is not None else None
        if source_age_ms is not None and source_age_ms > 150:
            if self.soft_interlocks:
                raise TimeoutError("Result is over 150 ms old; next goal not sent")
            self.warn("result_age", source_age_ms=source_age_ms)
        self.bus.sync_write("Goal_Position", raw, normalize=False)
        self.goal_writes += 1
        self.rounding_adjustments += adjustments
        self.last_send = self.clock.perf_counter()
        self.last_target = np.asarray(target).copy()
        self.last_diagnostics = {"command_delta": delta.tolist(), "source_age_at_send_ms": source_age_ms}
        return self.last_send

    def hold_on_exit(self):
        if not self.enabled:
            return None
        raw = self.bus.sync_read("Present_Position", normalize=False, num_retry=1)
        if any(not self.calibration[n]["range_min"] <= raw[n] <= self.calibration[n]["range_max"] for n in NAMES):
            raise RuntimeError("Exit feedback outside current bounds; last goal retained")
        self.bus.sync_write("Goal_Position", raw, normalize=False)
        self.goal_writes += 1
        return {"position_raw": raw, "torque": self.bus.sync_read("Torque_Enable", normalize=False, num_retry=1),
                "status": self.bus.sync_read("Status", normalize=False, num_retry=1),
                "temperature": self.bus.sync_read("Present_Temperature", normalize=False, num_retry=1)}


def load_reference_start(path, calibration):
    reference = json.loads(Path(path).read_text(encoding="utf-8"))
    if reference.get("joint_order") != list(NAMES) or reference.get("calibration_reference") != calibration:
        raise RuntimeError("Saved start pose joint order/calibration differs; no reset allowed")
    if reference.get("units") != ["degrees"]*5+["gripper_percent"]:
        raise RuntimeError("Saved start pose units differ")
    target = np.array([reference["normalized_position"][n] for n in NAMES], dtype=float)
    limits = limits_from_calibration(calibration)
    if target.shape != (6,) or not np.isfinite(target).all() or np.any(target < limits[:, 0]) or np.any(target > limits[:, 1]):
        raise RuntimeError("Saved start pose is nonfinite or outside unchanged bounds")
    return target


def reset_to_reference(motion, target, output, record, observe=None, emit=None, speeds=None):
    """Operator-checked, monotone weighted reset. Not a collision planner.

    Only valid after explicit confirmation of an empty gripper and clear entire
    reset route. Does not infer a generic shoulder lift or reverse a trajectory.
    """
    from so101_weighted_reset import SPEEDS, weighted_targets
    speeds = np.asarray(SPEEDS if speeds is None else speeds, dtype=float)
    if speeds.shape != (6,) or not np.isfinite(speeds).all() or np.any(speeds <= 0) or np.any(speeds > np.array([20.]*5+[100.])):
        raise ValueError("Reset speeds require five positive rates <=20 deg/s and gripper rate <=100 percent/s; these bounds are not collision guarantees")
    measured = motion.bus.sync_read("Present_Position", num_retry=1)
    current = np.array([measured[n] for n in NAMES])
    target = np.asarray(target, dtype=float)
    limits = limits_from_calibration(motion.calibration)
    if not np.isfinite(current).all() or np.any(current < limits[:, 0]) or np.any(current > limits[:, 1]):
        raise RuntimeError("Current reset pose is outside unchanged bounds")
    if target.shape != (6,) or not np.isfinite(target).all() or np.any(target < limits[:, 0]) or np.any(target > limits[:, 1]):
        raise RuntimeError("Reference reset target is outside unchanged bounds")
    record.update(outcome="moving", initial_measured=dict(zip(NAMES, current.tolist())),
                  target=dict(zip(NAMES, target.tolist())), speeds=dict(zip(NAMES, speeds.tolist())),
                  body_tolerance_deg=float(ALIGN_TOLERANCE[0]), gripper_tolerance_percent=float(ALIGN_TOLERANCE[-1]),
                  planned_duration_s=float(np.max(abs(target-current)/speeds)),
                  reverse_replay=False, collision_route_operator_confirmed=True,
                  empty_gripper_operator_confirmed=True, collision_planning=False)
    print(f"ACT_REFERENCE_RESET_START weighted reset to saved start; estimated={record['planned_duration_s']:.1f}s; Esc stops", flush=True)
    if emit:
        emit("stage", stage="resetting", message="沿已确认的路径低速复位到保存的标准起点；此时尚未开始 ACT 计时。")
    started = motion.clock.perf_counter()
    last_notice = started
    frame_index = 0
    with Path(output).open("x", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["phase", "step", "elapsed_s", *[f"goal_{n}" for n in NAMES], *[f"state_{n}" for n in NAMES]])
        for goal in weighted_targets(current, target, speeds=speeds):
            tick = motion.clock.perf_counter()
            actual = motion.bus.sync_read("Present_Position", num_retry=1)
            motion.send(goal, source_time=tick)
            writer.writerow(["reset", frame_index, motion.clock.perf_counter()-started, *goal, *[actual[n] for n in NAMES]])
            frame_index += 1
            if observe is not None:
                _, state, frames, _, _ = observe()
                if emit:
                    emit("preview", frames=frames)
                    emit("telemetry", state=state.tolist(), reset_elapsed_s=motion.clock.perf_counter()-started,
                         health=motion.health_records[-1] if motion.health_records else None)
            if motion.clock.perf_counter()-last_notice >= 5:
                print(f"ACT_REFERENCE_RESET {motion.clock.perf_counter()-started:.1f}/{record['planned_duration_s']:.1f}s", flush=True)
                last_notice = motion.clock.perf_counter()
        record["outcome"] = "verifying"
        stable = 0
        verify_deadline = motion.clock.perf_counter()+15
        while stable < 3:
            tick = motion.clock.perf_counter()
            actual = motion.bus.sync_read("Present_Position", num_retry=1)
            errors = abs(np.array([actual[n] for n in NAMES])-target)
            record["last_verify_errors"] = dict(zip(NAMES, errors.tolist()))
            stable = stable+1 if np.all(errors <= ALIGN_TOLERANCE) else 0
            motion.send(target, source_time=tick)
            writer.writerow(["verify", frame_index, motion.clock.perf_counter()-started, *target, *[actual[n] for n in NAMES]])
            frame_index += 1
            if observe is not None:
                _, state, frames, _, _ = observe()
                if emit:
                    emit("preview", frames=frames)
                    emit("telemetry", state=state.tolist(), reset_elapsed_s=motion.clock.perf_counter()-started,
                         health=motion.health_records[-1] if motion.health_records else None)
            if motion.clock.perf_counter() > verify_deadline:
                raise TimeoutError("Saved reference reset did not settle; ACT not started")
    record.update(outcome="reached_reference", elapsed_s=motion.clock.perf_counter()-started,
                  sent_frames=frame_index, final_measured=actual)
    print("ACT_REFERENCE_RESET_COMPLETE " + json.dumps(record["last_verify_errors"]), flush=True)
    return record


def resolve_follower_port(requested, ports):
    """USB identity, not the last COM number, determines the physical follower."""
    matches = [p.device for p in ports if p.serial_number == EXPECTED_FOLLOWER_SERIAL]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one follower USB {EXPECTED_FOLLOWER_SERIAL}; found {matches}")
    if requested != "auto" and requested.upper() != matches[0].upper():
        raise RuntimeError("Requested COM port is not the verified follower; no hardware writes")
    return matches[0]


def wait_for_operator(controller, motion, observe, calibration, emit):
    """Ready gate. No writes unless the operator explicitly requests current-pose hold.

    Start is accepted only with every motor already reporting torque enabled.
    This lets an operator restore holding torque, remove a support, then start.
    """
    emit("stage", stage="ready", message="预热完成。核对相机和场景后点击开始；不会自动运动。")
    def checked_preflight():
        return preflight(motion.bus, calibration, motion.temperature_confirmation_s, motion.stop,
                         motion.allow_gripper_overload, motion.warn)
    last_health = -float("inf")
    health = None
    while True:
        if controller.stop.is_set():
            raise KeyboardInterrupt
        _, state, frames, stamps, _ = observe()
        now = motion.clock.perf_counter()
        if now-last_health >= 0.5:
            health, _ = checked_preflight()
            last_health = now
        emit("preview", frames=frames)
        emit("telemetry", state=state.tolist(), health=health,
             camera_age_ms={n: (now-t)*1000 for n, t in stamps.items()})
        if controller.hold.is_set():
            controller.hold.clear()
            motion.start_hold()
            health, _ = checked_preflight()
            emit("stage", stage="ready", message="已保持当前位置。请移开手和支撑，重新确认后点击开始。")
            last_health = -float("inf")
        if controller.start.is_set():
            controller.start.clear()
            # Revalidate here rather than accepting a cached GUI display.
            health, _ = checked_preflight()
            if all(t == 1 for t in health["Torque_Enable"].values()):
                return
            emit("stage", stage="ready", message="尚未全部开启力矩。先支撑机械臂并点击当前位置保持，再移开支撑。")
        if controller.stop.wait(0.1):
            raise KeyboardInterrupt


def main(argv=None, controller=None, resources=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--inspect-live", action="store_true", help="Read live inputs and first target, but send no motor register writes")
    parser.add_argument("--standalone-cameras", action="store_true",
                        help="Open raw physical cameras directly when the optional alignment-preview service is not running")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--duration", type=float, default=MAX_TRIAL_DURATION)
    parser.add_argument("--control-mode", choices=("guarded", "standard"), default="guarded",
                        help="standard: reference reset then direct ACT; soft timing/jump thresholds only log")
    parser.add_argument("--gripper-overload-policy", choices=("stop", "log_gripper_only"), default="stop",
                        help="log_gripper_only: isolated gripper Status=32 logs without stopping; no servo parameter writes")
    parser.add_argument("--port", default="auto", help="Resolve the follower by its USB serial, or verify an explicit COM port")
    parser.add_argument("--top-camera", type=int, default=0)
    parser.add_argument("--wrist-camera", type=int, default=1)
    parser.add_argument("--checkpoint", type=Path, default=shadow.CHECKPOINT)
    parser.add_argument("--calibration", type=Path, default=shadow.CALIBRATION)
    parser.add_argument("--reset-before-act", action="store_true", help="Reset to the saved task start before model target alignment")
    parser.add_argument("--reference-start-pose", type=Path, default=REFERENCE_START)
    parser.add_argument("--reset-speeds", type=float, nargs=6,
                        help="Optional five arm target rates (deg/s), then gripper percent/s; unchanged defaults if omitted")
    parser.add_argument("--clear-reset-route-confirmed", action="store_true", help="Operator confirms the entire current-to-start route is clear")
    parser.add_argument("--empty-gripper-confirmed", action="store_true", help="Operator confirms an empty, unobstructed gripper")
    parser.add_argument("--reset-only", action="store_true", help="Execute only the approved saved-start reset; never load or execute ACT")
    parser.add_argument("--restore-start-from-trial", type=Path,
                        help="DISABLED: reverse restoration can collide with the reset object and other obstacles")
    args = parser.parse_args(argv)
    if args.reset_speeds is not None and (not np.isfinite(args.reset_speeds).all() or any(v <= 0 for v in args.reset_speeds) or np.any(np.asarray(args.reset_speeds)>np.array([20.]*5+[100.]))):
        raise ValueError("Reset speeds must be six finite positive values; no hardware accessed")
    def emit(kind, **values):
        if controller is not None:
            controller.emit(kind, **values)
    if args.restore_start_from_trial is not None:
        raise ValueError("Reverse-path restoration is disabled: the user reports obstacles on that path. No hardware accessed. Restore using a separately confirmed collision-free route.")
    if args.execute and args.inspect_live:
        raise ValueError("Select either --execute or --inspect-live")
    if args.reset_only and not args.reset_before_act:
        raise ValueError("Reset-only requires --reset-before-act")
    if args.inspect_live and args.reset_before_act:
        raise ValueError("Read-only inspection cannot request a physical reset")
    if args.execute and args.reset_before_act and not (args.clear_reset_route_confirmed and args.empty_gripper_confirmed):
        raise ValueError("Reference reset requires explicit clear-route and empty-gripper confirmations")
    if args.execute and args.control_mode == "standard" and not args.reset_before_act:
        raise ValueError("Current standard-mode contract requires the confirmed saved-start reset")
    if not 0 < args.duration <= MAX_TRIAL_DURATION:
        raise ValueError(f"Current trials must be bounded to the demonstration budget of {MAX_TRIAL_DURATION:g} seconds")
    if args.top_camera < 0 or args.wrist_camera < 0 or args.top_camera == args.wrist_camera:
        raise ValueError("Two distinct nonnegative camera indices are required")
    if args.inspect_live and args.restore_start_from_trial is not None:
        raise ValueError("Live inspection cannot request physical start restoration")
    plan = {"checkpoint": str(args.checkpoint), "follower_port": args.port, "usb_serial": EXPECTED_FOLLOWER_SERIAL,
            "chunk_size": 100, "n_action_steps": 1, "temporal_ensemble_coeff": 0.01, "fps": 30,
            "trial_duration_s": args.duration, "maximum_trial_duration_s": MAX_TRIAL_DURATION,
            "trial_duration_basis": TRIAL_DURATION_BASIS,
            "time_budget_excludes_reference_reset": True, "time_budget_excludes_initial_alignment": True,
            "clip_to_existing_bounds": True, "boundary_changes": False,
            "fixed_home_return": False, "initial_predicted_goal_alignment_deg_s": 2,
            "camera_access_mode": "direct without optional preview service" if args.standalone_cameras else "pause optional preview service, then direct capture",
            "explicit_start_restore_from_trial": str(args.restore_start_from_trial) if args.restore_start_from_trial else None,
            "abrupt_command_stop_threshold": dict(zip(NAMES, MAX_COMMAND_JUMP.tolist())),
            "exit_releases_torque": False, "stop_key": "Esc", "software_stop_is_not_hardware_emergency_stop": True}
    plan["operator_start_gate"] = controller is not None
    plan["camera_indices"] = {"top": args.top_camera, "wrist": args.wrist_camera}
    plan["reset_before_act"] = args.reset_before_act
    plan["reference_start_pose"] = str(args.reference_start_pose) if args.reset_before_act else None
    plan["reset_only"] = args.reset_only
    plan["configured_reset_speeds"] = args.reset_speeds
    plan["control_mode"] = args.control_mode
    plan["gripper_overload_policy"] = args.gripper_overload_policy
    plan["gripper_status_32_is_not_task_success"] = True
    plan["other_servo_alarms_remain_immediate_stops"] = True
    plan["servo_protection_parameters_changed"] = False
    plan["soft_threshold_behavior"] = "log_only" if args.control_mode == "standard" else "stop"
    plan["initial_model_goal_alignment"] = args.control_mode == "guarded"
    plan["temperature_limit_c"] = TEMPERATURE_LIMIT_C
    plan["temperature_confirmation_s"] = TEMPERATURE_CONFIRMATION_S if args.control_mode == "standard" else 0
    if args.control_mode == "standard":
        plan["initial_predicted_goal_alignment_deg_s"] = None
        plan["abrupt_command_stop_threshold"] = None
        plan["abrupt_command_warning_threshold"] = dict(zip(NAMES, MAX_COMMAND_JUMP.tolist()))
    print("REAL_ACT_PLAN " + json.dumps(plan, ensure_ascii=False), flush=True)
    if not args.execute and not args.inspect_live:
        return
    if args.output is None or args.output.resolve().drive.upper() != "E:" or args.output.exists():
        raise ValueError("Execution requires a new output directory on E drive")
    port = resolve_follower_port(args.port, shadow.list_ports.comports())
    plan["follower_port"] = port
    args.output.mkdir(parents=True)
    report = dict(plan, outcome="preflight_failed", model_steps=0,
                  task_success=None, task_result_source="pending_operator_report",
                  automatic_video_review=False)
    calibration = json.loads(args.calibration.read_text(encoding="utf-8"))
    reference_target = load_reference_start(args.reference_start_pose, calibration) if args.reset_before_act else None
    limits = limits_from_calibration(calibration)
    bus = shadow.FeetechMotorsBus(port, {
        n: shadow.Motor(calibration[n]["id"], "sts3215",
                        shadow.MotorNormMode.RANGE_0_100 if n == "gripper" else shadow.MotorNormMode.DEGREES)
        for n in NAMES
    }, calibration={n: shadow.MotorCalibration(**calibration[n]) for n in NAMES})
    guard_bus_writes(bus)
    stop = controller.stop if controller is not None else threading.Event()
    motion = Motion(bus, calibration, stop, soft_interlocks=args.control_mode == "guarded",
                    temperature_confirmation_s=plan["temperature_confirmation_s"],
                    allow_gripper_overload=args.gripper_overload_policy == "log_gripper_only")
    def checked_preflight():
        return preflight(bus, calibration, motion.temperature_confirmation_s, stop,
                         motion.allow_gripper_overload, motion.warn)
    def soft_issue(kind, message, **values):
        if motion.soft_interlocks:
            raise TimeoutError(message)
        motion.warn(kind, **values)
    cameras, videos, listener = {}, {}, None
    previous_preview = None
    resource_lease=False
    try:
        if resources is not None:
            if not args.standalone_cameras or not args.execute or args.reset_only:
                raise ValueError('常驻资源仅用于独立相机的 ACT 执行会话')
            resources.begin({'checkpoint':resources.checkpoint_signature(args.checkpoint),
                'calibration':calibration,'cameras':(args.top_camera,args.wrist_camera,640,480,30,'MJPG'),
                'inference':(100,1,.01),'control_mode':args.control_mode})
            resource_lease=True
        from pynput import keyboard
        listener = keyboard.Listener(on_press=lambda key: stop.set() if key == keyboard.Key.esc else None)
        listener.start()
        emit("stage", stage="preflight", message=f"快速核对从臂 {port} 的身份、标定和当前健康状态；复用已加载模型，不重新预热。" if resources is not None and resources.warmed else f"核对从臂 {port} 的标定、边界与舵机状态；不写寄存器。")
        bus.connect(handshake=False)
        report["preflight"], _ = checked_preflight()
        report["calibration"] = calibration
        if args.reset_only:
            motion.start_hold()
            report["reference_start_reset"] = {}
            reset_to_reference(motion, reference_target, args.output/"reference_reset.csv",
                               report["reference_start_reset"], emit=emit, speeds=args.reset_speeds)
            report["outcome"] = "completed_reference_reset_only"
            return report
        torch.set_num_threads(1)
        emit("stage", stage="model_loading", message="复用已加载的 ACT 和处理器；不重新下载或加载。" if resources is not None and resources.model is not None else "从 E 盘加载本地 ACT 权重和原始预处理器。")
        def load_model():
            cfg = shadow.ACTConfig.from_pretrained(str(args.checkpoint), local_files_only=True)
            if cfg.chunk_size != 100 or not torch.cuda.is_available():
                raise RuntimeError("Expected the trained 100-step ACT and CUDA")
            cfg.n_action_steps, cfg.temporal_ensemble_coeff, cfg.pretrained_backbone_weights = 1, 0.01, None
            policy = shadow.ACTPolicy.from_pretrained(str(args.checkpoint), config=cfg,
                                                     local_files_only=True, strict=True)
            policy.eval()
            pre, post = shadow.make_pre_post_processors(cfg, pretrained_path=str(args.checkpoint))
            return cfg,policy,pre,post
        if resources is not None:
            (cfg,policy,pre,post),report['model_reused']=resources.get_model(load_model)
        else:
            cfg,policy,pre,post=load_model()
            report['model_reused']=False
        policy.reset();pre.reset();post.reset()
        if not args.standalone_cameras:
            previous_preview = shadow.preview_status()
            shadow.preview_select(None)
            release_deadline = time.perf_counter()+5
            while time.perf_counter() < release_deadline:
                if all(c["phase"] == "paused" for c in shadow.preview_status()["cameras"].values()):
                    break
                if stop.wait(0.1):
                    raise KeyboardInterrupt
            else:
                raise RuntimeError("Alignment viewer did not release the cameras")
        emit("stage", stage="cameras", message="复用常驻顶部与腕部相机，重新读取当前画面。" if resources is not None and len(resources.cameras)==2 else "打开顶部和腕部相机。请关闭会议、相机预览和其他控制程序。")
        for name, index in (("top", args.top_camera), ("wrist", args.wrist_camera)):
            def new_camera(index=index):
                return shadow.OpenCVCamera(shadow.OpenCVCameraConfig(index_or_path=index, backend=700,
                                            width=640, height=480, fps=30, fourcc="MJPG", warmup_s=1))
            if resources is not None:
                camera,reused=resources.get_camera(name,new_camera)
                report.setdefault('cameras_reused',{})[name]=reused
            else:
                camera=new_camera()
                cameras[name]=camera
                camera.connect()
            cameras[name] = camera
            if not getattr(controller,'policy_video_recording',False):
                video = cv2.VideoWriter(str(args.output/f"act_{name}.avi"), cv2.VideoWriter_fourcc(*"MJPG"), 30, (640, 480))
                if not video.isOpened():
                    raise RuntimeError(f"Could not create {name} trial video")
                videos[name] = video

        def observation():
            if stop.is_set():
                raise KeyboardInterrupt
            begin = time.perf_counter()
            measured = bus.sync_read("Present_Position", num_retry=1)
            state = np.array([measured[n] for n in NAMES], dtype=np.float32)
            feedback_ms = (time.perf_counter()-begin)*1000
            if feedback_ms > 150:
                soft_issue("position_feedback_age", "Position feedback exceeded 150 ms", feedback_ms=feedback_ms)
            if not np.isfinite(state).all():
                raise FloatingPointError("Nonfinite follower measurement")
            frames, stamps = {}, {}
            for name, camera in cameras.items():
                if camera.thread is None or not camera.thread.is_alive():
                    raise RuntimeError(f"{name} capture stopped")
                if args.control_mode == "standard":
                    # Use the installed official camera's frame-availability
                    # check, rather than replaying a cached frame indefinitely.
                    camera.async_read()
                with camera.frame_lock:
                    if camera.latest_frame is None or camera.latest_timestamp is None:
                        raise RuntimeError(f"{name} frame missing")
                    frames[name] = camera.latest_frame.copy()
                    stamps[name] = camera.latest_timestamp
                if frames[name].shape != (480, 640, 3):
                    raise RuntimeError(f"{name} image shape differs")
                age_ms = (time.perf_counter()-stamps[name])*1000
                if age_ms > 200:
                    soft_issue("camera_input_age", f"{name} image over 200 ms old", camera=name, age_ms=age_ms)
            batch = {"observation.state": torch.from_numpy(state)}
            for name in cameras:
                batch[f"observation.images.{name}"] = torch.from_numpy(frames[name]).permute(2, 0, 1).float().div_(255)
            return batch, state, frames, stamps, begin

        with torch.inference_mode():
            if args.restore_start_from_trial is not None:
                restore_path = load_restore_path(args.restore_start_from_trial, calibration)
                checked_preflight()
                # Restore is only enabled by this explicit command-line option.
                # It does not establish a mandatory home for ordinary ACT use.
                current = bus.sync_read("Present_Position", num_retry=1)
                if np.any(abs(np.array([current[n] for n in NAMES])-restore_path[0]) > np.array([5.0]*5+[20.0])):
                    raise RuntimeError("Current pose does not match the previous endpoint; nothing restored")
                motion.start_hold()
                report["explicit_start_restoration"] = restore_start(motion, restore_path, args.output/"start_restoration.csv")
            batch, state, frames, stamps, _ = observation()
            emit("preview", frames=frames)
            report['warmup_iterations']=0
            if resources is None or not resources.warmed:
                emit("stage", stage="warmup", message="首次进行 20 次 GPU 预热；不执行模型动作。")
                processed = pre(batch)
                for _ in range(20):
                    if stop.is_set():
                        raise KeyboardInterrupt
                    policy.select_action(processed)
                    report['warmup_iterations']+=1
                torch.cuda.synchronize()
                if resources is not None:resources.warmed=True
            else:
                emit('stage',stage='warmup',message='复用本会话已预热模型与相机，不重复预热；已清空上次动作队列。')
            policy.reset()
            pre.reset()
            post.reset()
            if controller is not None and args.execute:
                wait_for_operator(controller, motion, observation, calibration, emit)
                # Waiting/preview inputs are never played back as policy actions.
                policy.reset()
                pre.reset()
                post.reset()
            if args.reset_before_act:
                checked_preflight()
                motion.start_hold()
                report["reference_start_reset"] = {}
                reset_to_reference(motion, reference_target, args.output/"reference_reset.csv",
                                   report["reference_start_reset"], observe=observation if controller is not None else None,
                                   emit=emit, speeds=args.reset_speeds)
                # No warmup or pre-reset action queue may survive the reset.
                policy.reset()
                pre.reset()
                post.reset()
            batch, state, frames, stamps, _ = observation()
            if args.control_mode == "guarded" or args.inspect_live:
                first_candidate = post(policy.select_action(pre(batch))).detach().cpu().numpy().reshape(6)
                first_goal, first_clip = bounded_target(first_candidate, limits)
                report["first_candidate"] = dict(zip(NAMES, first_candidate.tolist()))
                report["first_bounded_goal"] = dict(zip(NAMES, first_goal.tolist()))
                report["first_clipped_joints"] = [n for n, clipped in zip(NAMES, first_clip) if clipped]
                if np.any(abs(first_goal-state) > 30):
                    soft_issue("first_target_difference", "First ACT target is over 30 degrees/gripper points from the current pose",
                               difference=(first_goal-state).tolist())
            report["initial_measured"] = dict(zip(NAMES, state.tolist()))
            for name, frame in frames.items():
                cv2.imwrite(str(args.output/f"initial_{name}.png"), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
            if args.inspect_live:
                report["outcome"] = "inspected_live_no_motion"
                print("ACT_LIVE_INSPECTION " + json.dumps({k: report[k] for k in
                      ("first_candidate", "first_bounded_goal", "first_clipped_joints", "initial_measured")}), flush=True)
                return
            checked_preflight()
            motion.start_hold()
            if args.control_mode == "guarded":
                measured_start = bus.sync_read("Present_Position", num_retry=1)
                alignment_start = np.array([measured_start[n] for n in NAMES])
                align_duration = max(float(np.max(abs(first_goal-alignment_start)/ALIGN_SPEED)), 1.0)
                align_frames = math.ceil(align_duration/PERIOD)
                emit("stage", stage="aligning", message="低速接入模型首个目标；与标准起点复位是两个阶段。停止按钮和 Esc 可停止。")
                print(f"ACT_INITIAL_ALIGN_START {align_duration:.2f}s, 2deg/s; first clip={report['first_clipped_joints']}; Esc stops", flush=True)
                with (args.output/"initial_alignment.csv").open("x", newline="", encoding="utf-8") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(["step", *[f"goal_{n}" for n in NAMES], *[f"state_{n}" for n in NAMES]])
                    for step in range(align_frames):
                        goal = alignment_start+(first_goal-alignment_start)*(step+1)/align_frames
                        measured = bus.sync_read("Present_Position", num_retry=1)
                        motion.send(goal)
                        if controller is not None:
                            _, state, frames, stamps, _ = observation()
                            emit("preview", frames=frames)
                            emit("telemetry", state=state.tolist(), health=motion.health_records[-1] if motion.health_records else None,
                                 alignment_step=step+1, alignment_steps=align_frames)
                        writer.writerow([step, *goal, *[measured[n] for n in NAMES]])
                    stable = 0
                    align_deadline = time.perf_counter()+15
                    while stable < 3:
                        measured = bus.sync_read("Present_Position", num_retry=1)
                        errors = abs(np.array([measured[n] for n in NAMES])-first_goal)
                        stable = stable+1 if np.all(errors <= ALIGN_TOLERANCE) else 0
                        motion.send(first_goal)
                        if controller is not None:
                            _, state, frames, stamps, _ = observation()
                            emit("preview", frames=frames)
                        if time.perf_counter() > align_deadline:
                            raise TimeoutError("Initial alignment did not settle; ACT trial not started")
                    report["initial_alignment_errors"] = dict(zip(NAMES, errors.tolist()))
            else:
                report["initial_alignment_skipped"] = "Direct ACT after confirmed reference reset"
            policy.reset()
            pre.reset()
            post.reset()
            prepare_capture=getattr(controller,'prepare_policy_capture',None)
            if callable(prepare_capture):prepare_capture(bus)
            emit("stage", stage="running", message=f"ACT 推理中，最长 {args.duration:g} 秒。完成计时不等于任务成功。")
            print(f"ACT_LIVE_START duration={args.duration:g}s; original real-image weights; 100pred/1exec+TE; ESC STOPS", flush=True)
            start = time.perf_counter()
            deadline = start+args.duration
            clipped_counts = np.zeros(6, dtype=int)
            overrun_count = 0
            previous_send = None
            next_tick = start
            with (args.output/"act_trial.csv").open("x", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["step", "elapsed_s", "processing_ms", "top_age_at_send_ms", "wrist_age_at_send_ms",
                                 "send_interval_ms", *[f"state_{n}" for n in NAMES],
                                 *[f"candidate_{n}" for n in NAMES], *[f"sent_goal_{n}" for n in NAMES],
                                 *[f"clipped_{n}" for n in NAMES], "source_age_at_send_ms",
                                 *[f"command_delta_{n}" for n in NAMES]])
                while time.perf_counter() < deadline:
                    motion.wait_until(next_tick)
                    if time.perf_counter() >= deadline:
                        break
                    batch, state, frames, stamps, tick = observation()
                    candidate = post(policy.select_action(pre(batch))).detach().cpu().numpy().reshape(6)
                    torch.cuda.synchronize()
                    target, clipped = bounded_target(candidate, limits)
                    if report["model_steps"] == 0 and args.control_mode == "standard":
                        report["first_candidate"] = dict(zip(NAMES, candidate.tolist()))
                        report["first_bounded_goal"] = dict(zip(NAMES, target.tolist()))
                        report["first_clipped_joints"] = [n for n, flag in zip(NAMES, clipped) if flag]
                        if np.any(abs(target-state) > 30):
                            motion.warn("first_target_difference", difference=(target-state).tolist())
                    processing_ms = (time.perf_counter()-tick)*1000
                    for name, stamp in stamps.items():
                        age_ms = (time.perf_counter()-stamp)*1000
                        if age_ms > 250:
                            soft_issue("camera_send_age", "Camera frame became over 250 ms old during inference",
                                       camera=name, age_ms=age_ms)
                    try:
                        sent = motion.send(target, source_time=tick, trial_deadline=deadline, jump_check=True)
                    except TemperatureRecovered:
                        policy.reset()
                        pre.reset()
                        post.reset()
                        report["temperature_recovery_count"] = report.get("temperature_recovery_count", 0)+1
                        next_tick = time.perf_counter()
                        continue
                    if sent is None:
                        break
                    clipped_counts += clipped
                    overrun_count += processing_ms > 1000/30
                    writer.writerow([report["model_steps"], sent-start, processing_ms,
                                     (sent-stamps["top"])*1000, (sent-stamps["wrist"])*1000,
                                     (sent-previous_send)*1000 if previous_send is not None else 0,
                                     *state, *candidate, *target, *clipped.astype(int),
                                     motion.last_diagnostics["source_age_at_send_ms"],
                                     *motion.last_diagnostics["command_delta"]])
                    previous_send = sent
                    for name, video in videos.items():
                        video.write(cv2.cvtColor(frames[name], cv2.COLOR_RGB2BGR))
                    capture_step=getattr(controller,'capture_policy_step',None)
                    if callable(capture_step):
                        capture_step(bus=bus,state=state,candidate=candidate,target=target,clipped=clipped,
                            frames=frames,stamps=stamps,observation_time=tick,send_time=sent)
                    next_tick = max(next_tick+PERIOD, tick+PERIOD)
                    report["model_steps"] += 1
                    emit("preview", frames=frames)
                    emit("telemetry", elapsed_s=sent-start, duration_s=args.duration,
                         steps=report["model_steps"], state=state.tolist(), target=target.tolist(),
                         processing_ms=processing_ms, effective_hz=report["model_steps"]/max(sent-start, PERIOD),
                         clipped_counts=clipped_counts.tolist(),
                         camera_age_ms={n: (sent-t)*1000 for n, t in stamps.items()},
                         diagnostic_warning_count=len(motion.warning_records),
                         health=motion.health_records[-1] if motion.health_records else None)
                    if report["model_steps"] % 30 == 0:
                        print(f"ACT_LIVE {sent-start:.1f}s; step={report['model_steps']}; process={processing_ms:.1f}ms; clipped={clipped_counts.tolist()}", flush=True)
                report["trial_elapsed_s"] = time.perf_counter()-start
                report["clipped_steps_by_joint"] = dict(zip(NAMES, clipped_counts.tolist()))
                report["processing_over_33_333ms"] = int(overrun_count)
            report["outcome"] = "completed_timed_trial"
    except KeyboardInterrupt:
        report["outcome"] = "user_stopped"
    except BaseException as error:
        report["outcome"] = "failed_after_enable" if motion.enabled else "preflight_failed"
        report["failure"] = repr(error)
        print(f"ACT_STOPPED {error}", flush=True)
        emit("error", message=str(error))
    finally:
        emit("stage", stage="finishing", message="停止下发模型动作，保存日志；不自动归位或释放力矩。")
        try:
            report["final_feedback"] = motion.hold_on_exit()
        except Exception as error:
            report["holding_update_failure"] = repr(error)
            print(f"ACT HOLD WARNING: {error}; last goal retained", flush=True)
        if listener is not None:
            listener.stop()
        for video in videos.values():
            try:
                video.release()
            except Exception as error:
                report.setdefault("cleanup_errors", []).append(f"video: {error!r}")
        if resources is None:
            for name, camera in cameras.items():
                try:
                    if camera.is_connected:
                        camera.disconnect()
                except Exception as error:
                    report.setdefault("cleanup_errors", []).append(f"{name} camera: {error!r}")
        if bus.is_connected:
            try:
                bus.disconnect(disable_torque=False)
            except Exception as error:
                report.setdefault("cleanup_errors", []).append(f"serial disconnect: {error!r}")
        if previous_preview and previous_preview.get("active"):
            try:
                shadow.preview_select(previous_preview["selected"])
            except Exception as error:
                report["preview_restore_failure"] = repr(error)
        if resource_lease:
            keep=report['outcome'] in ('completed_timed_trial','user_stopped') and not report.get('cleanup_errors') and not report.get('holding_update_failure')
            errors=resources.finish(keep)
            report['resident_resources_retained']=keep
            if errors:report.setdefault('cleanup_errors',[]).extend(errors)
        report["goal_register_writes"] = motion.goal_writes
        report["raw_boundary_rounding_adjustments"] = motion.rounding_adjustments
        report["servo_health_records"] = motion.health_records
        report["diagnostic_warnings"] = motion.warning_records
        report["gripper_overload_recorded_count"] = sum(r["kind"] == "gripper_overload" for r in motion.warning_records)
        with (args.output/"report.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        print(f"ACT_FINISHED {report['outcome']}; steps={report['model_steps']}; torque retained; report={args.output/'report.json'}", flush=True)
        emit("finished", report=report, output=str(args.output))
    if controller is not None:
        return report
    if report["outcome"] not in ("completed_timed_trial", "user_stopped"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
