"""Loopback-only UI host. Standard library; no Torch/OpenCV/device imports here."""
from __future__ import annotations

import argparse
import base64
from collections import deque
from datetime import datetime
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, quote, urlparse
import webbrowser

from common import HERE, STATE_DIR, ROBOT_HOME, atomic_json, environment, load_config, number, robot_path, validate_config, input_path, ensure_workspace, capabilities, require_capability, read_calibration
from dataset_library import archive_dataset, child_folder, create_dataset, list_archives, list_datasets, restore_dataset
from reset_profile import inspect_pose, save_pose_copy
from dataset_naming import DOMAINS, TASKS, PURPOSES, STAGES, REVIEWS, defaults, describe, preview_name, store_metadata, suggest_name, validate_capture, validate_spec, simple_defaults, capture_plan
import feedback_store
import joint_control
import software_limits
from app_identity import Deployment
from crop_collection import plan_crop
from review_store import read_collection, read_reviews, require_success, save_review, save_reviews, review_revision

PREFIX = "SO101_EVENT "
KINDS = {"preview", "camera_scan", "monitor", "control", "teleop", "record", "act", "replay", "crop"}
CAMERA_TASKS = {"teleop", "record", "act"}


class Platform:
    def __init__(self, offline=False):
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.config = load_config()
        ensure_workspace(self.config)
        self.restart_required = False
        self.deployment=Deployment()
        self.token = secrets.token_urlsafe(32)
        self.proc = None
        self.job = None
        self.preview_proc = None
        self.preview_job = None
        self.frames = {}
        self.frame_owner = None
        self.events = deque(maxlen=200)
        self.telemetry = {}
        self.catalog_cache = {}
        self.offline = offline
        self.log("info", "界面已就绪。未连接相机、串口或模型。")

    def log(self, level, message):
        with self.lock:
            self.events.append({"time": datetime.now().strftime("%H:%M:%S"), "level": level,
                                "message": str(message)[:2000]})

    def active(self):
        return self.proc is not None and self.proc.poll() is None

    def preview_active(self):
        return self.preview_proc is not None and self.preview_proc.poll() is None

    def resources_active(self):
        return self.active() or self.preview_active()

    def camera_owner(self):
        if self.preview_active():
            return self.preview_job["id"]
        if self.active() and self.job["kind"] in CAMERA_TASKS:
            return self.job["id"]
        return None

    def snapshot(self):
        with self.lock:
            disk = shutil.disk_usage(self.config["dataset_home"])
            memory = None
            try:
                import psutil
                parent = psutil.Process()
                memory = {"host_mb": round(parent.memory_info().rss / 1048576, 1), "worker_mb": 0}
                if self.active():
                    memory["worker_mb"] = round(psutil.Process(self.proc.pid).memory_info().rss / 1048576, 1)
                memory["preview_mb"] = round(psutil.Process(self.preview_proc.pid).memory_info().rss / 1048576, 1) if self.preview_active() else 0
            except Exception:
                memory = None  # Resource sampling must not break the control/status channel.
            return {"config": self.config, "active": self.active(), "job": self.job,
                    "preview_active": self.preview_active(), "preview_job": self.preview_job,
                    "resources_active": self.resources_active(), "camera_owner_id": self.camera_owner(),
                    "camera_active": self.camera_owner() is not None, "frame_owner": self.frame_owner,
                    "telemetry": self.telemetry, "events": list(self.events),
                    "frames": {k: v[1] for k, v in self.frames.items()},
                    "disk_free_gb": round(disk.free / 1073741824, 1), "memory": memory,
                    "project": str(HERE), "offline": self.offline,"identity":self.deployment.identity(),
                    "restart_required": self.restart_required, "capabilities": capabilities(self.config)}

    def start(self, request):
        if self.restart_required:
            raise RuntimeError('工作区或服务端口已变更，请先关闭并重新打开平台')
        kind = request.get("kind")
        if kind not in KINDS:
            raise ValueError("未知操作")
        if self.offline and kind != "crop":
            raise PermissionError("本次为离线验收：相机、串口及运动入口全部锁定")
        if self.deployment.changed(force=True):raise RuntimeError('程序源文件已更新，请结束当前会话并重开正式入口，不混用前后台版本')
        with self.lock:
            independent_preview = kind == "preview"
            if independent_preview:
                if self.preview_active():
                    raise RuntimeError("独立相机预览已开启，不重复打开设备")
                if self.active() and self.job["kind"] not in ("monitor", "control"):
                    raise RuntimeError("当前任务独占设备；相机预览仅与监控、基本控制并行")
            elif self.active():
                raise RuntimeError("已有设备会话。请先正常停止，等待设备释放；不会自动强杀。")
            if not independent_preview and self.preview_active() and kind not in ("monitor", "control"):
                raise RuntimeError("请先正常结束独立相机预览，等待相机释放后再启动此任务")
            cfg = self.config.copy()
            payload = dict(request.get("options") or {})
            create_capture=payload.get('capture_mode')=='new'
            record_plan=None
            if kind == "record":
                from capture_groups import prepare_record
                record_plan=prepare_record(cfg,payload)
            if kind == "act":
                cfg["act_seconds"] = number(payload.get("duration", cfg["act_seconds"]), .5, 17.5, "ACT 时限")
                cfg['task']=str(payload.get('task') or cfg['task']).strip()
                if not cfg['task'] or len(cfg['task'])>300:raise ValueError('ACT 任务描述应为 1–300 字')
                payload['group']=str(payload.get('group') or '固定位置').strip()[:100]
                if "reference_start_pose" in payload:
                    cfg["reference_start_pose"] = str(input_path(payload["reference_start_pose"]))
            if kind == "control" and payload.get("initial_command") is not None:
                if payload["initial_command"] not in ("hold", "release", "reset") or payload.get("confirmed") is not True:
                    raise ValueError("控制快捷入口需要明确确认保持、支撑释放或完整复位路线")
                if "reference_start_pose" in payload:
                    cfg["reference_start_pose"] = str(input_path(payload["reference_start_pose"]))
            if kind == "act" or (kind == "control" and payload.get("initial_command") == "reset"):
                inspect_pose(cfg)  # File/coordinate checks only, before spawning any hardware owner.
            if kind == "replay":
                payload["dataset_root"] = str(robot_path(payload.get("dataset_root", ""), existing=True))
                payload["episode"] = number(payload.get("episode", -1), 0, 100000, "episode", True)
            if kind == "crop":
                from motion_trim import prepare_job
                payload = prepare_job(cfg,payload,self.job)
            require_capability(cfg, 'preview' if kind=='camera_scan' else kind)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3)
            out = Path(cfg["log_home"]) / stamp
            out.mkdir(parents=True, exist_ok=False)
            if kind == "crop":
                from motion_trim import materialize
                payload = materialize(payload,out)
            if kind=='act':
                payload['feedback_root']=feedback_store.allocate(cfg,stamp,out,payload)
            atomic_json(out / "session.json", {"kind": kind, "config": cfg, "options": payload,
                                              "hardware_access_permitted": kind != "crop", "created": stamp})
            if kind=="record":
                if record_plan:
                    from capture_groups import commit_groups
                    commit_groups(record_plan)
                elif create_capture:
                    create_dataset(cfg,Path(payload['dataset_root']).name)
                if not record_plan:store_metadata(payload["dataset_root"],payload["dataset_spec"],source="capture_plan")
            job = {"id": stamp, "kind": kind, "phase": "LOADING", "message": "正在准备工作进程",
                        "output": str(out), "started": time.time(), "finished": False}
            if independent_preview:
                self.preview_job = job
            else:
                self.job = job
                self.telemetry = {}
            if kind=='record':
                job['dataset_root']=payload['dataset_root']
                job['capture_roots']=[t['root'] for t in payload.get('capture_targets',[])] or [payload['dataset_root']]
            if kind=='act':job['feedback_root']=payload['feedback_root']
            if independent_preview or kind in CAMERA_TASKS or kind == "camera_scan":
                self.frames, self.frame_owner = {}, None
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = None
            try:
                proc = subprocess.Popen([cfg["python"], "-s", "-B", "-u", "-X", "utf8",
                                              str(HERE / "worker.py")], cwd=HERE, env=environment(cfg),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=creationflags)
                if independent_preview:
                    self.preview_proc = proc
                else:
                    self.proc = proc
                proc.stdin.write(json.dumps({"kind": kind, "config": cfg, "options": payload,
                                                   "output": str(out)}, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except Exception:
                if proc is not None and proc.poll() is None:
                    proc.stdin.close()  # EOF asks the worker to stop, never a hardware-process kill.
                job.update(phase="ERROR", finished=True, message="工作进程启动失败")
                if kind=='act':feedback_store.update(payload['feedback_root'],state='incomplete',runtime_outcome='worker_start_failed')
                raise
            threading.Thread(target=self._read, args=(proc, job, out), daemon=True).start()
            self.log("info", f"启动 {kind}，PID {proc.pid}；设备只由此工作进程持有")
            return job.copy()

    def save_config(self, request):
        with self.lock:
            if self.resources_active():
                raise RuntimeError('设备或导出会话运行中不能修改配置')
            raw = dict(request)
            confirmed = raw.pop('_binding_confirmed', False)
            binding = ('leader_usb_serial','follower_usb_serial','leader_port','follower_port','calibration','leader_calibration')
            changed = any(raw.get(k,self.config[k]) != self.config[k] for k in binding)
            if changed and confirmed is not True:
                raise ValueError('更换设备身份或标定需要明确确认；保存不会连接设备')
            if raw.get('calibration',self.config['calibration']) != self.config['calibration']:
                raw.update(manual_joint_limits={},manual_limits_calibration_id=None)
                if raw.get('reference_start_pose',self.config['reference_start_pose']) == self.config['reference_start_pose']:
                    raw['reference_start_pose']=''
            cfg = validate_config({**self.config,**raw})
            for key in ('calibration','leader_calibration'):
                if cfg[key]:read_calibration(cfg[key])
            if cfg['reference_start_pose']:inspect_pose(cfg)
            restart = any(cfg[k] != self.config[k] for k in ('workspace_root','server_port'))
            atomic_json(STATE_DIR/'config_history'/('before_'+str(time.time_ns())+'.json'),self.config)
            atomic_json(STATE_DIR/'settings.json',cfg)
            self.restart_required = restart
            if not restart:
                ensure_workspace(cfg)
                self.config = cfg
                self.catalog_cache.clear()
            self.log('info','配置已保存；需关闭并重新打开平台后启用新工作区/端口' if restart else '配置已保存，未连接或移动设备')
            return {**cfg,'_restart_required':restart}

    def inventory(self, ports=False):
        python=input_path(self.config['python'])
        process=subprocess.run([str(python),'-s','-B','-X','utf8',str(HERE/'device_inventory.py')]+(['--ports'] if ports else []),
            cwd=HERE,env=environment(self.config),capture_output=True,text=True,encoding='utf-8',timeout=20,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if process.returncode:
            raise ValueError('所选 Python 缺少依赖或检查失败；请先配置 LeRobot 环境：'+process.stderr[-800:])
        return json.loads(process.stdout)

    def _read(self, proc, job, out):
        with (out / "console.log").open("a", encoding="utf-8") as log:
            for line in proc.stdout:
                if line.startswith(PREFIX):
                    try:
                        event = json.loads(line[len(PREFIX):])
                        kind = event.pop("type")
                        with self.lock:
                            if kind == "frame":
                                raw = base64.b64decode(event["data"], validate=True)
                                if len(raw) <= 500000 and event["camera"] in ("top", "wrist") and self.camera_owner() == job["id"]:
                                    self.frames[event["camera"]] = (raw, time.time())
                                    self.frame_owner = job["id"]
                            elif kind == "telemetry":
                                self.telemetry = event
                            elif kind == "phase":
                                job.update(event)
                                self.log("info", event.get("message", event.get("phase", "")))
                            elif kind == "error":
                                job.update(phase="ERROR", message=event.get("message", "设备会话异常"))
                                self.log("error", job["message"])
                            elif kind == "done":
                                if job["kind"]=="crop" and event.get("outcome")=="cropped":
                                    session=json.loads((out/"session.json").read_text(encoding="utf-8"))
                                    options=session["options"]
                                    store_metadata(event["destination"],options["dataset_spec"],source="crop_collection",
                                        lineage={"operation":"crop_collection","source":options["dataset_root"]})
                                    self.catalog_cache.clear()
                                job.update(finished=True, result=event)
                                if job["phase"] != "ERROR":
                                    job.update(phase="FINISHED", message="裁剪集合已更新；同一原轨迹仅保留一份，原始数据未修改" if job["kind"] == "crop" and event.get("outcome") == "cropped" else "会话结束；请查看结果，退出不自动释放力矩")
                                    if job["kind"] == "preview":
                                        job["message"] = "相机预览已关闭；未连接机械臂，也未改变力矩"
                                    if job["kind"] == "crop" and event.get("outcome") == "motion_preview":
                                        job["message"] = "运动边界分析完成，请查看建议区间；尚未写入裁剪集"
                                    if job["kind"] == "crop" and event.get("outcome") == "operator_stopped":
                                        job["message"] = event.get("message","已取消；未提交的整批未替换旧裁剪集")
                                    if event.get("torque_released") is True:
                                        job["message"] = "已确认从臂力矩关闭；请保持可靠支撑"
                            elif kind == "cameras":
                                job["cameras"] = event["cameras"]
                            elif kind == "saved":
                                job["saved"] = event["total"]
                                job.update({k:v for k,v in event.items() if k.startswith('group_')})
                        if kind != "frame":
                            log.write(json.dumps({"type": kind, **event}, ensure_ascii=False) + "\n")
                    except Exception as exc:
                        self.log("warning", f"无法解析后台状态：{exc}")
                else:
                    log.write(line)
                    if line.strip():
                        self.log("detail", line.strip())
                log.flush()
        code = proc.wait()
        with self.lock:
            if not job.get("finished"):
                job.update(phase="ERROR", message=f"后台意外退出（{code}），不能据此确认保持状态", finished=True)
            job["exit_code"] = code
            job["ended"] = time.time()
            if job.get('feedback_root'):
                _,feedback=feedback_store.manifest(self.config,job['feedback_root'])
                if feedback['state'] in ('preparing','recording','finalizing'):
                    feedback_store.update(job['feedback_root'],state='incomplete',runtime_outcome='worker_exit_without_complete_capture',exit_code=code)
            atomic_json(out / "host_summary.json", job)
            self.log("info" if code == 0 else "error", f"工作进程已退出，代码 {code}；设备状态以最终反馈为准")

    def command(self, request):
        allowed = {"stop", "stop_preview", "stop_device", "scene", "start", "advance", "hold", "release", "jog", "reset", "joint_target", "pause_capture", "resume_capture"}
        name = request.get("command")
        if name not in allowed:
            raise ValueError("未知控制命令")
        if name in ("scene", "start", "hold", "release", "jog", "reset", "joint_target", "resume_capture") and request.get("confirmed") is not True:
            raise ValueError("该操作需要界面中的明确确认")
        if self.offline and name not in ("stop", "stop_preview", "stop_device"):
            raise PermissionError("离线验收不接受设备控制命令")
        if name not in ('stop','stop_preview','stop_device') and self.deployment.changed(force=True):raise RuntimeError('程序文件已更新；先正常停止并重开，不继续下发新运动')
        with self.lock:
            if name in ("stop", "stop_preview", "stop_device"):
                selected = self.preview_job if name == "stop_preview" else self.job if name == "stop_device" or self.active() else self.preview_job
                running = self.preview_active() if name == "stop_preview" else self.active() if name == "stop_device" else self.resources_active()
                if not running:
                    raise RuntimeError("没有运行中的会话")
                if request.get("job_id") != selected["id"]:
                    raise ValueError("会话已变化，请刷新状态，不重发旧命令")
                targets = [(self.preview_proc,self.preview_job)] if name == "stop_preview" else [(self.proc,self.job)] if name == "stop_device" else [
                    (self.proc,self.job),(self.preview_proc,self.preview_job)]
                errors=[]
                with self.write_lock:
                    for proc, job in targets:
                        if proc is None or proc.poll() is not None:
                            continue
                        try:
                            proc.stdin.write(json.dumps({"command":"stop","job_id":job["id"]})+"\n")
                            proc.stdin.flush()
                            job.update(stopping=True,phase="STOPPING",message="已请求停止，等待设备正常释放；不自动卸力")
                        except (OSError,ValueError) as exc:
                            errors.append(str(exc))
                self.log("info", "已请求："+name+"；等待对应会话正常退出，不自动释放力矩")
                if errors:raise RuntimeError("部分停止请求未确认："+"；".join(errors))
                return
            if not self.active():
                raise RuntimeError("没有运行中的设备会话")
            if request.get("job_id") != self.job["id"]:
                raise ValueError("会话已变化，请刷新状态，不重发旧命令")
            if name=='joint_target':
                if self.job['kind']!='control' or self.job['phase']!='CONTROL' or self.job.get('target_pending'):
                    raise ValueError('关节目标仅用于空闲基本控制会话；不与遥操、采集、复位或 ACT 并发')
                calibration=json.loads(Path(self.config['calibration']).read_text(encoding='utf-8'))
                request={**request,'joints':joint_control.validate_request(request,calibration,self.config)}
            if name == "advance" and request.get("phase") not in ("RECORDING", "RESET"):
                raise ValueError("只有录制/复位阶段可以提前结束")
            if name in ('pause_capture','resume_capture'):
                required=('RECORDING','RESET') if name=='pause_capture' else ('PAUSED',)
                if self.job['kind']!='record' or self.job['phase'] not in required:
                    raise ValueError('当前采集阶段不接受暂停或继续')
                if name=='pause_capture' and request.get('phase')!=self.job['phase']:
                    raise ValueError('阶段已变化，请重试暂停')
            with self.write_lock:
                self.proc.stdin.write(json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n")
                self.proc.stdin.flush()
            if name=='joint_target':
                self.job['target_pending']=True;self.job['target_result']=None
            if name in ('pause_capture','resume_capture'):
                self.job.update(phase='PAUSING' if name=='pause_capture' else 'RESUMING',deadline=None,
                    message='正在暂停并保存片段' if name=='pause_capture' else '正在缓慢追平主臂，尚未录制')
            self.log("info", "已请求：" + name + ("；等待后台确认停止" if name == "stop" else ""))

    def save_manual_limits(self,request):
        with self.lock:
            if self.active():raise RuntimeError('请先结束机械臂或任务会话；独立相机预览可以保留')
            if self.deployment.changed(force=True):raise RuntimeError('源码已更新，请从正式入口重开')
            calibration=json.loads(Path(self.config['calibration']).read_text(encoding='utf-8'))
            cfg=validate_config(software_limits.settings_for_request(self.config,calibration,request))
            # Backup the application configuration only. No calibration file or device is touched.
            backup=HERE/'state/manual_limits_history'/('before_'+str(time.time_ns())+'.json')
            atomic_json(backup,self.config)
            atomic_json(HERE/'state/settings.json',cfg)
            self.config=cfg
        self.log('info','仅保存基本控制软件限位；原标定、舵机参数、采集与 ACT 边界不变')
        return {'profile':joint_control.profile(cfg),'config':cfg,'hardware_accessed':False,'backup':str(backup)}

    def catalog(self, root, episode=None):
        root = robot_path(root, existing=True)
        key = str(root), episode, (root / "meta/info.json").stat().st_mtime_ns
        if key in self.catalog_cache:
            return self.catalog_cache[key]
        process = subprocess.run([self.config["python"], "-s", "-B", "-X", "utf8",
            str(HERE / "data_tools.py"), "--root", str(root)] + ([] if episode is None else ["--episode", str(episode)]),
            capture_output=True, text=True, encoding="utf-8", timeout=40, env=environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if process.returncode:
            raise ValueError(process.stderr[-1600:] or "读取数据集失败")
        result = json.loads(process.stdout)
        for ep in result["episodes"]:
            for video in ep["videos"].values():
                video["url"] = "/media?root=" + quote(str(root), safe="") + "&file=" + quote(video["path"], safe="") + '&revision=' + str(key[2])
        if result.get("selected"):
            result["selected"] = next(e for e in result["episodes"] if e["index"] == episode)
        result["annotations"] = self.annotations(root)
        result["naming"] = describe(root)
        result['crop_collection']=read_collection(root)
        result["crop_revision"]=review_revision(root)
        if result['crop_collection']:
            mapping={row['episode_index']:row for row in result['crop_collection']['episodes']}
            for ep in result['episodes']:
                ep['source_episode']=mapping[ep['index']]['source_episode']
                ep['crop_range']=mapping[ep['index']]['requested_seconds']
        if len(self.catalog_cache) >= 3:
            self.catalog_cache.pop(next(iter(self.catalog_cache)))
        self.catalog_cache[key] = result
        return result

    def annotations(self, root):
        return read_reviews(root)

    def manage_dataset(self, request):
        with self.lock:
            if self.resources_active():
                raise RuntimeError("请先正常结束当前任务，再管理整个数据集文件夹")
            action = request.get("action")
            if action == "create":
                spec=validate_spec(request.get("spec"))
                if spec["domain"]!="real" or spec["stage"]!="raw" or spec["review"]!="pending":
                    raise ValueError("新建采集文件夹固定为 real/raw/待审核；处理版请从原始数据导出")
                planned=preview_name(self.config,spec)
                if planned["exists"]:
                    raise ValueError("此规范名称或目录已存在，请更换批次/版本；不会覆盖")
                result = create_dataset(self.config, planned["name"])
                result["naming"]=store_metadata(result["root"],spec,source="new_capture_folder")
            elif action == "label":
                root=child_folder(self.config,request.get("root",""))
                if not (root/"meta/info.json").is_file() and not (root/".platform-plan.json").is_file():
                    raise ValueError("只能给已识别的数据集设置名称与分类")
                spec=validate_spec(request.get("spec"))
                if request.get("confirmed") is not True:
                    raise ValueError("请确认仅修改名称/分类/备注，不执行处理或改变训练数据")
                from capture_groups import group_context
                context=group_context(root)
                if context and spec.get('instruction')!=context['instruction']:raise ValueError('小组指令固定，不能通过改显示名称改变；描述与备注可单独修改')
                if spec["review"]=="train":
                    info=root/"meta/info.json"
                    if not info.is_file() or json.loads(info.read_text(encoding="utf-8")).get("total_frames",0)<=0:
                        raise ValueError("空采集文件夹不能标为可训练")
                result={"root":str(root),"naming":store_metadata(root,spec,review_confirmed=request.get("review_confirmed") is True),
                        "physical_folder_renamed":False,"data_modified":False}
            elif action == "archive":
                result = archive_dataset(self.config, request.get("root", ""), request.get("confirmed_name"))
            elif action == "restore":
                result = restore_dataset(self.config, request.get("manifest", ""))
            else:
                raise ValueError("未知数据集管理操作")
            self.catalog_cache.clear()
            self.log("info", "数据集管理：" + action)
            return result


class Handler(BaseHTTPRequestHandler):
    server_version = "SO101/0.1"

    @property
    def app(self):
        return self.server.app

    def log_message(self, *_):
        pass

    def secure(self, mutation=False,allow_stale_stop=False):
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected:
            raise PermissionError("仅接受本机地址")
        if mutation and (self.headers.get("X-SO101-Token") != self.app.token or
                         self.headers.get("Origin") != "http://" + expected):
            raise PermissionError("请求来源或令牌无效，请从本机平台页面操作")
        if mutation and not allow_stale_stop and self.headers.get('X-SO101-Build') and self.headers.get('X-SO101-Build')!=self.app.deployment.build:
            raise PermissionError('界面构建与后台不同，拒绝旧版本请求；请重开正式入口')

    def send(self, status, raw, mime="application/json; charset=utf-8", **headers):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; media-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def json(self, value, status=200):
        self.send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.secure()
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/api/state":
                return self.json(self.app.snapshot())
            if url.path == '/api/identity':
                return self.json(self.app.deployment.identity())
            if url.path == "/api/naming-options":
                return self.json({"domains":DOMAINS,"tasks":TASKS,"purposes":PURPOSES,"stages":STAGES,"reviews":REVIEWS,"defaults":defaults(),
                    "simple_defaults":simple_defaults(self.app.config['task']),
                    "pattern":"so101_来源_raw或proc_系统时间到分钟_语言指令_场景描述"})
            if url.path == "/api/reset-profile":
                return self.json(inspect_pose(self.app.config, query.get("path")))
            if url.path == '/api/joint-control-profile':
                return self.json(joint_control.profile(self.app.config))
            if url.path == "/api/ports":
                ports=self.app.inventory(ports=True)
                for port in ports:
                    port['role']=next((role for role in ('leader','follower') if port['serial'] and port['serial']==self.app.config[role+'_usb_serial']),'其他')
                return self.json(ports)
            if url.path == '/api/environment':
                return self.json(self.app.inventory())
            if url.path == "/api/datasets":
                return self.json(list_datasets(self.app.config))
            if url.path == '/api/capture-collections':
                from capture_groups import collections
                return self.json(collections(self.app.config))
            if url.path == '/api/feedback':
                return self.json(feedback_store.history(self.app.config))
            if url.path == "/api/archives":
                return self.json(list_archives())
            if url.path == "/api/dataset":
                ep = int(query["episode"]) if "episode" in query else None
                return self.json(self.app.catalog(query["root"], ep))
            if url.path == '/api/capture-review':
                from capture_pause import review_catalog
                with self.app.lock:return self.json(review_catalog(self.app,query.get('job_id')))
            if url.path == '/api/crop-plan':
                episode=number(query.get('episode',-1),0,100000,'episode',True)
                return self.json(plan_crop(self.app.config,query['root'],episode))
            if url.path == '/api/motion-trim-plan':
                from motion_trim import read_preview
                with self.app.lock:
                    plan = read_preview(self.app.job,query.get("job_id"))
                return self.json({key:value for key,value in plan.items() if key != "evidence"})
            if url.path.startswith("/frame/"):
                name = url.path.rsplit("/", 1)[-1]
                with self.app.lock:
                    frame = self.app.frames.get(name)
                    if query.get("session") and query["session"] != self.app.frame_owner:
                        frame = None
                if frame:
                    return self.send(200, frame[0], "image/jpeg")
                return self.send(204, b"", "image/jpeg")
            if url.path == "/media":
                root = robot_path(query["root"], existing=True)
                file = (root / query["file"]).resolve()
                if not (root / "meta/info.json").is_file() or not file.is_relative_to(root) or file.suffix.lower() != ".mp4":
                    raise PermissionError("只允许读取所选数据集的视频")
                return self.media(file)
            assets = {"/": "index.html", "/app.js": "app.js", "/capture_pause.js":"capture_pause.js", "/dataset_batch.js":"dataset_batch.js", "/dataset_browser.js":"dataset_browser.js", "/capture_groups.js":"capture_groups.js", "/naming.js": "naming.js", "/history.js":"history.js", "/joint_control.js":"joint_control.js", "/portable.js":"portable.js", "/style.css": "style.css", "/layout.css": "layout.css", "/manual_limits.css":"manual_limits.css"}
            if url.path not in assets:
                return self.json({"error": "未找到"}, 404)
            file = HERE / "dist" / assets[url.path]
            if query.get('build') and query['build']!=self.app.deployment.build:
                raise PermissionError('资源版本与当前后台不同，请关闭旧窗口后重新打开正式入口')
            raw = self.app.deployment.assets[assets[url.path]]
            if url.path == "/":
                raw = raw.replace(b"__SO101_TOKEN__", self.app.token.encode("ascii"))
                raw = raw.replace(b'__SO101_BUILD__',self.app.deployment.build.encode('ascii'))
            return self.send(200, raw, mimetypes.guess_type(file)[0] + "; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            self.json({"error": str(exc)}, 403 if isinstance(exc, PermissionError) else 400)

    def media(self, file):
        size = file.stat().st_size
        start, end, status = 0, size - 1, 200
        requested = self.headers.get("Range")
        if requested:
            try:
                if not requested.startswith("bytes=") or "," in requested:
                    raise ValueError()
                left, right = requested[6:].split("-", 1)
                if left:
                    start = int(left)
                    end = min(int(right), size - 1) if right else size - 1
                else:
                    start = max(0, size - int(right))
                if not 0 <= start <= end < size:
                    raise ValueError()
                status = 206
            except ValueError:
                return self.send(416, b"", Content_Range=f"bytes */{size}")
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command != "HEAD":
            with file.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = stream.read(min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    def do_POST(self):
        try:
            self.secure(mutation=True,allow_stale_stop=self.path=='/api/command')
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 32768:
                raise ValueError("请求体大小无效")
            request = json.loads(self.rfile.read(length))
            is_stop=self.path=='/api/command' and request.get('command') in ('stop','stop_preview','stop_device')
            if self.app.restart_required and self.path!='/api/shutdown' and not is_stop:
                raise RuntimeError('配置变更需要重启，当前只允许停止或正常关闭')
            if self.path in ('/api/jobs','/api/hardware-mode','/api/command','/api/manual-limits') and not is_stop and self.headers.get('X-SO101-Build')!=self.app.deployment.build:
                raise PermissionError('缺少匹配的界面构建标识，拒绝混合版本操作；停止请求仍可使用')
            if self.path == "/api/jobs":
                return self.json(self.app.start(request))
            if self.path == '/api/capture-plan':
                return self.json(capture_plan(self.app.config,request))
            if self.path == "/api/dataset-name":
                ignore=child_folder(self.app.config,request["editing_root"]) if request.get("editing_root") else None
                naming_cfg={**self.app.config,'dataset_home':str(ignore.parent)} if ignore else self.app.config
                return self.json(suggest_name(naming_cfg,request.get("spec"),request.get("increment","batch")) if request.get("suggest") is True else preview_name(naming_cfg,request.get("spec"),ignore))
            if self.path == "/api/dataset-management":
                return self.json(self.app.manage_dataset(request))
            if self.path == "/api/hardware-mode":
                with self.app.lock:
                    if self.app.resources_active():
                        raise RuntimeError("请先结束当前任务")
                    if request.get("confirmed") is not True or not isinstance(request.get("offline"), bool):
                        raise ValueError("需要明确确认硬件入口模式")
                    # Unlocking changes UI authorization only. No discovery, device worker or motion.
                    self.app.offline = request["offline"]
                    self.app.log("info", "离线硬件锁定" if self.app.offline else "设备操作入口已解锁；尚未连接任何设备")
                return self.json({"offline": self.app.offline, "hardware_accessed": False})
            if self.path == "/api/command":
                self.app.command(request)
                return self.json({"accepted": True, "note": "已请求，完成状态以后台反馈为准"})
            if self.path == "/api/config":
                return self.json(self.app.save_config(request))
            if self.path == '/api/manual-limits':
                return self.json(self.app.save_manual_limits(request))
            if self.path == "/api/reset-profile":
                with self.app.lock:
                    if self.app.resources_active():
                        raise RuntimeError("先结束当前会话，再另存任务起点")
                    if request.get("confirmed") is not True:
                        raise ValueError("需要确认另存起点；不会读取设备或产生运动")
                    return self.json(save_pose_copy(self.app.config, request.get("positions"), request.get("source_path")))
            if self.path == "/api/annotations/batch":
                root = child_folder(self.app.config,request.get("root",""))
                with self.app.lock:
                    from capture_pause import require_review_idle
                    require_review_idle(self.app)
                    if self.app.active() and self.app.job["kind"] == "crop":
                        raise RuntimeError("请等待当前裁剪或分析事务结束，再修改审核状态")
                    if request.get("crop_revision") != review_revision(root):
                        raise ValueError("裁剪集版本已变化，请重新打开数据集后标记，避免标错原始轨迹")
                    notes = save_reviews(root,request.get("episodes"),request.get("result"))
                    self.app.catalog_cache.clear()
                return self.json({"annotations":notes,"changed":len(request["episodes"])})
            if self.path == '/api/capture-review':
                from capture_pause import review_change
                with self.app.lock:return self.json(review_change(self.app,request))
            if self.path == '/api/episode-restore':
                from review_store import restore_review
                root=child_folder(self.app.config,request.get('root',''))
                with self.app.lock:
                    if self.app.active() and not (self.app.job['kind']=='record' and self.app.job['phase']=='PAUSED'):
                        raise RuntimeError('请先暂停采集或结束当前任务')
                    if request.get('crop_revision')!=review_revision(root):raise ValueError('裁剪集已变化，请重新打开')
                    episode=request.get('episode')
                    if type(episode) is not int:raise ValueError('轨迹索引无效')
                    notes=restore_review(root,episode);self.app.catalog_cache.clear()
                return self.json(notes)
            if self.path == "/api/annotation":
                root = robot_path(request["root"], existing=True)
                index = number(request["episode"], 0, 100000, "episode", True)
                if request.get("result") not in ("success", "failure", "unreviewed", "exclude"):
                    raise ValueError("未知审核标记")
                with self.app.lock:
                    from capture_pause import require_review_idle
                    require_review_idle(self.app)
                    if self.app.active() and self.app.job['kind']=='crop':
                        raise RuntimeError('请等待当前裁剪事务完成，再修改成功/失败状态')
                    if request.get("crop_revision") != review_revision(root):
                        raise ValueError("裁剪集版本已变化，请重新打开数据集后标记")
                    notes=save_review(root,index,request['result'],request.get('note',''))
                    self.app.catalog_cache.clear()
                return self.json(notes)
            if self.path == "/api/result":
                if self.app.active() or not self.app.job or self.app.job["kind"] != "act":
                    raise ValueError("只有已结束的 ACT 会话可以标记结果")
                value = request.get("success")
                if value is not True and value is not False and value is not None:
                    raise ValueError("结果必须为成功、失败或未判断")
                if self.app.job.get('feedback_root'):
                    return self.json(feedback_store.label(self.app.config,self.app.job['feedback_root'],value,
                        request.get('note',''),request.get('failure_reason','')))
                report = Path(self.app.job["output"]) / "act/report.json"
                if not report.exists():
                    raise ValueError("本轮 ACT 尚无完整运行报告")
                atomic_json(report.parent / "operator_result.json", {"task_success": value,
                    "source": "operator_ui", "reported_at": datetime.now().isoformat(), "automatic_video_review": False})
                return self.json({"saved": True})
            if self.path == '/api/feedback/result':
                if self.app.active() and self.app.job.get('feedback_root')==request.get('root'):
                    raise ValueError('当前 ACT 尚未保存完成')
                return self.json(feedback_store.label(self.app.config,request.get('root',''),request.get('success'),request.get('note',''),request.get('failure_reason','')))
            if self.path == "/api/open-folder":
                options = {"project": HERE, "datasets": Path(self.app.config["dataset_home"]),
                           "logs": Path(self.app.config["log_home"]),"feedback":Path(self.app.config['feedback_home'])}
                target = options.get(request.get("target"))
                if target is None or not target.is_dir():
                    raise ValueError("该目录暂不存在")
                os.startfile(str(target))
                return self.json({"opened": str(target)})
            if self.path == "/api/shutdown":
                if self.app.resources_active():
                    raise RuntimeError("设备/导出会话仍在运行，请先正常停止")
                self.json({"closing": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            return self.json({"error": "未找到"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            self.json({"error": str(exc)}, 403 if isinstance(exc, PermissionError) else 409 if isinstance(exc, RuntimeError) else 400)


def main():
    environment()
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--offline", action="store_true", help="Lock every hardware entry; dataset review/crop remain available")
    args = parser.parse_args()
    if not sys.flags.no_user_site:
        raise SystemExit("请通过 launch.cmd 或使用隔离环境的 Python 加 -s 参数启动")
    app = Platform(offline=args.offline)
    if args.check:
        print(json.dumps({"configuration": "passed", "hardware_accessed": False,
                          "python": sys.executable, "no_extra_dependencies": True}, ensure_ascii=False))
        return
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port if args.port is not None else app.config['server_port']), Handler)
    except OSError:
        raise SystemExit(f"端口 {args.port} 已使用；若平台已打开请回到原窗口，不重复启动")
    server.app = app
    url = f"http://127.0.0.1:{server.server_port}/"
    print("SO101 数采平台 · " + url + "\n未连接硬件；关闭前请先在界面停止设备会话。", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        if app.resources_active():
            selected=app.job if app.active() else app.preview_job
            app.command({"command": "stop", "job_id": selected["id"]})
            print("等待工作进程正常收尾；不要强杀硬件进程。", flush=True)
            for proc in (app.proc,app.preview_proc):
                if proc is not None:proc.wait()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
