"""Native desktop host using the installed system WebView2, never bundled Chromium."""
from __future__ import annotations

import argparse
import ctypes
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import traceback
from urllib.request import ProxyHandler, Request, build_opener

from common import HERE, atomic_json, environment, load_config, robot_path
from app_identity import Deployment,VERSION


def choose_start_pose_file(window, open_dialog, current_path, fallback_path):
    """Native file picker only; reading/coordinate validation stays in the backend."""
    try:
        initial = robot_path(current_path, existing=True)
    except (ValueError, OSError):
        initial = robot_path(fallback_path, existing=True)
    directory = initial if initial.is_dir() else initial.parent
    result = window.create_file_dialog(open_dialog, directory=str(directory),
        allow_multiple=False, file_types=("任务起点 JSON (*.json)",))
    if not result:
        return None
    selected = robot_path(result[0], existing=True)
    if not selected.is_file() or selected.suffix.lower() != ".json":
        raise ValueError("请选择 E:\\robot 内的任务起点 JSON 文件，不是文件夹或标定文件")
    return str(selected)


def main():
    environment()
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="Disable all hardware entry points")
    parser.add_argument("--smoke-test", action="store_true", help="Offline DOM integration checks, no hardware")
    parser.add_argument("--camera-ui-test", action="store_true", help="Offline camera/monitor coexistence and JPEG/blob rendering checks")
    parser.add_argument('--launch-nonce',default='',help='Local desktop launch receipt; no device authorization')
    args = parser.parse_args()
    offline = args.offline or args.smoke_test or args.camera_ui_test
    if not sys.flags.no_user_site:
        raise RuntimeError("请使用 launch.cmd 启动隔离环境")
    cfg = load_config()
    if HERE.resolve()!=Path('E:/robot/projects/so101_data_platform').resolve():
        raise RuntimeError('此目录不是正式项目。请使用 E:\\robot\\SO101数采平台.cmd，不启动源码副本')
    if args.launch_nonce and not re.fullmatch(r'[a-f0-9]{32}',args.launch_nonce):raise ValueError('启动标识无效')
    deployment=Deployment()
    host_identity=deployment.identity()
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    mutex = kernel.CreateMutexW(None, False, "Local\\SO101_DataPlatformDesktop")
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(mutex)
        ctypes.windll.user32.MessageBoxW(None, "SO101 数采平台已经打开。请回到原窗口，不重复启动。", "SO101 数采平台", 0x40)
        return
    state_dir = HERE / "state"
    state_dir.mkdir(exist_ok=True)
    url = "http://127.0.0.1:8791/"
    proc = None
    output = (state_dir / "desktop.log").open("a", encoding="utf-8")
    token = None
    loopback = build_opener(ProxyHandler({}))  # Local control/status must never go through a system proxy.

    def request(path, value=None):
        if value is None:
            with loopback.open(url + path, timeout=3) as response:
                return json.load(response)
        data = json.dumps(value).encode("utf-8")
        req = Request(url + path, data=data, headers={"Content-Type": "application/json",
            "X-SO101-Token": token, "Origin": url.rstrip("/"),'X-SO101-Build':deployment.build})
        with loopback.open(req, timeout=3) as response:
            return json.load(response)

    try:
        # No re-use of an unverified existing server at this port.
        import socket
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", 8791)) == 0:
                raise RuntimeError("端口 8791 已占用。请关闭旧平台的后台，或检查 desktop.log；不会连接未知服务。")
        proc = subprocess.Popen([cfg["python"], "-s", "-B", "-u", "-X", "utf8", str(HERE / "server.py"),
            "--no-browser"] + (["--offline"] if offline else []), cwd=HERE, env=environment(),
            stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 15
        while True:
            if proc.poll() is not None:
                raise RuntimeError("本地后台启动失败，请查看 state/desktop.log")
            try:
                snapshot = request("api/state")
                if snapshot.get("project") != str(HERE) or bool(snapshot.get("offline")) != offline:
                    raise RuntimeError("后台身份或运行模式不符")
                if snapshot.get('identity',{}).get('build')!=deployment.build:
                    raise RuntimeError('原生窗口与后台构建不同，拒绝打开混合版本')
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("本地后台启动超时")
                time.sleep(.1)
        with loopback.open(url, timeout=3) as response:
            html = response.read().decode("utf-8")
        match = re.search(r'name="so101-token" content="([^"]+)"', html)
        if not match:
            raise RuntimeError("本地页面缺少会话令牌")
        token = match.group(1)

        import webview
        window = None

        class NativeAPI:
            def get_identity(self):
                return {**host_identity,'backend_instance':snapshot['identity']['instance'],
                    'launch_nonce':args.launch_nonce,'python':sys.executable}
            def choose_folder(self, current_path=""):
                try:
                    initial=robot_path(current_path,existing=True)
                    if not initial.is_dir():initial=initial.parent
                except (ValueError,OSError):
                    initial=Path(cfg['dataset_home'])
                result = window.create_file_dialog(webview.FileDialog.FOLDER, directory=str(initial))
                return str(result[0]) if result else None

            def choose_start_pose(self, current_path=""):
                return choose_start_pose_file(window, webview.FileDialog.OPEN,
                    current_path, cfg["reference_start_pose"])

            def close_window(self):
                if request("api/state").get("resources_active"):
                    return {"closed": False, "reason": "请先等待当前任务正常结束"}
                window.destroy()
                return {"closed": True}

        window = webview.create_window(f"SO101 数采平台 · v{VERSION} · {deployment.build}" + (" · 离线" if offline else ""), url,
            js_api=NativeAPI(), width=1440, height=940, min_size=(1024, 720), text_select=True,
            background_color="#f4f6f8")

        def closing():
            try:
                if request("api/state").get("resources_active"):
                    threading.Thread(target=lambda: window.evaluate_js("window.platformRequestClose()"), daemon=True).start()
                    return False
            except OSError:
                # Do not destroy the UI while a worker's state is unknown.
                ctypes.windll.user32.MessageBoxW(None, "无法确认后台状态。请检查机械臂及后台，不要把关闭窗口当作已停止。", "SO101 数采平台", 0x30)
                return False
            return True

        window.events.closing += closing

        def smoke():
            window.events.loaded.wait(15)
            checks = {"hardware_accessed": False, "renderer": "edgechromium"}
            try:
                import hashlib
                from unittest.mock import patch
                originals = {p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
                    for p in (cfg['reference_start_pose'],cfg['calibration'])}
                deadline = time.monotonic() + 15
                while not window.evaluate_js("Boolean(window.platformRequestClose && document.getElementById('connection-state').textContent.includes('正常'))"):
                    if time.monotonic() > deadline:
                        raise TimeoutError("前端未就绪")
                    time.sleep(.2)
                checks["initial"] = window.evaluate_js("({title:document.title,hardwareLocked:document.getElementById('offline-notice').classList.contains('hidden')===false,disabled:document.getElementById('prepare-record').disabled,overflow:document.documentElement.scrollWidth>innerWidth,bodyText:document.body.innerText.slice(0,600)})")
                checks["javascript_errors"] = window.evaluate_js("window.__qaErrors || []")
                checks['build_identity']=window.evaluate_js("({html:htmlBuild,backend:state.identity.build,native:nativeRuntimeIdentity.build,instance:state.identity.instance,formal:state.identity.formal_entry,mismatch:runtimeMismatch})")
                window.evaluate_js("setView('control');true")
                deadline=time.monotonic()+10
                while not window.evaluate_js('Boolean(jointControlProfile)'):
                    if time.monotonic()>deadline:raise TimeoutError('关节目标配置未就绪')
                    time.sleep(.1)
                checks['joint_target_panel']=window.evaluate_js("({rows:document.getElementById('joint-target-rows').children.length,visible:!document.getElementById('joint-control-panel').classList.contains('hidden'),executeDisabled:document.getElementById('joint-execute').disabled,stopDisabled:document.getElementById('joint-stop').disabled,selected:document.querySelectorAll('#joint-target-rows input[type=checkbox]:checked').length})")
                checks['joint_target_plan']=window.evaluate_js("(()=>{document.getElementById('target-check-0').checked=true;document.getElementById('target-position-0').value='1.25';document.getElementById('target-speed-0').value='2';var single=jointTargetPlan();document.getElementById('target-check-5').checked=true;document.getElementById('target-position-5').value='20';document.getElementById('target-speed-5').value='10';var multiple=jointTargetPlan();document.getElementById('target-position-0').value='';var emptyRejected=false;try{jointTargetPlan()}catch{emptyRejected=true}return {single,multiple,emptyRejected,active:state.active}})()")
                window.evaluate_js("document.getElementById('joint-select-none').click();for(var i=0;i<6;i++)document.getElementById('target-position-'+i).value='';setView('capture');true")
                deadline = time.monotonic()+10
                while not window.evaluate_js("Boolean(document.querySelector('#record-name-form .naming-preview')?.textContent.startsWith('so101_') && document.getElementById('record-root').value)"):
                    if time.monotonic()>deadline:
                        raise TimeoutError("采集规范命名表单未就绪")
                    time.sleep(.1)
                checks["capture_naming"] = window.evaluate_js("({readonly:document.getElementById('record-root').readOnly,path:document.getElementById('record-root').value,fixed:document.querySelector('#record-name-form .naming-fixed-tags').textContent})")
                checks['simple_workflow']=window.evaluate_js("({modes:Array.from(document.getElementById('record-target-mode').options).map(o=>o.textContent),manualBatch:document.querySelectorAll('[data-name=batch],[data-name=version],[data-name=date],[data-name=purpose]').length,parent:document.getElementById('record-parent').value})")
                with patch.object(window,'create_file_dialog',return_value=(cfg['dataset_home'],)) as folder_dialog:
                    window.evaluate_js("document.getElementById('record-parent-browse').click();true")
                    deadline=time.monotonic()+10
                    while folder_dialog.call_count<1:
                        if time.monotonic()>deadline:raise TimeoutError('数据集父目录选择按钮未调用原生桥接')
                        time.sleep(.1)
                    checks['simple_workflow']['folder_picker']=folder_dialog.call_count==1
                window.evaluate_js("document.getElementById('record-group').value='娃娃位置变化';document.getElementById('record-group').dispatchEvent(new Event('input'));true")
                deadline=time.monotonic()+10
                while not window.evaluate_js("document.getElementById('record-root').value.includes('娃娃位置变化')"):
                    if time.monotonic()>deadline:raise TimeoutError('场景描述没有更新名称')
                    time.sleep(.1)
                checks['simple_workflow']['language_in_name']=window.evaluate_js("document.getElementById('record-root').value.includes(document.getElementById('record-task').value)")
                window.evaluate_js("document.getElementById('record-target-mode').value='existing';recordTargetChanged();true")
                checks['simple_workflow']['append_mode']=window.evaluate_js("document.getElementById('record-parent-label').hidden&&!document.getElementById('record-existing').classList.contains('hidden')")
                window.evaluate_js("document.getElementById('record-target-mode').value='new';document.getElementById('record-group').value='';recordTargetChanged();handle(recordNameForm.update());true")
                time.sleep(.3)
                checks['capture_naming']['path']=window.evaluate_js("document.getElementById('record-root').value")
                window.evaluate_js("var note=document.querySelector('#record-name-form textarea');note.value='命名验收：中文 / 位置变化';note.dispatchEvent(new Event('input',{bubbles:true}));true")
                time.sleep(.4)
                checks["note_does_not_change_path"] = window.evaluate_js("document.getElementById('record-root').value") == checks["capture_naming"]["path"]
                window.evaluate_js("var note=document.querySelector('#record-name-form textarea');note.value='';note.dispatchEvent(new Event('input',{bubbles:true}));true")
                window.evaluate_js("document.querySelector('[data-view=\"settings\"]').click()")
                deadline = time.monotonic()+10
                while not window.evaluate_js("Boolean(document.getElementById('reset-pose-0').value)"):
                    if time.monotonic()>deadline:
                        raise TimeoutError("任务起点配置未就绪")
                    time.sleep(.1)
                checks["reset_settings"] = window.evaluate_js("({rows:document.getElementById('reset-profile-rows').children.length,base:document.getElementById('cfg-reset-speed').value,gripper:document.getElementById('cfg-reset-gripper-speed').value,rates:Array.from(document.querySelectorAll('[id^=reset-rate-]')).map(e=>e.textContent),reference:document.getElementById('cfg-reference').value,source:document.getElementById('reset-source').textContent,errors:window.__qaErrors||[]})")
                checks["pose_precision"] = window.evaluate_js("({display:Array.from(document.querySelectorAll('[id^=reset-pose-][type=number]')).map(e=>e.value),positions:resetPosePositions(),picker:Boolean(document.getElementById('browse-start-pose')),bridge:typeof window.pywebview?.api?.choose_start_pose==='function'})")
                original_positions = json.loads(Path(cfg['reference_start_pose']).read_text(encoding='utf-8'))['normalized_position']
                checks["pose_precision"]["preserved"] = checks['pose_precision']['positions'] == original_positions
                checks["pose_editing"] = window.evaluate_js("(()=>{var i=document.getElementById('reset-pose-0');i.value='1.25';i.dispatchEvent(new Event('input'));var p=resetPosePositions(),unsavedBlocked=false,emptyBlocked=false;try{requireSavedResetPose()}catch{unsavedBlocked=true}i.value='';i.dispatchEvent(new Event('input'));try{resetPosePositions()}catch{emptyBlocked=true}return {positions:p,unsavedBlocked,emptyBlocked}})()")
                # Exercise the actual button -> JS bridge -> native chooser -> validated
                # profile path, with only the OS modal result replaced by a fake selection.
                with patch.object(window,'create_file_dialog',return_value=(cfg['reference_start_pose'],)) as chooser:
                    window.evaluate_js("document.getElementById('browse-start-pose').click();true")
                    deadline=time.monotonic()+10
                    while not window.evaluate_js("Boolean(document.getElementById('reset-pose-0').value && document.getElementById('toast').textContent.startsWith('起点已读取并核对'))"):
                        if time.monotonic()>deadline:
                            raise TimeoutError('起点文件选择按钮/原生桥接未完成')
                        time.sleep(.1)
                    checks['pose_picker']={'calls':chooser.call_count,'json_filter':chooser.call_args.kwargs['file_types']==('任务起点 JSON (*.json)',),
                        'restored':window.evaluate_js('resetPosePositions()')==original_positions}
                window.evaluate_js("document.querySelector('[data-view=\"datasets\"]').click()")
                time.sleep(.8)
                checks["datasets"] = window.evaluate_js("({visible:!document.getElementById('datasets-page').classList.contains('hidden'),options:document.getElementById('dataset-select').options.length})")
                window.evaluate_js("var s=document.getElementById('dataset-select');s.value=Array.from(s.options).find(o=>o.value.replaceAll('\\\\','/').endsWith('/so101_supplement_20261015')).value;s.dispatchEvent(new Event('change'));true")
                deadline = time.monotonic()+25
                while not window.evaluate_js("document.getElementById('review-top').readyState>=2 && document.getElementById('episode-list').children.length===30"):
                    if time.monotonic()>deadline:
                        raise TimeoutError("已有数据集视频未就绪")
                    time.sleep(.2)
                checks["episode_viewer"] = window.evaluate_js("({episodes:document.getElementById('episode-list').children.length,topWidth:document.getElementById('review-top').videoWidth,wristWidth:document.getElementById('review-wrist').videoWidth,plotWidth:document.getElementById('trajectory-chart').width})")
                checks["extensions"] = window.evaluate_js("({jointCheckboxes:document.querySelectorAll('#joint-checkboxes input[type=checkbox]').length,allChecked:document.querySelectorAll('#joint-checkboxes input:checked').length,metrics:Array.from(document.getElementById('plot-metric').options).map(o=>o.textContent),task:document.getElementById('episode-task').textContent,protectedDeleteDisabled:document.getElementById('archive-dataset').disabled,modeButton:document.getElementById('hardware-mode').textContent})")
                checks["dataset_naming"] = window.evaluate_js("({options:Array.from(document.getElementById('dataset-select').options).map(o=>o.textContent),tags:document.getElementById('dataset-tags').innerText,detail:document.getElementById('dataset-name-detail').innerText,editor:Boolean(document.querySelector('#edit-name-form textarea')),readonlyCrop:document.getElementById('crop-output').readOnly})")
                window.evaluate_js("document.getElementById('plot-metric').value='observation.sensors.temperature';document.getElementById('plot-metric').dispatchEvent(new Event('change'));true")
                time.sleep(.2)
                checks["temperature_view"] = window.evaluate_js("({hint:document.getElementById('signal-hint').textContent,values:document.getElementById('signal-values').innerText,errors:window.__qaErrors||[]})")
                window.evaluate_js("document.getElementById('episode-list').lastElementChild.click()")
                deadline = time.monotonic()+20
                while not window.evaluate_js("document.getElementById('episode-title').textContent.startsWith('Episode 30') && document.getElementById('review-top').readyState>=2"):
                    if time.monotonic()>deadline:
                        raise TimeoutError("合并视频中的最后一个 episode 未就绪")
                    time.sleep(.2)
                checks["last_episode"] = window.evaluate_js("({title:document.getElementById('episode-title').textContent,videoTime:document.getElementById('review-top').currentTime,videoWidth:document.getElementById('review-top').videoWidth,errors:window.__qaErrors||[]})")
                window.evaluate_js("document.getElementById('play-pause').click()")
                time.sleep(1)
                checks["playback"] = window.evaluate_js("({paused:document.getElementById('review-top').paused,timeLabel:document.getElementById('review-time').textContent,errors:window.__qaErrors||[]})")
                window.evaluate_js("document.getElementById('play-pause').click(); document.querySelector('[data-view=\"capture\"]').click()")
                window.evaluate_js("setView('datasets');handle(selectEpisode(26));true")
                deadline=time.monotonic()+20
                while not window.evaluate_js("Boolean(selectedEpisode?.index===26 && cropPlan)"):
                    if time.monotonic()>deadline:raise TimeoutError('失败轨迹裁剪门禁未就绪')
                    time.sleep(.1)
                checks['crop_failure_gate']=window.evaluate_js("({result:library.annotations[26].result,disabled:document.getElementById('export-crop').disabled,canCrop:cropPlan.can_crop})")
                window.evaluate_js("handle(selectEpisode(0));true")
                deadline=time.monotonic()+20
                while not window.evaluate_js("Boolean(selectedEpisode?.index===0 && cropPlan?.replacing)"):
                    if time.monotonic()>deadline:raise TimeoutError('已有裁剪的覆盖计划未就绪')
                    time.sleep(.1)
                checks['crop_upsert_ui']=window.evaluate_js("({replacing:cropPlan.replacing,total:cropPlan.total_crops,disabled:document.getElementById('export-crop').disabled,button:document.getElementById('export-crop').textContent,start:document.getElementById('crop-start').value,end:document.getElementById('crop-end').value,destination:cropPlan.destination})")
                window.evaluate_js("document.getElementById('crop-browse').click();true")
                deadline=time.monotonic()+25
                while not window.evaluate_js("Boolean(library?.crop_collection && selectedEpisode && document.getElementById('review-top').readyState>=2)"):
                    if time.monotonic()>deadline:raise TimeoutError('统一裁剪集合的视频未就绪')
                    time.sleep(.1)
                checks['crop_collection_ui']=window.evaluate_js("({count:library.total_episodes,success:Object.values(library.annotations).filter(a=>a.result==='success').length,inherited:Object.values(library.annotations).every(a=>a.inherited),title:document.getElementById('episode-title').textContent,disabled:document.getElementById('export-crop').disabled,sourceButton:!document.getElementById('open-crop-source').classList.contains('hidden'),topWidth:document.getElementById('review-top').videoWidth,wristWidth:document.getElementById('review-wrist').videoWidth,errors:window.__qaErrors||[]})")
                window.evaluate_js("document.getElementById('open-crop-source').click();true")
                deadline=time.monotonic()+20
                while not window.evaluate_js("Boolean(!library?.crop_collection && selectedEpisode?.index===0 && cropPlan?.replacing)"):
                    if time.monotonic()>deadline:raise TimeoutError('未能返回原始轨迹调整裁剪')
                    time.sleep(.1)
                checks['crop_return_source']=True
                window.evaluate_js("setView('capture');true")
                checks["layout"] = window.evaluate_js("({camerasSameRow:Math.abs(document.querySelector('[data-camera=\"top\"]').getBoundingClientRect().top-document.querySelector('[data-camera=\"wrist\"]').getBoundingClientRect().top)<2,inspectorWidth:document.querySelector('.inspector').getBoundingClientRect().width})")
                window.evaluate_js("document.querySelector('[data-view=\"feedback\"]').click();true")
                time.sleep(.3)
                checks['feedback_history']=window.evaluate_js("({visible:!document.getElementById('feedback-page').classList.contains('hidden'),summary:document.getElementById('feedback-summary').textContent,hasTask:Boolean(document.getElementById('act-task')),errors:window.__qaErrors||[]})")
                integration=state_dir/'feedback_integration_report.json'
                if integration.is_file():
                    qa=json.loads(integration.read_text(encoding='utf-8'))
                    entry=json.loads((Path(qa['root'])/'manifest.json').read_text(encoding='utf-8'));entry['has_dataset']=True
                    window.evaluate_js('feedbackItems='+json.dumps([entry],ensure_ascii=False)+';selectedFeedback=feedbackItems[0];renderFeedback();renderFeedbackDetail();true')
                    window.evaluate_js("document.querySelector('#feedback-detail button').click();true")
                    deadline=time.monotonic()+20
                    while not window.evaluate_js("document.getElementById('episode-title').textContent.startsWith('Episode 1 ')&&document.getElementById('review-top').readyState>=2"):
                        if time.monotonic()>deadline:raise TimeoutError('回流数据查看器未就绪')
                        time.sleep(.1)
                    checks['feedback_viewer']=window.evaluate_js("({videoWidth:document.getElementById('review-top').videoWidth,sensors:Array.from(document.getElementById('plot-metric').options).map(o=>o.value),errors:window.__qaErrors||[]})")
                    window.evaluate_js('handle(refreshDatasets());handle(refreshFeedback());true')
                window.evaluate_js("document.querySelector('[data-view=\"datasets\"]').click()")
                checks["passed"] = checks["initial"]["hardwareLocked"] and checks["initial"]["disabled"] and not checks["initial"]["overflow"] and checks["datasets"]["visible"]
                checks["passed"] = checks["passed"] and checks["episode_viewer"]["episodes"]==30 and not checks["last_episode"]["errors"]
                checks["passed"] = checks["passed"] and not checks["playback"]["paused"] and not checks["playback"]["errors"]
                checks["passed"] = checks["passed"] and checks["extensions"]["jointCheckboxes"]==6 and checks["extensions"]["protectedDeleteDisabled"] and not checks["temperature_view"]["errors"]
                checks["passed"] = checks["passed"] and checks["reset_settings"]["rows"]==6 and checks["reset_settings"]["base"]=='4' and not checks["reset_settings"]["errors"]
                checks["passed"] = checks["passed"] and checks["capture_naming"]["readonly"] and checks["note_does_not_change_path"] and checks["dataset_naming"]["editor"] and checks["dataset_naming"]["readonlyCrop"]
                checks['pose_files_unchanged']=all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==digest for p,digest in originals.items())
                checks['passed']=checks['passed'] and checks['pose_precision']['preserved'] and checks['pose_precision']['bridge'] and all(re.fullmatch(r'-?\d+\.\d{2}',v) for v in checks['pose_precision']['display'])
                checks['passed']=checks['passed'] and checks['pose_editing']['positions']['shoulder_pan']==1.25 and checks['pose_editing']['positions']['shoulder_lift']==original_positions['shoulder_lift']
                checks['passed']=checks['passed'] and checks['pose_editing']['unsavedBlocked'] and checks['pose_editing']['emptyBlocked'] and checks['pose_picker']['calls']==1 and checks['pose_picker']['restored'] and checks['pose_files_unchanged']
                checks['passed']=checks['passed'] and checks['simple_workflow']['manualBatch']==0 and checks['simple_workflow']['language_in_name'] and checks['simple_workflow']['append_mode'] and checks['simple_workflow']['folder_picker'] and checks['feedback_history']['visible']
                if 'feedback_viewer' in checks:checks['passed']=checks['passed'] and checks['feedback_viewer']['videoWidth']==640 and not checks['feedback_viewer']['errors']
                checks['passed']=checks['passed'] and checks['joint_target_panel']['rows']==6 and checks['joint_target_panel']['executeDisabled'] and checks['joint_target_panel']['stopDisabled'] and checks['joint_target_panel']['visible']
                checks['passed']=checks['passed'] and len(checks['joint_target_plan']['single']['joints'])==1 and len(checks['joint_target_plan']['multiple']['joints'])==2 and checks['joint_target_plan']['emptyRejected'] and not checks['joint_target_plan']['active']
                checks['passed']=checks['passed'] and not checks['build_identity']['mismatch'] and checks['build_identity']['html']==checks['build_identity']['backend']==checks['build_identity']['native']==deployment.build
                checks['passed']=checks['passed'] and checks['crop_failure_gate']['result']=='failure' and checks['crop_failure_gate']['disabled'] and not checks['crop_failure_gate']['canCrop']
                checks['passed']=checks['passed'] and checks['crop_upsert_ui']['replacing'] and not checks['crop_upsert_ui']['disabled'] and checks['crop_upsert_ui']['total']==29
                checks['passed']=checks['passed'] and checks['crop_collection_ui']['count']==checks['crop_collection_ui']['success']==29 and checks['crop_collection_ui']['inherited'] and checks['crop_collection_ui']['disabled'] and checks['crop_collection_ui']['sourceButton'] and checks['crop_return_source'] and not checks['crop_collection_ui']['errors']
                window.evaluate_js("setView('datasets');var s=document.getElementById('dataset-select');s.value="+json.dumps(checks['crop_upsert_ui']['destination'])+";handle(openDataset(s.value));document.querySelector('main').scrollTop=0;true")
            except Exception as error:
                checks.update(passed=False, error=str(error))
            atomic_json(state_dir / "ui_smoke_report.json", checks)

        def ready():
            window.events.loaded.wait(15)
            deadline=time.monotonic()+20
            while not window.evaluate_js("Boolean(window.platformRequestClose && document.getElementById('connection-state').textContent.includes('正常') && nativeRuntimeIdentity)"):
                if time.monotonic()>deadline:raise TimeoutError('窗口身份核对未完成')
                time.sleep(.1)
            visible=window.evaluate_js("({build:htmlBuild,backend:state.identity.build,instance:state.identity.instance,native:nativeRuntimeIdentity.build,mismatch:runtimeMismatch,version:document.getElementById('runtime-version').textContent})")
            if visible['mismatch'] or any(visible[key]!=deployment.build for key in ('build','backend','native')) or visible['instance']!=snapshot['identity']['instance']:
                raise RuntimeError('实际窗口、后台与本地源码身份不一致，不通过验收')
            receipt={'verified':True,'hardware_accessed':False,'launch_nonce':args.launch_nonce,
                'host':host_identity,'window':visible,'url':url,'offline_at_launch':offline}
            if args.launch_nonce:
                launch=json.loads((state_dir/'launch_receipts'/(args.launch_nonce+'.json')).read_text(encoding='utf-8-sig'))
                if launch.get('expected_build')!=deployment.build or launch.get('nonce')!=args.launch_nonce:raise RuntimeError('桌面启动回执与实际窗口不同')
                receipt['desktop_launch']=launch
            atomic_json(state_dir/'window_identity_report.json',receipt)
            if args.camera_ui_test:
                report_path=state_dir/'camera_ui_report.json'
                atomic_json(report_path,{'phase':'starting','passed':False,'hardware_accessed':False,'build':deployment.build})
                try:
                    from camera_ui_check import run as check_camera_ui
                    check_camera_ui(window,request,report_path)
                except Exception as error:
                    atomic_json(report_path,{'passed':False,'hardware_accessed':False,'build':deployment.build,'error':repr(error)})
                    traceback.print_exc(file=output);output.flush()
            if args.smoke_test:smoke()

        webview.start(func=ready, gui="edgechromium", debug=False,
            private_mode=False, storage_path="E:/robot/cache/so101_ui/webview2",
            localization={"global.quitConfirmation": "确认关闭 SO101 数采平台？"})
    finally:
        if proc and proc.poll() is None:
            try:
                if not request("api/state").get("resources_active"):
                    request("api/shutdown", {})
                else:
                    snapshot = request("api/state")
                    selected=snapshot['job'] if snapshot['active'] else snapshot['preview_job']
                    request("api/command", {"command": "stop", "job_id": selected["id"]})
                    while request("api/state").get("resources_active"):
                        time.sleep(.2)
                    request("api/shutdown", {})
                proc.wait(timeout=8)
            except Exception:
                # No unconditional terminate/kill: an unverified worker could still own hardware.
                traceback.print_exc(file=output)
        output.close()
        kernel.CloseHandle(mutex)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        folder = HERE / "state"
        folder.mkdir(exist_ok=True)
        with (folder / "startup_error.log").open("a", encoding="utf-8") as stream:
            traceback.print_exc(file=stream)
        ctypes.windll.user32.MessageBoxW(None, str(exc) + "\n\n错误已保存在项目 state/startup_error.log。", "SO101 数采平台启动失败", 0x10)
