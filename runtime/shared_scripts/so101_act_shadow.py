"""ACT live-input timing and output audit. All servo register writes are forbidden."""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
import urllib.request
from pathlib import Path

import so101_profile as profile
profile.initialize_environment()

import cv2
import numpy as np
import torch
from serial.tools import list_ports
from lerobot.cameras.opencv import OpenCVCamera, OpenCVCameraConfig
from lerobot.motors import Motor, MotorCalibration, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus
from lerobot.policies.act.configuration_act import ACTConfig
from lerobot.policies.act.modeling_act import ACTPolicy, ACTTemporalEnsembler
from lerobot.policies.factory import make_pre_post_processors


CHECKPOINT = profile.resource("checkpoint", "models/act/pretrained_model")
CALIBRATION = profile.resource("calibration", "config/follower.json")
NAMES = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")
CAMERA_URL = "http://127.0.0.1:8768"


def preview_status():
    # Loopback robot utilities must not be routed through an HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(CAMERA_URL + "/api/status", timeout=2) as response:
        return json.load(response)


def preview_select(camera):
    request = urllib.request.Request(CAMERA_URL + "/api/camera",
        data=json.dumps({"camera": camera}).encode(), method="POST",
        headers={"Content-Type": "application/json", "Origin": CAMERA_URL})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=3) as response:
        return json.load(response)


def stats(values):
    values = np.array(values, dtype=float)
    return {"min": float(values.min()), "median": float(np.median(values)),
            "p95": float(np.percentile(values, 95)), "p99": float(np.percentile(values, 99)),
            "max": float(values.max())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--frames", default=180, type=int)
    parser.add_argument("--profile", choices=("both", "ensemble", "chunk10"), default="both")
    args = parser.parse_args()
    profile.output_path(args.output)
    if args.output.exists():
        raise FileExistsError("Refusing to overwrite an earlier shadow audit")
    if args.frames < 30 or args.frames > 600:
        raise ValueError("This bounded shadow test requires 30..600 frames per profile")
    args.output.mkdir(parents=True)
    calibration = json.loads(CALIBRATION.read_text(encoding="utf-8"))
    port = profile.resolve_port("follower", list_ports.comports())
    limits = np.array([
        [-(calibration[n]["range_max"] - calibration[n]["range_min"]) * 180 / 4095,
         (calibration[n]["range_max"] - calibration[n]["range_min"]) * 180 / 4095]
        if n != "gripper" else [0, 100] for n in NAMES
    ])
    bus = FeetechMotorsBus(port, {
        n: Motor(calibration[n]["id"], "sts3215", MotorNormMode.RANGE_0_100 if n == "gripper" else MotorNormMode.DEGREES)
        for n in NAMES
    }, calibration={n: MotorCalibration(**calibration[n]) for n in NAMES})
    attempted_writes = 0

    def no_writes(*a, **kw):
        nonlocal attempted_writes
        attempted_writes += 1
        raise RuntimeError("SHADOW MODE: motor register writes are strictly forbidden")

    bus.write = no_writes
    bus.sync_write = no_writes
    bus._write = no_writes
    bus._sync_write = no_writes
    cameras = {}
    previous_preview = None
    report = {"kind": "LIVE INPUT SHADOW INFERENCE — NO MOTION COMMANDS", "checkpoint": str(CHECKPOINT),
              "joint_order": list(NAMES), "joint_units": ["degree"] * 5 + ["gripper_percent"],
              "candidate_prediction_horizon_steps": 100, "requested_control_fps": 30,
              "bounds": dict(zip(NAMES, limits.tolist())),
              "input_preprocessing": "Raw live RGB, float32/255 and original checkpoint processors; no reference overlay, no synthetic image patch",
              "limitations": ["Arm does not follow predictions, so this is not closed-loop task evaluation",
                              "Current object placement and robot pose are not automatically reset",
                              "Timing excludes actual command transmission, which is forbidden in shadow mode",
                              "Camera timestamps mark host receipt, not hardware exposure; no hardware synchronization is claimed"],
              "profiles": []}
    try:
        previous_preview = preview_status()
        preview_select(None)
        deadline = time.perf_counter() + 5
        while time.perf_counter() < deadline:
            if all(c["phase"] == "paused" for c in preview_status()["cameras"].values()):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Camera preview did not release both devices")
        bus.connect(handshake=False)
        for register, key in (("Homing_Offset", "homing_offset"), ("Min_Position_Limit", "range_min"), ("Max_Position_Limit", "range_max")):
            current = bus.sync_read(register, normalize=False, num_retry=1)
            if any(current[n] != calibration[n][key] for n in NAMES):
                raise RuntimeError(f"Live calibration mismatch at {register}")
        report["torque_before"] = bus.sync_read("Torque_Enable", normalize=False, num_retry=1)
        report["goal_before_raw"] = bus.sync_read("Goal_Position", normalize=False, num_retry=1)
        if any(bus.sync_read("Operating_Mode", normalize=False, num_retry=1).values()):
            raise RuntimeError("Follower not in position mode")
        if any(bus.sync_read("Status", normalize=False, num_retry=1).values()):
            raise RuntimeError("Follower reports a servo error")
        print("SHADOW_READ_ONLY: writes disabled; loading original real-image ACT weights", flush=True)
        torch.set_num_threads(1)
        cfg = ACTConfig.from_pretrained(str(CHECKPOINT), local_files_only=True)
        if cfg.chunk_size != 100 or not torch.cuda.is_available():
            raise RuntimeError("Expected trained chunk=100 and local CUDA GPU")
        cfg.n_action_steps = 1
        cfg.temporal_ensemble_coeff = 0.01
        # Full backbone weights are in the checkpoint. Do not download an
        # unrelated initialization checkpoint; strict loading verifies coverage.
        cfg.pretrained_backbone_weights = None
        policy = ACTPolicy.from_pretrained(str(CHECKPOINT), config=cfg, local_files_only=True, strict=True)
        policy.eval()
        pre, post = make_pre_post_processors(cfg, pretrained_path=str(CHECKPOINT))
        report["gpu"] = torch.cuda.get_device_name(0)
        for name, index in (("top", 0), ("wrist", 1)):
            camera = OpenCVCamera(OpenCVCameraConfig(index_or_path=index, backend=700,
                                  width=640, height=480, fps=30, fourcc="MJPG", warmup_s=1))
            cameras[name] = camera
            camera.connect()
        print("SHADOW_CAMERAS_READY: top=0 wrist=1; RGB 640x480", flush=True)

        def observation():
            io_start = time.perf_counter()
            state = bus.sync_read("Present_Position", num_retry=1)
            state_time = time.perf_counter()
            state_array = np.array([state[n] for n in NAMES], dtype=np.float32)
            frames, stamps = {}, {}
            for name, camera in cameras.items():
                if camera.thread is None or not camera.thread.is_alive():
                    raise RuntimeError(f"{name} capture thread stopped")
                with camera.frame_lock:
                    frame = camera.latest_frame
                    timestamp = camera.latest_timestamp
                    if frame is None or timestamp is None:
                        raise RuntimeError(f"{name} has no frame")
                    frames[name] = frame.copy()
                    stamps[name] = timestamp
                if frames[name].shape != (480, 640, 3):
                    raise ValueError(f"Unexpected {name} frame shape")
                if (time.perf_counter() - stamps[name]) > 0.5:
                    raise TimeoutError(f"{name} image older than 500 ms")
            io_end = time.perf_counter()
            batch = {"observation.state": torch.from_numpy(state_array)}
            for name in cameras:
                batch[f"observation.images.{name}"] = torch.from_numpy(frames[name]).permute(2, 0, 1).float().div_(255)
            meta = {"acquire_ms": (io_end - io_start) * 1000,
                    "tensor_ms": (time.perf_counter() - io_end) * 1000,
                    "top_age_ms": (io_end - stamps["top"]) * 1000,
                    "wrist_age_ms": (io_end - stamps["wrist"]) * 1000,
                    "state_age_at_snapshot_ms": (io_end - state_time) * 1000,
                    "camera_pair_receipt_gap_ms": abs(stamps["top"] - stamps["wrist"]) * 1000,
                    "top_timestamp": stamps["top"], "wrist_timestamp": stamps["wrist"]}
            return batch, state_array, frames, meta

        batch, initial, frames, meta = observation()
        report["observed_pose_at_input_capture"] = dict(zip(NAMES, initial.tolist()))
        for name, image in frames.items():
            cv2.imwrite(str(args.output / f"input_{name}.png"), cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
        with torch.inference_mode():
            processed = pre(batch)
            for _ in range(20):
                policy.select_action(processed)
            torch.cuda.synchronize()
            policy.reset()
            chunk = post(policy.predict_action_chunk(processed)).detach().cpu().numpy().reshape(100, 6)
            if not np.isfinite(chunk).all():
                raise FloatingPointError("Non-finite candidate chunk")
            np.savetxt(args.output / "initial_candidate_chunk.csv", chunk, delimiter=",", header=",".join(NAMES), comments="")
            chunk_oob = (chunk < limits[:, 0]) | (chunk > limits[:, 1])
            report["initial_candidate_chunk"] = {
                "first_target": dict(zip(NAMES, chunk[0].tolist())),
                "first_target_minus_current": dict(zip(NAMES, (chunk[0] - initial).tolist())),
                "range_min": dict(zip(NAMES, chunk.min(axis=0).tolist())),
                "range_max": dict(zip(NAMES, chunk.max(axis=0).tolist())),
                "max_neighbor_change": dict(zip(NAMES, abs(np.diff(chunk, axis=0)).max(axis=0).tolist())),
                "outside_existing_bounds_count": dict(zip(NAMES, chunk_oob.sum(axis=0).tolist())),
                "all_100_future_points_are_candidates_only": True,
            }
            profiles = (("100pred_1exec_TE001", 1, 0.01), ("100pred_10exec_noTE", 10, None))
            if args.profile == "ensemble":
                profiles = profiles[:1]
            elif args.profile == "chunk10":
                profiles = profiles[1:]
            for label, n_action_steps, coeff in profiles:
                policy.config.n_action_steps = n_action_steps
                policy.config.temporal_ensemble_coeff = coeff
                if coeff is not None:
                    policy.temporal_ensembler = ACTTemporalEnsembler(coeff, 100)
                policy.reset()
                pre.reset()
                post.reset()
                timings, actions, states, model_ticks, top_stamps, wrist_stamps = [], [], [], [], [], []
                profile_start = time.perf_counter()
                next_tick = profile_start
                previous_tick = None
                with (args.output / f"{label}.csv").open("x", newline="", encoding="utf-8") as log:
                    writer = csv.writer(log)
                    writer.writerow(["step", "elapsed_s", "loop_interval_ms", "acquire_ms", "tensor_ms", "pre_ms", "policy_ms", "post_ms", "prepare_goal_ms", "total_pre_send_ms", "deadline_lag_ms", "model_called", "top_age_ms", "wrist_age_ms", "camera_pair_receipt_gap_ms", "top_timestamp", "wrist_timestamp", *[f"state_{n}" for n in NAMES], *[f"candidate_{n}" for n in NAMES]])
                    for step in range(args.frames):
                        remaining = next_tick - time.perf_counter()
                        if remaining > 0:
                            time.sleep(remaining)
                        tick = time.perf_counter()
                        batch, state, _, data = observation()
                        pre_start = time.perf_counter()
                        processed = pre(batch)
                        torch.cuda.synchronize()
                        model_start = time.perf_counter()
                        called = coeff is not None or len(policy._action_queue) == 0
                        selected_action = policy.select_action(processed)
                        torch.cuda.synchronize()
                        post_start = time.perf_counter()
                        action = post(selected_action).detach().cpu().numpy().reshape(6)
                        torch.cuda.synchronize()
                        goal_start = time.perf_counter()
                        if not np.isfinite(action).all():
                            raise FloatingPointError("Policy output became non-finite")
                        # Only exercise the conversion: never send this goal.
                        bus._unnormalize({calibration[n]["id"]: float(action[i]) for i, n in enumerate(NAMES)})
                        finished = time.perf_counter()
                        total_ms = (finished - tick) * 1000
                        timings.append([data["acquire_ms"], data["tensor_ms"], (model_start-pre_start)*1000,
                                        (post_start-model_start)*1000, (goal_start-post_start)*1000,
                                        (finished-goal_start)*1000, total_ms, (tick-next_tick)*1000,
                                        data["top_age_ms"], data["wrist_age_ms"], data["camera_pair_receipt_gap_ms"]])
                        actions.append(action.copy())
                        states.append(state.copy())
                        model_ticks.append(called)
                        top_stamps.append(data["top_timestamp"])
                        wrist_stamps.append(data["wrist_timestamp"])
                        writer.writerow([step, tick-profile_start, (tick-previous_tick)*1000 if previous_tick is not None else 0,
                            *timings[-1][:8], int(called), *timings[-1][8:], data["top_timestamp"], data["wrist_timestamp"], *state, *action])
                        previous_tick = tick
                        next_tick = max(next_tick + 1/30, tick + 1/30)
                        if step % 30 == 0:
                            print(f"SHADOW {label} {step}/{args.frames}; pre-send {total_ms:.1f}ms; NO COMMAND SENT", flush=True)
                elapsed = time.perf_counter() - profile_start
                matrix = np.array(timings)
                action_array = np.array(actions)
                states_array = np.array(states)
                called_array = np.array(model_ticks)
                outside = (action_array < limits[:, 0]) | (action_array > limits[:, 1])
                profile = {"label": label, "chunk_size": 100, "n_action_steps": n_action_steps,
                    "temporal_ensemble_coeff": coeff, "frames": len(actions), "elapsed_s": elapsed,
                    "total_pre_send_ms": stats(matrix[:, 6]),
                    "actual_model_call_policy_ms": stats(matrix[called_array, 3]),
                    "pre_send_over_33_333ms_count": int(np.sum(matrix[:, 6] > 1000/30)),
                    "top_age_ms": stats(matrix[:, 8]), "wrist_age_ms": stats(matrix[:, 9]),
                    "camera_pair_receipt_gap_ms": stats(matrix[:, 10]),
                    "unique_top_frames": len(set(top_stamps)), "unique_wrist_frames": len(set(wrist_stamps)),
                    "outside_existing_bounds_count": dict(zip(NAMES, outside.sum(axis=0).tolist())),
                    "max_outside_existing_bounds": dict(zip(NAMES, np.maximum(
                        np.maximum(limits[:, 0]-action_array, action_array-limits[:, 1]), 0).max(axis=0).tolist())),
                    "first_target_minus_current": dict(zip(NAMES, (action_array[0]-states_array[0]).tolist())),
                    "max_neighbor_output_change": dict(zip(NAMES, abs(np.diff(action_array, axis=0)).max(axis=0).tolist())),
                    "observed_pose_change_during_shadow": dict(zip(NAMES, np.ptp(states_array, axis=0).tolist())),
                    "phase_component_ms": {key: stats(matrix[:, i]) for i, key in enumerate(("acquire", "tensor", "preprocessor", "policy", "postprocessor", "prepare_goal"))}}
                report["profiles"].append(profile)
                print("PROFILE_DONE " + json.dumps({k: profile[k] for k in ("label", "total_pre_send_ms", "pre_send_over_33_333ms_count", "outside_existing_bounds_count")}), flush=True)
        report["torque_after"] = bus.sync_read("Torque_Enable", normalize=False, num_retry=1)
        report["goal_after_raw"] = bus.sync_read("Goal_Position", normalize=False, num_retry=1)
        report["final_status"] = bus.sync_read("Status", normalize=False, num_retry=1)
        report["goal_register_unchanged"] = report["goal_before_raw"] == report["goal_after_raw"]
        report["torque_register_unchanged"] = report["torque_before"] == report["torque_after"]
        report["outcome"] = "completed"
    except BaseException as error:
        report["outcome"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        report["failure"] = repr(error)
        print(f"SHADOW_STOPPED {error}", flush=True)
    finally:
        for camera in cameras.values():
            if camera.is_connected:
                camera.disconnect()
        if bus.is_connected:
            bus.disconnect(disable_torque=False)
        if previous_preview and previous_preview.get("active"):
            try:
                preview_select(previous_preview["selected"])
            except Exception as error:
                report["preview_restore_failure"] = str(error)
        report["attempted_register_writes"] = attempted_writes
        report["commands_sent"] = 0
        report["no_boundary_or_checkpoint_changes"] = True
        with (args.output / "report.json").open("x", encoding="utf-8") as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
        print(f"SHADOW_FINISHED {report.get('outcome')} commands_sent=0 report={args.output / 'report.json'}", flush=True)
    if report.get("outcome") != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
