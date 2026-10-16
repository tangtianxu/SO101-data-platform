"""Replay one real demonstration after read-only calibration and pose checks.

Default is inspection only. The replay uses recorded follower measurements,
not leader commands. Execution first aligns slowly in the same connection,
then preserves the original timing. Out-of-range measurements are rejected,
never silently clipped. No calibration, PID or EEPROM limits are changed.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path

os.environ.setdefault("HF_HOME", r"E:\robot\hf-cache")
os.environ.setdefault("TORCH_HOME", r"E:\robot\torch-cache")
os.environ.setdefault("TEMP", r"E:\robot\temp")
os.environ.setdefault("TMP", r"E:\robot\temp")

import numpy as np
import pyarrow.parquet as pq
from lerobot.motors import Motor, MotorCalibration, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus
from serial.tools import list_ports


ROOT = Path(r"E:\robot\datasets\so101_pick_place_clean_no_hand_20260925")
CALIBRATION = Path(r"E:\robot\hf-cache\lerobot\calibration\robots\so_follower\so101_follower.json")
NAMES = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")
EXPECTED_FOLLOWER_SERIAL = "SET_FOLLOWER_USB_SERIAL"
ALIGN_BODY_SPEED_DEG_S = 2.0
ALIGN_GRIPPER_SPEED_PERCENT_S = 5.0
ALIGN_TOLERANCE = np.array([1.0, 1.0, 1.0, 1.0, 1.0, 3.0])
MAX_INITIAL_DIFFERENCE = np.array([30, 30, 30, 30, 30, 30])


def prepare(episode: int):
    info = json.loads((ROOT / "meta/info.json").read_text(encoding="utf-8"))
    for field in ("action", "observation.state"):
        if info["features"][field]["names"] != [f"{name}.pos" for name in NAMES]:
            raise ValueError(f"Unexpected joint order in {field}")
    if info["fps"] != 30:
        raise ValueError("This verified replay profile requires the recorded 30 Hz dataset")
    table = pq.read_table(sorted((ROOT / "data").glob("chunk-*/*.parquet")),
                          columns=["episode_index", "action", "observation.state"])
    mask = table["episode_index"].to_numpy() == episode
    source = np.asarray(table["observation.state"].to_pylist(), dtype=float)[mask]
    original_action = np.asarray(table["action"].to_pylist(), dtype=float)[mask]
    if not len(source) or not np.isfinite(source).all():
        raise ValueError("Episode is missing or contains non-finite measurements")
    calibration = json.loads(CALIBRATION.read_text(encoding="utf-8"))
    limits = np.asarray([
        [-(calibration[n]["range_max"] - calibration[n]["range_min"]) * 180 / 4095,
         (calibration[n]["range_max"] - calibration[n]["range_min"]) * 180 / 4095]
        if n != "gripper" else [0, 100] for n in NAMES
    ])
    outside = (source < limits[:, 0] - 1e-6) | (source > limits[:, 1] + 1e-6)
    if outside.any():
        raise ValueError("Recorded follower path exceeds existing bounds; ask before changing limits: " +
                         json.dumps(dict(zip(NAMES, outside.sum(axis=0).tolist()))))
    commands = source.copy()
    summary = {"episode_zero_based": episode, "frames": len(source), "fps": info["fps"],
               "duration_s": len(source) / info["fps"], "field": "observation.state",
               "clipped_frames_by_joint": dict(zip(NAMES, [0] * len(NAMES))),
               "original_action_out_of_bounds_frames": dict(zip(NAMES, np.sum(
                   (original_action < limits[:, 0]) | (original_action > limits[:, 1]), axis=0).tolist())),
               "align_body_speed_deg_s": ALIGN_BODY_SPEED_DEG_S,
               "align_gripper_speed_percent_s": ALIGN_GRIPPER_SPEED_PERCENT_S,
               "initial_target": dict(zip(NAMES, commands[0].round(3).tolist())),
               "limits": dict(zip(NAMES, limits.round(3).tolist()))}
    return commands, calibration, info["fps"], summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", type=int, default=3)
    parser.add_argument("--port", default="COM10")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--align-only", action="store_true", help="Only align to this demonstration's start; never play any demonstration frame")
    parser.add_argument("--log", type=Path)
    args = parser.parse_args()
    commands, calibration, fps, summary = prepare(args.episode)
    summary["operation"] = "align_only" if args.align_only else "align_then_replay"
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if not args.execute:
        return
    if args.log is None:
        parser.error("Execution requires an explicit E-drive log path")
    if args.log.exists():
        raise FileExistsError("Refusing to overwrite a previous replay log")
    if args.log.resolve().drive.upper() != "E:":
        raise ValueError("Replay outputs must stay on E drive")
    summary_path = args.log.with_suffix(".json")
    if summary_path.exists():
        raise FileExistsError(summary_path)
    identity = [p for p in list_ports.comports()
                if p.device == args.port and p.serial_number == EXPECTED_FOLLOWER_SERIAL]
    if len(identity) != 1:
        raise RuntimeError("Follower USB identity mismatch; no motion sent")

    bus = FeetechMotorsBus(args.port, {
        n: Motor(calibration[n]["id"], "sts3215",
                 MotorNormMode.RANGE_0_100 if n == "gripper" else MotorNormMode.DEGREES)
        for n in NAMES
    }, calibration={n: MotorCalibration(**calibration[n]) for n in NAMES})
    # One object owns this port. Only goals and torque enable may be written.
    original_write, original_sync_write = bus.write, bus.sync_write
    def guarded_write(key, *a, **kw):
        if key not in ("Goal_Position", "Torque_Enable"):
            raise RuntimeError(f"Register write is forbidden: {key}")
        return original_write(key, *a, **kw)
    def guarded_sync_write(key, *a, **kw):
        if key not in ("Goal_Position", "Torque_Enable"):
            raise RuntimeError(f"Register write is forbidden: {key}")
        return original_sync_write(key, *a, **kw)
    bus.write, bus.sync_write = guarded_write, guarded_sync_write
    running = False
    listener = None
    stop = False
    outcome = "preflight_failed"
    final_feedback = None
    last_frame = None
    failure = None
    max_io_ms = 0.0
    alignment_errors = None
    try:
        bus.connect(handshake=False)
        for register, calibration_key in (("Homing_Offset", "homing_offset"),
                                          ("Min_Position_Limit", "range_min"),
                                          ("Max_Position_Limit", "range_max")):
            current = bus.sync_read(register, normalize=False, num_retry=1)
            if any(current[n] != calibration[n][calibration_key] for n in NAMES):
                raise RuntimeError(f"Live follower calibration mismatch at {register}")
        mode = bus.sync_read("Operating_Mode", normalize=False, num_retry=1)
        status = bus.sync_read("Status", normalize=False, num_retry=1)
        torque = bus.sync_read("Torque_Enable", normalize=False, num_retry=1)
        if any(mode.values()) or any(status.values()) or any(v not in (0, 1) for v in torque.values()):
            raise RuntimeError(f"Preflight requires position mode, zero errors, known torque state: {mode}, {status}, {torque}")
        initial = bus.sync_read("Present_Position", num_retry=1)
        initial_array = np.array([initial[n] for n in NAMES])
        errors = np.abs(initial_array - commands[0])
        if np.any(errors > MAX_INITIAL_DIFFERENCE):
            raise RuntimeError("Current pose is too far away for this approved alignment: " +
                               json.dumps(dict(zip(NAMES, errors.round(2).tolist()))))
        initial_raw = bus.sync_read("Present_Position", normalize=False, num_retry=1)
        if any(not calibration[n]["range_min"] <= initial_raw[n] <= calibration[n]["range_max"] for n in NAMES):
            raise RuntimeError("Present position is outside existing bounds; no torque enabled")
        summary["initial_measured"] = initial
        summary["initial_torque"] = torque
        summary["usb_serial"] = EXPECTED_FOLLOWER_SERIAL
        summary["port"] = args.port
        summary["calibration"] = calibration

        from pynput import keyboard
        def on_press(key):
            nonlocal stop
            if key == keyboard.Key.esc:
                stop = True
        listener = keyboard.Listener(on_press=on_press)
        listener.start()
        args.log.parent.mkdir(parents=True, exist_ok=True)
        with args.log.open("x", newline="", encoding="utf-8") as log:
            writer = csv.writer(log)
            writer.writerow(["wall_elapsed_s", "frame", "loop_ms", "phase"] +
                            [f"target_{n}" for n in NAMES] + [f"actual_{n}" for n in NAMES] +
                            ["read_write_ms", "schedule_lag_ms"])
            if stop:
                raise KeyboardInterrupt
            # Load the current position before enabling torque: no stale goal.
            bus.sync_write("Goal_Position", initial_raw, normalize=False)
            # Feetech.enable_torque() also writes EEPROM Lock. Use only the
            # torque register, so the register allow-list stays exact.
            running = True
            bus.sync_write("Torque_Enable", {n: 1 for n in NAMES}, normalize=False)
            enabled = bus.sync_read("Torque_Enable", normalize=False, num_retry=1)
            if any(v != 1 for v in enabled.values()):
                raise RuntimeError("Not all follower motors enabled torque")
            start = time.perf_counter()
            previous_tick = start
            last_health = start
            next_deadline = start
            period = 1.0 / fps

            def send_and_log(target, frame, phase):
                nonlocal previous_tick, max_io_ms, last_health, next_deadline
                remaining = next_deadline - time.perf_counter()
                if remaining > 0:
                    time.sleep(remaining)
                if stop:
                    return None
                tick = time.perf_counter()
                lag = tick - next_deadline
                if lag > 0.15:
                    raise RuntimeError("Replay is >150 ms late; refusing a catch-up burst")
                if tick - last_health >= 0.5:
                    live_status = bus.sync_read("Status", normalize=False, num_retry=1)
                    if any(live_status.values()):
                        raise RuntimeError(f"Follower reports servo error: {live_status}")
                    last_health = tick
                actual = bus.sync_read("Present_Position", num_retry=1)
                if time.perf_counter() - tick > 0.15:
                    raise RuntimeError("Feedback read exceeded 150 ms; no next goal sent")
                # Validate exact encoder targets as well as degree-space limits.
                raw_by_id = bus._unnormalize({calibration[n]["id"]: float(target[i]) for i, n in enumerate(NAMES)})
                raw_goal = {n: raw_by_id[calibration[n]["id"]] for n in NAMES}
                if any(not calibration[n]["range_min"] <= raw_goal[n] <= calibration[n]["range_max"] for n in NAMES):
                    raise RuntimeError("Target encoder count exceeds an unchanged servo bound")
                bus.sync_write("Goal_Position", raw_goal, normalize=False)
                finished = time.perf_counter()
                io_ms = (finished - tick) * 1000
                max_io_ms = max(max_io_ms, io_ms)
                writer.writerow([round(tick - start, 6), frame, round((tick - previous_tick) * 1000, 3), phase,
                                 *target, *[actual[n] for n in NAMES], round(io_ms, 3), round(lag * 1000, 3)])
                previous_tick = tick
                if io_ms > 150:
                    raise RuntimeError("Replay I/O exceeded 150 ms")
                # Never compress two trajectory steps into a catch-up burst.
                next_deadline = max(next_deadline + period, tick + period)
                return np.array([actual[n] for n in NAMES])

            # One port owner and uninterrupted torque through the transition.
            speeds = np.array([ALIGN_BODY_SPEED_DEG_S] * 5 + [ALIGN_GRIPPER_SPEED_PERCENT_S])
            alignment_duration = max(float(np.max(errors / speeds)), 1.0)
            alignment_frames = int(np.ceil(alignment_duration * fps))
            print(f"ALIGN_START duration >= {alignment_duration:.2f}s; body {ALIGN_BODY_SPEED_DEG_S}deg/s", flush=True)
            for frame in range(alignment_frames):
                target = initial_array + (commands[0] - initial_array) * (frame + 1) / alignment_frames
                if send_and_log(target, -alignment_frames + frame, "align_slow") is None:
                    break
                if frame % int(fps) == 0:
                    print(f"ALIGN {frame/fps:.1f}/{alignment_frames/fps:.1f}s", flush=True)
            stable = 0
            waiting_start = time.perf_counter()
            while not stop:
                actual = send_and_log(commands[0], -1, "align_verify")
                if actual is None:
                    break
                alignment_errors = abs(actual - commands[0])
                stable = stable + 1 if np.all(alignment_errors <= ALIGN_TOLERANCE) else 0
                if stable >= 3:
                    print("ALIGN_COMPLETE " + json.dumps(dict(zip(NAMES, alignment_errors.round(3).tolist()))), flush=True)
                    break
                if time.perf_counter() - waiting_start > 15:
                    raise RuntimeError("Alignment failed to converge in 15s; demonstration not started")
            if args.align_only:
                outcome = "user_stopped" if stop else "aligned_only"
            else:
                for frame in range(len(commands)):
                    if stop:
                        outcome = "user_stopped"
                        break
                    if send_and_log(commands[frame], frame, "replay") is None:
                        outcome = "user_stopped"
                        break
                    last_frame = frame
                    if frame % int(fps) == 0:
                        print(f"REPLAY {frame/fps:.1f}/{len(commands)/fps:.1f} s", flush=True)
                else:
                    outcome = "completed"
                    time.sleep(period)
            log.flush()
    except KeyboardInterrupt:
        outcome = "user_stopped"
    except Exception as error:
        failure = repr(error)
        outcome = "failed_after_enable" if running else "preflight_failed"
        print(f"REPLAY_STOPPED: {error}", flush=True)
    finally:
        if running and bus.is_connected:
            # Hold the measured position on interruption, if feedback is available.
            try:
                now_raw = bus.sync_read("Present_Position", normalize=False, num_retry=1)
                if outcome not in ("completed", "aligned_only"):
                    bus.sync_write("Goal_Position", now_raw, normalize=False)
                final_feedback = {"position_raw": now_raw,
                                  "torque": bus.sync_read("Torque_Enable", normalize=False, num_retry=1),
                                  "status": bus.sync_read("Status", normalize=False, num_retry=1),
                                  "temperature": bus.sync_read("Present_Temperature", normalize=False, num_retry=1)}
            except Exception as e:
                print(f"Could not update holding goal: {e}", flush=True)
        if listener is not None:
            listener.stop()
        if bus.is_connected:
            bus.disconnect(disable_torque=False)
        summary.update({"outcome": outcome, "failure": failure, "last_replayed_frame": last_frame,
                        "max_io_ms": round(max_io_ms, 3), "final_feedback": final_feedback,
                        "alignment_error": dict(zip(NAMES, alignment_errors.tolist())) if alignment_errors is not None else None,
                        "boundary_changed": False, "exit_releases_torque": False})
        args.log.parent.mkdir(parents=True, exist_ok=True)
        with summary_path.open("x", encoding="utf-8") as summary_file:
            json.dump(summary, summary_file, ensure_ascii=False, indent=2)
        print(f"Outcome={outcome}; port closed; existing torque state preserved; log={args.log}", flush=True)
    if outcome not in ("completed", "aligned_only", "user_stopped"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
