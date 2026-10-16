'use strict';
window.__qaErrors=[];
window.addEventListener('error',e=>{window.__qaErrors.push(String(e.message));window.__qaErrors=window.__qaErrors.slice(-20);});
window.addEventListener('unhandledrejection',e=>{window.__qaErrors.push(String(e.reason?.message||e.reason));window.__qaErrors=window.__qaErrors.slice(-20);});
const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="so101-token"]').content;
const htmlBuild=document.querySelector('meta[name="so101-build"]').content;
let runtimeMismatch=false;
const jointNames = ['底座旋转', '肩部抬升', '肘部', '腕部俯仰', '腕部旋转', '夹爪'];
const jointKeys = ['shoulder_pan', 'shoulder_lift', 'elbow_flex', 'wrist_flex', 'wrist_roll', 'gripper'];
const jointColors = ['#1c7bc0','#d47816','#a14fb8','#dc4a6c','#14987d','#647045'];
let datasetItems=[], lastArchive=null;
const phaseNames = {LOADING:'准备中', PREPARING:'准备设备', PREVIEW:'仅预览', SCANNING:'检测相机', MONITOR:'只读监控', CONTROL:'基本控制', SCENE_CHECK:'检查画面', SYNCING:'低速同步', SYNCED:'同步完成', WAITING_START:'等待手动开始', COUNTDOWN:'即将开始', RECORDING:'START · 正在录制', SAVING:'END · 正在保存', RESET:'END · 复位中', RESET_FINISHED:'复位结束', FOLLOWING:'遥操跟随中', PREFLIGHT:'只读预检', MODEL_LOADING:'加载 ACT', CAMERAS:'准备相机', WARMUP:'GPU 预热', READY:'就绪 · 等待开始', RESETTING:'复位到参考起点', ALIGNING:'低速对齐', RUNNING:'ACT 正在执行', FINISHING:'正在收尾', STOPPING:'等待停止确认', FINISHED:'已结束', ERROR:'发生异常', REPLAY_READY:'回放准备完成', REPLAYING:'真机轨迹回放', CROPPING:'正在裁剪副本'};
let state = null, view = 'capture', initialized = false, requestBusy = false, pollBusy = false;
let library = null, selectedEpisode = null, reviewTime = 0, cropPreview = false, lastJob = null;
let cropPlan=null;
let lastErrorAt = 0, toastTimer, drawRequest = 0;
let lastPreview=null, operationEpoch=0;
const frameBusy = {top:false, wrist:false}, frameUrls = {}, frameJob = {};
phaseNames.JOINT_MOVING='关节目标执行中';

async function api(path, body) {
  if(body!==undefined&&runtimeMismatch&&!(path==='/api/command'&&['stop','stop_preview','stop_device'].includes(body.command)))throw Error('窗口与后台版本不同，请正常停止后重开正式入口');
  const options = body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-SO101-Token':token,'X-SO101-Build':htmlBuild}, body:JSON.stringify(body)};
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `请求失败 ${response.status}`);
  return value;
}
function toast(message, error=false) {
  $('toast').textContent = String(message).slice(0,700);
  $('toast').classList.toggle('error', error); $('toast').classList.remove('hidden');
  clearTimeout(toastTimer); toastTimer = setTimeout(()=>$('toast').classList.add('hidden'), error ? 8000 : 4500);
}
function handle(promise) { return Promise.resolve(promise).catch(e=>{toast(e.message,true); return null;}); }
function confirmOperation(title, text) {
  return new Promise(resolve=>{
    const dialog=$('confirm-dialog');
    if(dialog.open){resolve(false);return;}
    $('confirm-title').textContent=title; $('confirm-text').textContent=text;
    $('confirm-checkbox').checked=false; $('confirm-accept').disabled=true;
    dialog.returnValue='cancel';
    dialog.addEventListener('close',()=>resolve(dialog.returnValue==='ok'),{once:true});
    dialog.showModal();
  });
}
$('confirm-checkbox').addEventListener('change',()=>{$('confirm-accept').disabled=!$('confirm-checkbox').checked;});

function setView(next) {
  if (!['capture','control','datasets','act','settings','feedback'].includes(next)) return;
  if(view==='datasets' && next!=='datasets') pausePlayback();
  view=next;
  const names={capture:'采集工作台',control:'设备与控制',datasets:'数据集',act:'ACT 测试',settings:'配置与环境',feedback:'ACT 历史与数据回流'};
  document.querySelectorAll('.nav-item').forEach(el=>el.classList.toggle('active',el.dataset.view===next));
  $('page-name').textContent=names[next];
  $('workbench').classList.toggle('hidden',['datasets','settings','feedback'].includes(next));
  $('feedback-page').classList.toggle('hidden',next!=='feedback');
  $('datasets-page').classList.toggle('hidden',next!=='datasets');
  $('settings-page').classList.toggle('hidden',next!=='settings');
  $('joint-control-panel').classList.toggle('hidden',next!=='control');
  for(const id of ['approve-scene','start-session','end-episode','end-reset','shortcut-hint'])$(id).hidden=next==='control';
  document.querySelectorAll('[data-inspector]').forEach(el=>el.classList.toggle('hidden',el.dataset.inspector!==next));
  $('workspace-title').textContent=names[next];
  $('workspace-subtitle').textContent={capture:'先检查画面并同步，再连续采集。录制与复位阶段始终清楚可见。',control:'按需连接设备。点动、保持与释放力矩均是明确的独立操作。',act:'调用现有 ACT 流程，显示真实执行状态。任务结果由操作者判断。'}[next]||'';
  if(next==='datasets' && !$('dataset-select').options.length) handle(refreshDatasets());
  if(next==='datasets') scheduleDraw();
  if(next==='feedback')handle(refreshFeedback());
  try{localStorage.setItem('so101-view',next);}catch{}
}
document.querySelectorAll('[data-view]').forEach(el=>el.addEventListener('click',()=>setView(el.dataset.view)));

function populateConfig(cfg) {
  const map={'cfg-top':'top_camera','cfg-wrist':'wrist_camera','cfg-preview':'preview_hz','cfg-leader':'leader_usb_serial','cfg-follower':'follower_usb_serial','cfg-datasets':'dataset_home','cfg-checkpoint':'checkpoint','cfg-calibration':'calibration','record-task':'task','record-count':'episodes','record-duration':'record_seconds','reset-duration':'reset_seconds','act-duration':'act_seconds','act-checkpoint':'checkpoint','act-reference':'reference_start_pose'};
  Object.entries(map).forEach(([id,key])=>{$(id).value=cfg[key];});
  $('cfg-rerun').checked=cfg.rerun;
  populatePortableConfig(cfg);
  $('act-task').value=cfg.task;
  populateResetSettings(cfg);
  handle(ensureDatasetNaming());
}
function normalizeHealth(t) {
  const h=t?.health||{};
  return {temperature:h.temperature||h.Present_Temperature||{},torque:h.torque_enabled||h.torque||h.Torque_Enable||{},status:h.status||h.Status||{},current:h.current_raw||{}};
}
function showNumber(value, digits=1) {return typeof value==='number' && Number.isFinite(value)?value.toFixed(digits):'—';}
function updateTelemetry(t, active) {
  const health=normalizeHealth(t);
  jointKeys.forEach((name,index)=>{
    const row=$('joint-'+index);
    row.querySelector('[data-col=state]').textContent=showNumber(t.state?.[index]);
    row.querySelector('[data-col=target]').textContent=showNumber(t.target?.[index]);
    row.querySelector('[data-col=temp]').textContent=showNumber(health.temperature[name],0);
    row.querySelector('[data-col=torque]').textContent=health.torque[name]===1?'保持':health.torque[name]===0?'关闭':'—';
    row.querySelector('[data-col=status]').textContent=health.status[name]===undefined?'—':String(health.status[name]);
    row.querySelector('[data-col=current]').textContent=showNumber(health.current[name],0);
  });
  $('telemetry-hint').textContent=t.state?(active?'真实设备反馈'+(t.effective_hz?` · ${t.effective_hz.toFixed(1)} Hz`:''):'上次会话最终数据 · 非实时'):'尚未读取真实设备';
}
function updateControls() {
  if(!state)return;
  const active=state.active, job=state.job, phase=job?.phase||'', offline=state.offline||runtimeMismatch||state.identity?.source_changed||state.restart_required;
  const preview=!!state.preview_active, resources=!!(state.resources_active||active||preview);
  const prepare=['prepare-record','prepare-teleop','prepare-act','scan-cameras','monitor-device','connect-control','prepare-replay','act-reset-only'];
  document.querySelectorAll('.hardware').forEach(el=>{el.disabled=!!offline; el.title=offline?'本次离线验收已锁定硬件入口':'';});
  prepare.forEach(id=>{$(id).disabled=offline||active||requestBusy;});
  $('scan-cameras').disabled=offline||active||preview||requestBusy;
  const taskOwnsCameras=active&&!['monitor','control'].includes(job.kind);
  $('preview-cameras').disabled=preview?requestBusy||!!state.preview_job?.stopping:offline||taskOwnsCameras||requestBusy;
  $('preview-cameras').textContent=preview?'关闭双相机预览':state.camera_active?'使用任务内相机预览':'打开双相机预览';
  $('preview-cameras').title=taskOwnsCameras?'当前任务独占设备；监控、基本控制可与独立相机预览并行':'只打开或关闭相机，不连接机械臂，也不改变力矩';
  $('disconnect-device').disabled=!active||!['monitor','control'].includes(job.kind)||requestBusy;
  $('camera-session-hint').textContent=(preview?'相机：独立预览运行中':state.camera_active?'相机：由当前任务提供画面':'相机：未开启')+' · '+(active&&['monitor','control'].includes(job.kind)?'从臂：'+(job.kind==='monitor'?'只读监控':'基本控制'):'从臂：无独立控制连接');
  $('stop-all').disabled=!resources;
  $('approve-scene').disabled=offline||!active||phase!=='SCENE_CHECK';
  $('start-session').disabled=offline||!active||!['WAITING_START','READY','REPLAY_READY'].includes(phase);
  $('start-session').textContent=job?.kind==='act'?'开始 ACT（复位后执行）':job?.kind==='teleop'?'进入正式遥操':job?.kind==='replay'?'开始低速对齐与回放':'开始本批采集';
  $('end-episode').disabled=!active||phase!=='RECORDING'||!(state.telemetry?.episode_frames>0);
  $('end-reset').disabled=!active||phase!=='RESET';
  const control=active&&job.kind==='control'&&phase==='CONTROL';
  ['release-torque','jog-minus','jog-plus','reference-reset'].forEach(id=>{$(id).disabled=offline||!control;});
  $('hold-position').disabled=offline||(!control&&!(active&&job.kind==='act'&&phase==='READY'));
  for(const id of ['quick-hold','quick-release']){
    $(id).disabled=offline||requestBusy||(active&&!control);
    $(id).title=offline?'离线模式已锁定；可手动解锁':active&&!control?'先停止采集 / 遥操，再进行独立力矩操作；不会在录制中突然释放':'显式确认后才操作，不会在打开页面时自动连接';
  }
  $('hardware-mode').textContent=offline?'启用设备操作':'锁定硬件（离线）';
  $('hardware-mode').disabled=resources||runtimeMismatch||state.identity?.source_changed;
  const dataset=datasetItems.find(d=>d.root===$('dataset-select').value);
  $('archive-dataset').disabled=resources||!dataset||!!dataset.protected;
  $('archive-dataset').title=dataset?.protected||'整份文件夹移入工作区可恢复归档，不永久删除';
  $('create-dataset').disabled=resources;
  $('restore-dataset').disabled=resources||!lastArchive;
  $('restore-selected').disabled=resources||!$('archive-select').value;
  updateNamingControls();
  $('act-success').disabled=active||job?.kind!=='act'||!job?.finished;
  $('act-failure').disabled=$('act-success').disabled;
  $('save-settings').disabled=resources;
  $('save-reset-settings').disabled=resources;
  $('copy-start-pose').disabled=resources;
  $('browse-start-pose').disabled=resources;
  $('load-start-pose').disabled=resources;
  $('export-crop').disabled=active||!selectedEpisode||requestBusy||!cropPlan?.can_crop||library?.annotations?.[selectedEpisode?.index]?.result!=='success';
  $('export-crop').textContent=cropPlan?.replacing?'更新此条裁剪（覆盖旧版）':'保存到裁剪集';
  $('crop-browse').disabled=active||!cropPlan?.exists||!!library?.crop_collection;
  document.querySelectorAll('[data-result],#save-note').forEach(button=>button.disabled=active&&state.job?.kind==='crop');
  $('shortcut-hint').textContent=phase==='RECORDING'?'Space：结束本条并保存。Esc：停止本批。':phase==='RESET'?'Space：结束复位，进入下一条。Esc：停止本批。':'Space 仅在录制 / 复位阶段提前结束。Esc：停止当前任务。';
  updateJointControls();
  applyPortableState();
}
function updatePhase() {
  if(!state)return;
  const failedRobot=!state.active&&state.job?.phase==='ERROR';
  const job=state.active||failedRobot?state.job:state.preview_active?state.preview_job:state.job||state.preview_job, phase=job?.phase||'';
  $('phase-title').textContent=(phaseNames[phase]||'未开始')+(state.active&&state.preview_active?' + 双相机':'');
  $('phase-message').textContent=(job?.message||'界面打开不会连接设备或产生运动。')+(failedRobot&&state.preview_active?'；机械臂会话已退出，独立相机预览仍在运行。':state.active&&state.preview_active?'；独立相机预览同时运行。':'');
  const phaseCard=document.querySelector('.phase-card');
  phaseCard.classList.toggle('recording',phase==='RECORDING'||phase==='RUNNING');
  phaseCard.classList.toggle('reset',phase==='RESET'||phase==='SAVING');
  phaseCard.classList.toggle('error',phase==='ERROR');
  let counter=job?`会话 ${job.id.slice(9,15)}`:'等待配置';
  if(job?.episode!==undefined)counter=`Episode ${job.episode+1}`;
  if(job?.saved!==undefined)counter+=` · 累计保存 ${job.saved} 条`;
  $('phase-count').textContent=counter;
  if(state.active&&job?.deadline){
    const left=Math.max(0,job.deadline-Date.now()/1000);
    $('phase-time').textContent=left.toFixed(1)+' s';
  }else if(state.active&&phase==='CROPPING'&&state.telemetry.crop_total){
    $('phase-time').textContent=`${state.telemetry.crop_done} / ${state.telemetry.crop_total} 帧`;
  }else{$('phase-time').textContent=state.telemetry?.effective_hz?state.telemetry.effective_hz.toFixed(1)+' Hz':'—';}
  const maximum=phase==='RECORDING'?Number($('record-duration').value):phase==='RESET'?Number($('reset-duration').value):3;
  $('phase-progress').value=state.active&&job?.deadline?Math.max(0,Math.min(1,1-(job.deadline-Date.now()/1000)/maximum)):state.active&&phase==='CROPPING'?((state.telemetry.crop_done||0)/(state.telemetry.crop_total||1)):0;
}
async function poll() {
  if(pollBusy)return;pollBusy=true;
  try{
    const next=await api('/api/state'); state=next;
    await updateRuntimeIdentity(next.identity);
    if(!initialized){populateConfig(next.config);initialized=true;handle(refreshDatasets());}
    $('offline-notice').classList.toggle('hidden',!next.offline);
    $('hardware-badge').textContent=next.offline?'离线 · 硬件锁定':next.active&&next.job.kind!=='crop'?'设备会话运行中':next.preview_active?'仅相机预览':'设备未连接';
    $('connection-state').textContent='● 本地服务正常 · 系统 WebView2';
    const memory=next.memory?` · 后台 ${next.memory.host_mb} MB${next.memory.worker_mb?' / 任务 '+next.memory.worker_mb+' MB':''}${next.memory.preview_mb?' / 相机 '+next.memory.preview_mb+' MB':''}`:'';
    $('resource-state').textContent=`工作区可用 ${next.disk_free_gb} GB${memory}`;
    $('task-state').textContent=next.active?(next.job.kind==='crop'&&next.telemetry.crop_total?`裁剪 ${next.telemetry.crop_done} / ${next.telemetry.crop_total} 帧`:(phaseNames[next.job.phase]||next.job.phase)+(next.preview_active?' · 相机预览':'')):next.preview_active?'独立相机预览':'没有后台任务';
    updateTelemetry(next.telemetry||{},next.active);updateControls();updatePhase();
    const details=document.querySelector('.log-panel');
    if(details.open){
      const log=$('event-log'), nearBottom=log.scrollHeight-log.scrollTop-log.clientHeight<40;
      const fragment=document.createDocumentFragment();
      next.events.slice(-100).forEach(e=>{const line=document.createElement('div');line.className='log-line '+e.level;const time=document.createElement('time');time.textContent=e.time;const text=document.createElement('span');text.textContent=e.message;line.append(time,text);fragment.append(line);});
      log.replaceChildren(fragment);if(nearBottom)log.scrollTop=log.scrollHeight;
    }
    if(next.job?.cameras)$('camera-results').textContent='可用索引：'+next.job.cameras.map(c=>`${c.index} (${c.width}×${c.height})`).join('、');
    if(lastJob && next.job && lastJob.id===next.job.id && lastJob.active && !next.active){
      if(next.job.kind==='record')handle(refreshDatasets());
      if(next.job.kind==='act')handle(refreshFeedback());
      if(next.job.phase==='ERROR')toast(next.job.message,true);
      if(next.job.kind==='crop'&&next.job.result?.outcome==='cropped'){
        toast(`裁剪集已更新，共 ${next.job.result.total_crops} 条；${next.job.result.replaced?'已替换原来的裁剪结果':'原始轨迹未修改'}。`);handle((async()=>{const root=library?.root,index=selectedEpisode?.index;await refreshDatasets();if(root===library?.root&&index!==undefined)await selectEpisode(index);})());
      }
    }
    lastJob=next.job?{id:next.job.id,active:next.active}:null;
    if(lastPreview?.active&&next.preview_job?.id===lastPreview.id&&!next.preview_active&&next.preview_job.phase==='ERROR')toast('相机预览异常：'+next.preview_job.message,true);
    lastPreview=next.preview_job?{id:next.preview_job.id,active:next.preview_active}:null;
    document.querySelectorAll('.camera-status').forEach(el=>{const name=el.closest('.camera-panel').dataset.camera;const stamp=next.frames[name];el.textContent=stamp?(next.camera_active&&next.frame_owner===next.camera_owner_id&&Date.now()/1000-stamp<2?'实时预览':'最后画面 · 非实时'):'未连接';});
  }catch(e){$('connection-state').textContent='本地服务连接中断';if(Date.now()-lastErrorAt>10000){toast(e.message,true);lastErrorAt=Date.now();}}
  finally{pollBusy=false;}
}
async function refreshFrame(name){
  if(frameBusy[name]||!state?.camera_active||!state.frames[name]||state.frame_owner!==state.camera_owner_id||view==='datasets'||view==='settings'||document.hidden)return;
  const owner=state.camera_owner_id;
  frameBusy[name]=true;
  try{
    const res=await fetch('/frame/'+name+'?session='+encodeURIComponent(owner)+'&t='+Date.now());
    if(!res.ok||res.status===204)return;
    const blob=await res.blob();if(owner!==state.camera_owner_id)return;
    const image=$('frame-'+name), previous=frameUrls[name], current=URL.createObjectURL(blob);
    frameUrls[name]=current;
    await new Promise((resolve,reject)=>{
      image.onload=()=>{if(previous)URL.revokeObjectURL(previous);if(owner===state.camera_owner_id){image.hidden=false;image.nextElementSibling.hidden=true;frameJob[name]=owner;}resolve();};
      image.onerror=()=>{URL.revokeObjectURL(current);if(previous)URL.revokeObjectURL(previous);delete frameUrls[name];image.hidden=true;image.nextElementSibling.hidden=false;reject(Error(name+' 预览图像加载失败，请查看日志'));};
      image.src=current;
    });
  }
  finally{frameBusy[name]=false;}
}
async function startJob(kind,options={}){
  if(requestBusy)return;requestBusy=true;updateControls();
  const epoch=operationEpoch;
  try{
    if(state.preview_active&&!['preview','monitor','control'].includes(kind)){
      await command('stop_preview');
      const deadline=Date.now()+12000;
      do{await new Promise(resolve=>setTimeout(resolve,150));await poll();if(Date.now()>deadline)throw Error('相机尚未释放，未启动新任务');if(epoch!==operationEpoch)throw Error('准备已取消，未启动新任务');}while(state.preview_active);
    }
    const job=await api('/api/jobs',{kind,options});if(kind==='record')$('record-root').value=job.dataset_root;await poll();
  }
  finally{requestBusy=false;updateControls();}
}
async function command(name, extra={}) {
  if(name==='stop')operationEpoch++;
  const job=name==='stop_preview'?state?.preview_job:state?.active?state.job:name==='stop'?state?.preview_job:null;
  const running=name==='stop_preview'?state?.preview_active:name==='stop'?state?.resources_active:state?.active;
  if(!running||!job)throw new Error('没有运行中的会话');
  await api('/api/command',{command:name,job_id:job.id,...extra});
  if(name==='stop')toast('已请求停止，等待后台收尾，不会自动释放力矩。');
}
$('stop-all').addEventListener('click',()=>handle(command('stop')));
$('hardware-mode').addEventListener('click',()=>handle((async()=>{
  const offline=!state.offline;
  if(await confirmOperation(offline?'锁定硬件入口':'启用设备操作入口',offline?'仅锁定后续设备操作，离线查看和裁剪仍可使用。':'解锁只启用按钮，不枚举、不连接、更不会移动机械臂。之后连接、同步和开始运动仍是独立操作。')){
    await api('/api/hardware-mode',{offline,confirmed:true});await poll();
  }
})()));
$('prepare-record').addEventListener('click',()=>handle((async()=>{
  const plan=await captureNaming(),{root,existing}=plan;
  if(existing&&!await confirmOperation('追加到已有数据集','本次将追加新 episode，已有数据不删除、不覆盖。请核对任务、相机和标定与原数据一致。'))return;
  const episode_tasks=$('record-tasks').value.trim()?$('record-tasks').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean):[];
  await startJob('record',{capture_mode:plan.mode,parent:plan.parent,dataset_root:root,dataset_spec:plan.spec,task:$('record-task').value,episode_tasks,episodes:Number($('record-count').value),record_seconds:Number($('record-duration').value),reset_seconds:Number($('reset-duration').value),group:$('record-group').value,append_confirmed:existing});
})()));
$('prepare-teleop').addEventListener('click',()=>handle(startJob('teleop')));
$('prepare-act').addEventListener('click',()=>handle(startJob('act',{duration:Number($('act-duration').value),reference_start_pose:$('act-reference').value,task:$('act-task').value,group:$('act-group').value})));
async function quickControl(name){
  const text=name==='release'?'已可靠支撑从臂，关闭力矩后它不会保持姿态。':name==='reset'?'夹爪为空，当前姿态到预设起点的完整路径无障碍；加权复位不具备自动避障能力。':'请先支撑从臂。程序将读取当前位置、写入当前目标再开启保持；收到反馈后再移开支撑。';
  if(!await confirmOperation({hold:'开启力矩并保持',release:'支撑后释放力矩',reset:'仅复位，不执行模型'}[name],text))return;
  if(state.active)await command(name,{confirmed:true});
  else await startJob('control',{initial_command:name,one_shot:true,confirmed:true,reference_start_pose:name==='reset'?$('act-reference').value:state.config.reference_start_pose});
}
$('quick-hold').addEventListener('click',()=>handle(quickControl('hold')));
$('quick-release').addEventListener('click',()=>handle(quickControl('release')));
$('act-reset-only').addEventListener('click',()=>handle(quickControl('reset')));
$('reset-settings-link').addEventListener('click',()=>{setView('settings');$('reset-profile-card').scrollIntoView({behavior:'smooth',block:'start'});});
const starters={'scan-cameras':'camera_scan','monitor-device':'monitor','connect-control':'control'};
Object.entries(starters).forEach(([id,kind])=>$(id).addEventListener('click',()=>handle(startJob(kind))));
$('preview-cameras').addEventListener('click',()=>handle(state?.preview_active?command('stop_preview'):startJob('preview')));
$('disconnect-device').addEventListener('click',()=>handle(command('stop_device')));
$('approve-scene').addEventListener('click',()=>handle((async()=>{if(await confirmOperation('画面检查与启动同步','已核对 Top/Wrist 画面，机械臂夹紧，主臂扶稳，完整运动范围无碰撞物，手和支撑不在从臂路径上。确认后从臂开始低速靠近主臂。'))await command('scene',{confirmed:true});})()));
$('start-session').addEventListener('click',()=>handle((async()=>{
  const kind=state.job.kind;
  const text=kind==='act'?'夹爪为空、娃娃和盒子已摆好，当前姿态到参考起点的完整复位路线及抓放范围无障碍，手和支撑已移开。确认后先按既有方法复位，再运行 ACT。':kind==='replay'?'已检查整条示范路线、机械臂固定，夹爪为空、场景与示范匹配，手和支撑已移开。确认后先低速对齐，再按 observation.state 回放。':'主臂已扶稳，准备进行抓放。确认后有 3 秒倒计时，随后开始本批；后续复位保持遥操。';
  if(await confirmOperation(kind==='act'?'开始 ACT':kind==='replay'?'开始真机回放':'开始采集 / 遥操',text))await command('start',{confirmed:true});
})()));
$('end-episode').addEventListener('click',()=>handle(command('advance',{phase:'RECORDING'})));
$('end-reset').addEventListener('click',()=>handle(command('advance',{phase:'RESET'})));
$('hold-position').addEventListener('click',()=>handle((async()=>{if(await confirmOperation('开启当前位置保持','请先可靠支撑从臂。程序将实测位置写为目标后开启力矩，不追逐旧目标；待界面确认后再移开支撑。'))await command('hold',{confirmed:true});})()));
$('release-torque').addEventListener('click',()=>handle((async()=>{if(await confirmOperation('释放从臂力矩','从臂已靠在能够承重的支撑上，或由你可靠托住。关闭力矩后不会保持姿态，可能下落。'))await command('release',{confirmed:true});})()));
for(const [id,delta] of [['jog-minus',-1],['jog-plus',1]])$(id).addEventListener('click',()=>handle((async()=>{const joint=Number($('jog-joint').value);if(await confirmOperation('小步点动',`${jointNames[joint]} 将移动 ${delta} ${joint===5?'个百分点':'度'}。请确认已保持力矩、周围无障碍。`))await command('jog',{confirmed:true,joint,delta});})()));
$('reference-reset').addEventListener('click',()=>handle((async()=>{if(await confirmOperation('复位到参考起点','夹爪为空，当前到参考起点的完整路径无障碍。加权复位不等于自动避障，结束后保持力矩。'))await command('reset',{confirmed:true});})()));
for(const [id,success] of [['act-success',true],['act-failure',false]])$(id).addEventListener('click',()=>handle(api('/api/result',{success}).then(()=>{toast('已保存人工判断，不自动分析录像。');return refreshFeedback();})));

async function refreshDatasets(){
  const items=await api('/api/datasets'),select=$('dataset-select'),previous=select.value;datasetItems=items;
  select.replaceChildren(new Option('请选择数据集',''));
  items.forEach(d=>{const option=new Option(datasetOptionLabel(d)+(d.protected?' · 保留':''),d.root);option.title=d.naming?.display_name+'\n'+d.root;select.add(option);});
  if(items.some(d=>d.root===previous))select.value=previous;
  const archives=await api('/api/archives'), archiveSelect=$('archive-select');
  archiveSelect.replaceChildren(new Option('选择已删除的数据集',''));
  archives.forEach(d=>archiveSelect.add(new Option(d.name+' · '+d.created.slice(0,16),d.manifest)));
  refreshRecordTargets();updateControls();return items;
}
$('archive-select').addEventListener('change',updateControls);
$('restore-selected').addEventListener('click',()=>handle((async()=>{
  const manifest=$('archive-select').value;if(!manifest)return;
  if(!await confirmOperation('恢复归档数据集','将所选整份文件夹恢复到原目录。若原目录已存在则拒绝覆盖，不修改其他数据集。'))return;
  await api('/api/dataset-management',{action:'restore',manifest});await refreshDatasets();toast('已恢复到原目录。');
})()));
$('create-dataset').addEventListener('click',()=>handle((async()=>{
  await ensureDatasetNaming();const plan=await newNameForm.update();if(!plan)throw Error('请核对父目录、语言指令和描述');
  $('record-target-mode').value='new';$('record-parent').value=plan.parent;$('record-task').value=plan.spec.instruction;$('record-group').value=plan.spec.description;
  await recordNameForm.update();recordTargetChanged();setView('capture');toast('已带入新建计划；连接采集设备时创建目录，尚未访问设备。');
})()));
$('archive-dataset').addEventListener('click',()=>handle((async()=>{
  const item=datasetItems.find(d=>d.root===$('dataset-select').value);if(!item||item.protected)return;
  if(!await confirmOperation('删除整份采集文件夹',`将 ${item.name}（${item.episodes} 条）移出数据集库，归档到当前工作区的 dataset_archive。\n可恢复，不永久删除，也不会释放磁盘空间。训练权重、其他数据集不变。`))return;
  pausePlayback();lastArchive=await api('/api/dataset-management',{action:'archive',root:item.root,confirmed_name:item.name});
  await openDataset('');await refreshDatasets();$('dataset-select').value='';toast('整份数据集已移入可恢复归档。');
})()));
$('restore-dataset').addEventListener('click',()=>handle((async()=>{
  if(!lastArchive)return;
  await api('/api/dataset-management',{action:'restore',manifest:lastArchive.manifest});lastArchive=null;await refreshDatasets();toast('已恢复到原目录，不覆盖已有文件。');
})()));
$('refresh-datasets').addEventListener('click',()=>handle(refreshDatasets().then(()=>toast('数据集列表已刷新'))));
$('dataset-select').addEventListener('change',()=>handle(openDataset($('dataset-select').value)));
async function openDataset(root){
  pausePlayback(); selectedEpisode=null;cropPlan=null;
  if(!root){library=null;renderEpisodeList();$('dataset-summary').textContent='尚未打开数据集';renderDatasetNaming(null);$('episode-content').classList.add('hidden');$('dataset-empty').classList.remove('hidden');return;}
  const item=datasetItems.find(d=>d.root===root);
  renderDatasetNaming(item);
  if(item&&!item.ready){$('record-target-mode').value='existing';$('record-existing').value=root;recordTargetChanged();setView('capture');toast('此目录待采集，已填入采集计划。');return;}
  $('dataset-summary').textContent='正在读取元数据…';
  const loaded=await api('/api/dataset?root='+encodeURIComponent(root));
  if($('dataset-select').value!==root)return;
  library=loaded;
  $('dataset-summary').textContent=`${loaded.total_episodes} 条 · ${loaded.total_frames.toLocaleString()} 帧 · ${loaded.fps} Hz · ${loaded.version}`;
  renderEpisodeList();if(loaded.episodes.length)await selectEpisode(loaded.episodes[0].index);
}
function renderEpisodeList(){
  const list=$('episode-list'),search=$('episode-search').value.toLowerCase();list.replaceChildren();
  (library?.episodes||[]).filter(e=>`${e.index+1} ${e.tasks.join(' ')}`.toLowerCase().includes(search)).forEach(e=>{
    const button=document.createElement('button');button.className='episode-item'+(selectedEpisode?.index===e.index?' selected':'');
    const number=document.createElement('span');number.className='episode-number';number.textContent=String(e.index+1).padStart(2,'0');
    const detail=document.createElement('div');detail.className='episode-info';const title=document.createElement('strong');title.textContent=e.source_episode!==undefined?`原始第 ${e.source_episode+1} 条 · 裁剪版`:`Episode ${e.index+1}`;
    const subtitle=document.createElement('span');subtitle.textContent=`${e.duration.toFixed(2)} s · ${e.frames} 帧`;detail.append(title,subtitle);
    const mark=document.createElement('span');mark.className='episode-mark';mark.textContent={success:'✓',failure:'失败',exclude:'排除'}[library.annotations?.[e.index]?.result]||'';
    button.append(number,detail,mark);button.addEventListener('click',()=>handle(selectEpisode(e.index)));list.append(button);
  });
}
$('episode-search').addEventListener('input',renderEpisodeList);
let selectedLoad=0;
async function selectEpisode(index){
  pausePlayback();cropPlan=null;updateControls();const ticket=++selectedLoad;
  const loaded=await api('/api/dataset?root='+encodeURIComponent(library.root)+'&episode='+index);
  if(ticket!==selectedLoad)return;
  library={...library,trajectory:loaded.trajectory,annotations:loaded.annotations,signals:loaded.signals,signal_note:loaded.signal_note,features:loaded.features};selectedEpisode=loaded.selected;
  $('dataset-empty').classList.add('hidden');$('episode-content').classList.remove('hidden');
  $('episode-title').textContent=(selectedEpisode.source_episode!==undefined?`原始第 ${selectedEpisode.source_episode+1} 条 · 裁剪版`:`Episode ${index+1}`)+` · ${selectedEpisode.duration.toFixed(2)} s`;
  $('episode-task').textContent='语言指令：'+(selectedEpisode.tasks.join(' / ')||'未记录');
  const metric=$('plot-metric'), previous=metric.value;metric.replaceChildren(new Option('位置：实测 / 示教目标','position'));
  (library.signals||[]).forEach(s=>metric.add(new Option(s.label+' · '+s.unit,s.key)));
  if([...metric.options].some(o=>o.value===previous))metric.value=previous;
  renderFeatures();
  $('review-timeline').max=selectedEpisode.duration;$('review-timeline').step=1/library.fps;
  $('crop-start').value='0';$('crop-end').value=selectedEpisode.duration.toFixed(6);
  handle(syncCropNaming());
  $('annotation-note').value=library.annotations?.[index]?.note||'';
  for(const name of ['top','wrist']){
    const video=$('review-'+name),meta=selectedEpisode.videos['observation.images.'+name];
    video.removeAttribute('src');video.load();
    if(meta?.exists){video.src=meta.url;video.onloadedmetadata=()=>{video.currentTime=meta.start;video.playbackRate=Number($('playback-speed').value);};video.load();}
  }
  reviewTime=0;renderEpisodeList();renderAnnotation();updateReviewTime(0);scheduleDraw();updateControls();
}
function renderAnnotation(){document.querySelectorAll('[data-result]').forEach(b=>b.classList.toggle('selected',library?.annotations?.[selectedEpisode?.index]?.result===b.dataset.result));}
async function saveAnnotation(result){
  if(!selectedEpisode)return;
  library.annotations=await api('/api/annotation',{root:library.root,episode:selectedEpisode.index,result:result||library.annotations?.[selectedEpisode.index]?.result||'unreviewed',note:$('annotation-note').value});
  renderAnnotation();renderEpisodeList();await syncCropNaming();toast('审核记录已保存，原始版与裁剪版保持一致；未改写轨迹。');
}
document.querySelectorAll('[data-result]').forEach(b=>b.addEventListener('click',()=>handle(saveAnnotation(b.dataset.result))));
$('save-note').addEventListener('click',()=>handle(saveAnnotation()));
function pausePlayback(){for(const name of ['top','wrist'])$('review-'+name).pause();$('play-pause').textContent='播放';cropPreview=false;}
function updateReviewTime(time){reviewTime=time;$('review-timeline').value=time;$('review-time').textContent=`${time.toFixed(2)} / ${(selectedEpisode?.duration||0).toFixed(2)} s`;scheduleDraw();}
function seek(time){
  if(!selectedEpisode)return;
  time=Math.max(0,Math.min(selectedEpisode.duration-1/library.fps,time));
  for(const name of ['top','wrist']){const meta=selectedEpisode.videos['observation.images.'+name];const video=$('review-'+name);if(meta&&video.readyState>=1)video.currentTime=meta.start+time;}
  updateReviewTime(time);
}
async function play(){
  if(!selectedEpisode)return;
  if(reviewTime>=selectedEpisode.duration-1/library.fps)seek(0);
  const promises=[];for(const name of ['top','wrist']){const video=$('review-'+name);if(video.getAttribute('src'))promises.push(video.play());}
  await Promise.all(promises);$('play-pause').textContent='暂停';
}
$('play-pause').addEventListener('click',()=>handle($('review-top').paused?play():pausePlayback()));
$('previous-frame').addEventListener('click',()=>{pausePlayback();seek(reviewTime-1/library.fps);});
$('next-frame').addEventListener('click',()=>{pausePlayback();seek(reviewTime+1/library.fps);});
$('review-timeline').addEventListener('input',()=>{pausePlayback();seek(Number($('review-timeline').value));});
$('playback-speed').addEventListener('change',()=>{for(const name of ['top','wrist'])$('review-'+name).playbackRate=Number($('playback-speed').value);});
$('review-top').addEventListener('timeupdate',()=>{
  if(!selectedEpisode)return;const meta=selectedEpisode.videos['observation.images.top'];if(!meta)return;
  const t=Math.max(0,$('review-top').currentTime-meta.start),limit=cropPreview?Number($('crop-end').value):selectedEpisode.duration;
  updateReviewTime(Math.min(t,selectedEpisode.duration));
  if(t>=limit-.5/library.fps){pausePlayback();return;}
  const wrist=selectedEpisode.videos['observation.images.wrist'];
  if(wrist&&!$('review-top').paused&&$('review-wrist').readyState>=1&&Math.abs(($('review-wrist').currentTime-wrist.start)-t)>.12)$('review-wrist').currentTime=wrist.start+t;
});
for(const name of ['top','wrist'])$('review-'+name).addEventListener('error',()=>{if($('review-'+name).getAttribute('src'))toast(`${name} 视频无法解码。原录像未修改，请检查编码支持。`,true);});
$('mark-start').addEventListener('click',()=>{$('crop-start').value=reviewTime.toFixed(6);updateCropHint();});
$('mark-end').addEventListener('click',()=>{$('crop-end').value=reviewTime.toFixed(6);updateCropHint();});
function cropRange(){const start=Number($('crop-start').value),end=Number($('crop-end').value);if(!selectedEpisode||!Number.isFinite(start)||!Number.isFinite(end)||start<0||start>=end||end>selectedEpisode.duration+1e-5)throw new Error('保留范围应满足 0 ≤ 起点 < 终点 ≤ 本条时长');return {start,end};}
function updateCropHint(){try{const {start,end}=cropRange();$('crop-hint').textContent=cropPlan&&!cropPlan.can_crop?cropPlan.reason:`保留 ${start.toFixed(2)}–${end.toFixed(2)} s，约 ${(end-start).toFixed(2)} 秒。${cropPlan?.replacing?'重剪将替换此原轨迹已有的裁剪，不增加条数。':'同一来源统一保存到一个裁剪集，每条原轨迹仅一份。'}`;}catch(e){$('crop-hint').textContent=e.message;}scheduleDraw();}
['crop-start','crop-end'].forEach(id=>$(id).addEventListener('input',updateCropHint));
$('preview-crop').addEventListener('click',()=>handle((async()=>{const {start}=cropRange();pausePlayback();seek(start);cropPreview=true;await play();})()));
$('export-crop').addEventListener('click',()=>handle((async()=>{
  if(!cropPlan?.can_crop||library.annotations?.[selectedEpisode.index]?.result!=='success')throw Error('只有成功的原始轨迹才允许裁剪');
  const range=cropRange();
  if(!await confirmOperation(cropPlan.replacing?'替换此条裁剪':'保存到裁剪集',`保留原始第 ${selectedEpisode.index+1} 条的 ${range.start.toFixed(3)}–${range.end.toFixed(3)} 秒。\n${cropPlan.replacing?'将覆盖已有裁剪，不增加轨迹条数。':'与同一来源的其他裁剪统一保存在一个数据集中。'}\n${cropPlan.destination}\n原始数据不删除、不覆盖。审核状态与原始版一致。`))return;
  pausePlayback();
  await startJob('crop',{dataset_root:library.root,episode:selectedEpisode.index,...range});toast('正在更新裁剪集，可在底栏查看进度。');
})()));
$('crop-browse').addEventListener('click',()=>handle((async()=>{const destination=cropPlan?.destination;if(!destination||!cropPlan.exists)throw Error('尚无已保存的裁剪集');await refreshDatasets();$('dataset-select').value=destination;await openDataset(destination);})()));
$('open-crop-source').addEventListener('click',()=>handle((async()=>{const collection=library?.crop_collection,entry=collection?.episodes.find(e=>e.episode_index===selectedEpisode?.index);if(!entry)return;await refreshDatasets();$('dataset-select').value=collection.source;await openDataset(collection.source);await selectEpisode(entry.source_episode);})()));
$('prepare-replay').addEventListener('click',()=>handle((async()=>{await startJob('replay',{dataset_root:library.root,episode:selectedEpisode.index});setView('control');})()));

function scheduleDraw(){if(!drawRequest)drawRequest=requestAnimationFrame(()=>{drawRequest=0;drawChart();});}
function drawChart(){
  const canvas=$('trajectory-chart'),rect=canvas.getBoundingClientRect();if(!rect.width||!library?.trajectory||!selectedEpisode)return;
  const dpr=window.devicePixelRatio||1;canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);
  const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);const w=rect.width,h=rect.height,left=45,right=42,top=20,bottom=27;
  const joints=[...$('joint-checkboxes').querySelectorAll('input:checked')].map(e=>Number(e.value));
  const position=$('plot-metric').value==='position', data=library.trajectory, signal=library.signals?.find(s=>s.key===$('plot-metric').value);
  const ts=position?data.time:signal?.time||[],series=[];
  joints.forEach(j=>{
    if(position){series.push({j,values:data.state.map(v=>v[j]),dashed:false});series.push({j,values:data.action.map(v=>v[j]),dashed:true});}
    else if(signal?.values[0]?.length===6)series.push({j,values:signal.values.map(v=>v?.[j]),dashed:false});
  });
  if(!position&&signal?.values[0]?.length!==6)series.push({j:0,values:signal?.values.map(v=>Array.isArray(v)?v[0]:v)||[],dashed:false});
  const primary=series.filter(s=>!position||s.j!==5).flatMap(s=>s.values).filter(Number.isFinite);
  const lo=primary.length?Math.min(...primary):0,hi=primary.length?Math.max(...primary):1,padding=Math.max(position?2:.1,(hi-lo)*.12),min=lo-padding,max=hi+padding;
  const x=t=>left+t/selectedEpisode.duration*(w-left-right),y=(v,j)=>position&&j===5?h-bottom-v/100*(h-top-bottom):h-bottom-(v-min)/(max-min)*(h-top-bottom);
  ctx.clearRect(0,0,w,h);ctx.font='10px Segoe UI';ctx.lineWidth=1;
  for(let i=0;i<4;i++){const v=min+(max-min)*i/3,py=y(v,0);ctx.strokeStyle='#edf0f4';ctx.beginPath();ctx.moveTo(left,py);ctx.lineTo(w-right,py);ctx.stroke();ctx.fillStyle='#9aa5b2';ctx.textAlign='right';ctx.fillText(v.toFixed(Math.abs(max-min)<3?2:0),left-8,py+3);if(position&&joints.includes(5)){ctx.textAlign='left';ctx.fillText((100*i/3).toFixed(0)+'%',w-right+6,py+3);}}
  for(let i=0;i<5;i++){const t=selectedEpisode.duration*i/4;ctx.fillStyle='#9aa5b2';ctx.textAlign='center';ctx.fillText(t.toFixed(1)+'s',x(t),h-8);}
  const cs=Number($('crop-start').value),ce=Number($('crop-end').value);
  if(cs>=0&&ce>cs){ctx.fillStyle='#edf6f1';ctx.fillRect(x(cs),top,x(ce)-x(cs),h-top-bottom);}
  for(const value of [cs,ce]){if(Number.isFinite(value)){ctx.fillStyle='#6caa96';ctx.fillRect(x(value)-3,top,6,12);ctx.fillRect(x(value)-.5,top+12,1,h-top-bottom-12);}}
  series.forEach(s=>{ctx.strokeStyle=jointColors[s.j];ctx.lineWidth=s.dashed?1:1.7;ctx.setLineDash(s.dashed?[5,4]:[]);ctx.beginPath();let connected=false;s.values.forEach((v,i)=>{if(!Number.isFinite(v)){connected=false;return;}if(connected)ctx.lineTo(x(ts[i]),y(v,s.j));else ctx.moveTo(x(ts[i]),y(v,s.j));connected=true;});ctx.stroke();});
  ctx.setLineDash([]);ctx.strokeStyle='#425a73';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x(reviewTime),top);ctx.lineTo(x(reviewTime),h-bottom);ctx.stroke();
  ctx.fillStyle='#96a2b1';ctx.textAlign='left';ctx.fillText(position?'°':signal?.unit||'',left-25,12);
  $('signal-hint').textContent=position?'每个关节固定颜色；实线是从臂实测，虚线是示教目标。夹爪采用右侧百分比坐标。':`${signal?.source||'此字段未记录'} · ${ts.length} 个展示点`;
  $('signal-values').replaceChildren();
  let sampleIndex=-1;for(let i=0;i<ts.length;i++){if(ts[i]>reviewTime)break;sampleIndex=i;}
  series.filter(s=>!s.dashed).forEach(s=>{const el=document.createElement('span');el.className='joint-color-'+s.j;el.textContent=`${signal?.values[0]?.length!==6&&!position?signal?.label:jointNames[s.j]}：${showNumber(s.values[sampleIndex],position?1:2)}`;$('signal-values').append(el);});
}
$('plot-metric').addEventListener('change',scheduleDraw);new ResizeObserver(scheduleDraw).observe($('trajectory-chart'));
function renderFeatures(){
  const target=$('feature-inventory');target.replaceChildren();
  const note=document.createElement('p');note.textContent=library.signal_note;target.append(note);
  const available=new Set((library.signals||[]).map(s=>s.key));
  for(const [key,spec] of Object.entries(library.features||{})){const row=document.createElement('div');row.className='feature-row';const name=document.createElement('span');name.textContent=key;const detail=document.createElement('span');detail.textContent=`${spec.dtype} [${spec.shape}]`;row.append(name,detail);target.append(row);}
  for(const [key,label] of [['current_raw','电流'],['load_raw','负载'],['temperature','温度'],['torque_enabled','力矩开关'],['status','报警']]){
    const p=document.createElement('p');p.textContent=`${label}：${available.has('observation.sensors.'+key)?'可查看（数据集或原日志）':'该条历史数据未记录，不补零'}`;target.append(p);
  }
}
let cropDrag=null;
$('trajectory-chart').addEventListener('pointerdown',e=>{
  if(!selectedEpisode)return;const rect=e.currentTarget.getBoundingClientRect(),width=rect.width-87;
  const t=Math.max(0,Math.min(selectedEpisode.duration,(e.clientX-rect.left-45)/width*selectedEpisode.duration));
  const start=Number($('crop-start').value),end=Number($('crop-end').value),tolerance=selectedEpisode.duration*12/width;
  cropDrag=Math.abs(t-start)<tolerance?'crop-start':Math.abs(t-end)<tolerance?'crop-end':null;
  if(cropDrag){e.currentTarget.setPointerCapture(e.pointerId);e.preventDefault();pausePlayback();}
  else{pausePlayback();seek(t);}
});
$('trajectory-chart').addEventListener('pointermove',e=>{
  if(!cropDrag||!selectedEpisode)return;const rect=e.currentTarget.getBoundingClientRect();
  let t=Math.max(0,Math.min(selectedEpisode.duration,(e.clientX-rect.left-45)/(rect.width-87)*selectedEpisode.duration));
  t=Math.round(t*library.fps)/library.fps;
  if(cropDrag==='crop-start')t=Math.min(t,Number($('crop-end').value)-1/library.fps);
  else t=Math.max(t,Number($('crop-start').value)+1/library.fps);
  $(cropDrag).value=t.toFixed(6);updateCropHint();
});
for(const name of ['pointerup','pointercancel'])$('trajectory-chart').addEventListener(name,()=>{cropDrag=null;});

$('save-settings').addEventListener('click',()=>handle((async()=>{
  requireSavedResetPose();
  const cfg={...state.config,...resetSettingsValues(),...portableConfigValues(),top_camera:Number($('cfg-top').value),wrist_camera:Number($('cfg-wrist').value),preview_hz:Number($('cfg-preview').value),rerun:$('cfg-rerun').checked,dataset_home:$('cfg-datasets').value,checkpoint:$('cfg-checkpoint').value};
  if(!await confirmBindingChanges(cfg))return;
  acceptPortableConfig(await api('/api/config',{...cfg,_binding_confirmed:true}));
})()));
$('refresh-ports').addEventListener('click',()=>handle(showPortablePorts()));
document.querySelectorAll('[data-folder]').forEach(b=>b.addEventListener('click',()=>handle(api('/api/open-folder',{target:b.dataset.folder}))));
async function chooseFolder(inputId, purpose){
  if(!window.pywebview?.api){toast('原生目录选择器仅在独立桌面窗口中可用。');return;}
  const path=await window.pywebview.api.choose_folder();if(!path)return;
  if(purpose==='crop')$(inputId).value=path.replaceAll('\\','/')+'/'+library.name+`_ep${selectedEpisode.index+1}_trim`;
  else $(inputId).value=path;
}
$('record-target-mode').addEventListener('change',recordTargetChanged);
$('record-existing').addEventListener('change',recordTargetChanged);
$('record-browse').addEventListener('click',()=>{$('record-target-mode').value='existing';recordTargetChanged();$('record-existing').focus();});
$('model-browse').addEventListener('click',()=>handle(chooseFolder('cfg-checkpoint')));

let draggedCamera=null;
document.querySelectorAll('.camera-panel').forEach(panel=>{
  panel.querySelector('header').addEventListener('dragstart',e=>{draggedCamera=panel;e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain',panel.dataset.camera);});
  panel.addEventListener('dragover',e=>{if(draggedCamera&&draggedCamera!==panel){e.preventDefault();panel.classList.add('drag-over');}});
  panel.addEventListener('dragleave',()=>panel.classList.remove('drag-over'));
  panel.addEventListener('drop',e=>{e.preventDefault();panel.classList.remove('drag-over');if(!draggedCamera||draggedCamera===panel)return;const grid=$('camera-grid'),children=[...grid.children];grid.insertBefore(draggedCamera,children.indexOf(draggedCamera)<children.indexOf(panel)?panel.nextSibling:panel);saveLayout();});
  panel.addEventListener('dragend',()=>{draggedCamera=null;document.querySelectorAll('.drag-over').forEach(p=>p.classList.remove('drag-over'));});
  panel.querySelector('.maximize').addEventListener('click',()=>{const focused=panel.classList.toggle('focused');$('camera-grid').classList.toggle('focused',focused);if(focused)document.querySelectorAll('.camera-panel').forEach(p=>{if(p!==panel)p.classList.remove('focused');});});
});
function saveLayout(){try{localStorage.setItem('so101-layout',JSON.stringify({width:getComputedStyle(document.documentElement).getPropertyValue('--inspector-width'),order:[...$('camera-grid').children].map(p=>p.dataset.camera)}));}catch{}}
const splitter=$('inspector-splitter');let resizeStart=null;
splitter.addEventListener('pointerdown',e=>{resizeStart={x:e.clientX,width:document.querySelector('.inspector').getBoundingClientRect().width};splitter.setPointerCapture(e.pointerId);document.body.classList.add('resizing');});
splitter.addEventListener('pointermove',e=>{if(resizeStart)document.documentElement.style.setProperty('--inspector-width',Math.max(270,Math.min(470,resizeStart.width+resizeStart.x-e.clientX))+'px');});
function finishResize(){if(resizeStart){resizeStart=null;document.body.classList.remove('resizing');saveLayout();}}
splitter.addEventListener('pointerup',finishResize);splitter.addEventListener('pointercancel',finishResize);
splitter.addEventListener('keydown',e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();const width=document.querySelector('.inspector').getBoundingClientRect().width;document.documentElement.style.setProperty('--inspector-width',Math.max(270,Math.min(470,width+(e.key==='ArrowLeft'?10:-10)))+'px');saveLayout();}});
$('reset-layout').addEventListener('click',()=>{document.documentElement.style.removeProperty('--inspector-width');$('camera-grid').classList.remove('focused');document.querySelectorAll('.camera-panel').forEach(p=>p.classList.remove('focused'));$('camera-grid').append(document.querySelector('[data-camera="top"]'),document.querySelector('[data-camera="wrist"]'));try{localStorage.removeItem('so101-layout');}catch{}toast('已恢复默认布局。');});
try{const saved=JSON.parse(localStorage.getItem('so101-layout'));if(saved?.width&&/^\d+(\.\d+)?px$/.test(saved.width))document.documentElement.style.setProperty('--inspector-width',saved.width);for(const name of saved?.order||[]){const p=document.querySelector('.camera-panel[data-camera="'+(name==='top'?'top':'wrist')+'"]');if(p)$('camera-grid').append(p);}}catch{}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&state?.resources_active){e.preventDefault();if($('confirm-dialog').open)$('confirm-dialog').close('cancel');handle(command('stop'));return;}
  if(e.repeat||e.ctrlKey||e.metaKey||e.altKey||$('confirm-dialog').open)return;
  if(/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)||e.target.isContentEditable)return;
  if(e.code==='Space'&&state?.active&&['RECORDING','RESET'].includes(state.job.phase)){e.preventDefault();handle(command('advance',{phase:state.job.phase}));}
});
function installResetSettings(){
  const card=document.createElement('section');card.id='reset-profile-card';card.className='panel settings-card wide';
  card.innerHTML=`<h2>任务起点与加权复位</h2>
    <p class="field-help">任务起点是应用层的预设姿态，不是舵机中位或出厂零位。已有标定正常时可以直接编辑角度，无需重新标定；折叠收纳姿态也应单独记录，不能把六关节全设为 0 来代替。</p>
    <label>任务起点 JSON<div class="input-with-button"><input id="cfg-reference" spellcheck="false"><button id="browse-start-pose" class="button subtle" type="button">选择文件…</button></div></label>
    <div class="button-row"><button id="load-start-pose" class="button">读取所选起点（不连接设备）</button></div>
    <p id="reset-source" class="field-help"></p>
    <div class="form-grid"><label>转动关节基准目标速度 / °每秒<input id="cfg-reset-speed" type="number" min="0.1" max="20" step="0.1"></label><label>空夹爪目标速度 / 百分点每秒<input id="cfg-reset-gripper-speed" type="number" min="0.1" max="100" step="0.1"></label></div>
    <div class="table-scroll"><table class="reset-profile-table"><thead><tr><th>关节</th><th>目标起点（° / %）</th><th>复位速度比例</th><th>实际目标速率</th></tr></thead><tbody id="reset-profile-rows"></tbody></table></div>
    <p id="reset-pose-note" class="field-help">角度和夹爪百分比显示两位小数；未编辑的数值仍保留文件原精度，不改写原文件。</p>
    <p class="field-help">五个转动关节的目标速率 = 基准速度 × 各关节比例；夹爪单独按百分比。比例不是避障权重，当前方法不检查自碰撞；执行前仍需确认整条路线。不更改正式遥操或 ACT 任务段的速度。</p>
    <div class="button-row"><button id="copy-start-pose" class="button">另存编辑后的起点（不运动）</button><button id="save-reset-settings" class="button primary">保存复位设置</button></div>
    <p class="field-help">手工改角度后先“另存编辑后的起点”，再“保存复位设置”；原起点文件和标定不变。只选择或读取文件不会切换正在使用的起点，保存设置后用于下一会话，打开窗口不会自动复位。</p>`;
  document.querySelector('.settings-grid').prepend(card);
  jointNames.forEach((name,index)=>{
    const row=document.createElement('tr');
    row.innerHTML=`<td class="joint-color-${index}">${name}</td><td><input id="reset-pose-${index}" type="number" step="0.01" aria-label="${name} 起点"></td><td>${index<5?`<input id="reset-weight-${index}" type="number" min="0.05" max="1" step="0.05" aria-label="${name} 复位速度比例">`:'夹爪独立速度'}</td><td id="reset-rate-${index}">—</td>`;
    $('reset-profile-rows').append(row);
    $('reset-pose-'+index).addEventListener('input',()=>{
      if(resetPoseDraft[jointKeys[index]])resetPoseDraft[jointKeys[index]].edited=true;
      $('reset-pose-note').textContent='目标位置已编辑，尚未另存。请先另存起点，再保存复位设置；不会更改舵机标定。';
    });
    if(index<5)$('reset-weight-'+index).addEventListener('input',showResetRates);
  });
  for(const id of ['cfg-reset-speed','cfg-reset-gripper-speed'])$(id).addEventListener('input',showResetRates);
  $('load-start-pose').addEventListener('click',()=>handle(loadStartPose($('cfg-reference').value)));
  $('browse-start-pose').addEventListener('click',()=>handle((async()=>{
    if(!window.pywebview?.api?.choose_start_pose){toast('请在更新后的独立桌面窗口中使用原生文件选择器；浏览器中仍可粘贴 JSON 路径。');return;}
    const path=await window.pywebview.api.choose_start_pose($('cfg-reference').value);
    if(path){await loadStartPose(path);toast('起点已读取并核对标定；保存复位设置后用于下一会话，未连接设备。');}
  })()));
  $('save-reset-settings').addEventListener('click',()=>handle((async()=>{
    requireSavedResetPose();
    acceptPortableConfig(await api('/api/config',{...state.config,...resetSettingsValues()}));
  })()));
  $('copy-start-pose').addEventListener('click',()=>handle((async()=>{
    const positions=resetPosePositions();
    if(!await confirmOperation('另存应用层任务起点','将六个目标位置保存为工作区内的新 JSON，保留原起点和舵机标定不变。这里只保存，不执行动作；新目标及其复位路径仍需实际核对。'))return;
    const pose=await api('/api/reset-profile',{positions,source_path:loadedStartPosePath,confirmed:true});
    await loadStartPose(pose.path);toast('已另存新起点；点击“保存复位设置”后用于下一会话。');
  })()));
}
function resetSettingsValues(){return {reference_start_pose:$('cfg-reference').value.trim(),reset_arm_speed_deg_s:Number($('cfg-reset-speed').value),reset_gripper_speed_percent_s:Number($('cfg-reset-gripper-speed').value),reset_joint_weights:Object.fromEntries(jointKeys.slice(0,5).map((name,index)=>[name,Number($('reset-weight-'+index).value)]))};}
function showResetRates(){
  const cfg=resetSettingsValues();jointKeys.forEach((name,index)=>{
    const rate=index===5?cfg.reset_gripper_speed_percent_s:cfg.reset_arm_speed_deg_s*cfg.reset_joint_weights[name];
    $('reset-rate-'+index).textContent=showNumber(rate,2)+(index===5?' 百分点/s':' °/s');
  });
}
function populateResetSettings(cfg){
  $('cfg-reference').value=cfg.reference_start_pose;$('cfg-reset-speed').value=cfg.reset_arm_speed_deg_s;$('cfg-reset-gripper-speed').value=cfg.reset_gripper_speed_percent_s;
  jointKeys.slice(0,5).forEach((name,index)=>{$('reset-weight-'+index).value=cfg.reset_joint_weights[name];});
  showResetRates();
  if(cfg.reference_start_pose){handle(loadStartPose(cfg.reference_start_pose));}
  else{loadedStartPosePath='';resetPoseDraft={};jointKeys.forEach((n,i)=>{resetPoseDraft[n]={original:null,edited:false};$('reset-pose-'+i).value='';});$('reset-source').textContent='尚未配置任务起点。遥操/采集不要求固定起点；ACT 或复位前需按自己的标定填写六个目标并另存。';}
}
let resetPoseLoad=0,resetPoseDraft={},loadedStartPosePath='';
function resetPosePositions(){
  if(jointKeys.some(name=>!resetPoseDraft[name]))throw new Error('请先配置自己的从臂标定，并填写完整六关节起点。');
  return Object.fromEntries(jointKeys.map((name,index)=>{
    const draft=resetPoseDraft[name],input=$('reset-pose-'+index),text=input.value.trim();
    if(!text||!Number.isFinite(Number(text)))throw new Error(jointNames[index]+' 起点必须为有效数字，不能留空。');
    return [name,draft.edited||draft.original===null?Number(text):draft.original];
  }));
}
function requireSavedResetPose(){
  if(Object.values(resetPoseDraft).some(value=>value.edited))throw new Error('目标角度尚未另存：请先点击“另存编辑后的起点”，再保存设置。');
  if($('cfg-reference').value.trim()!==loadedStartPosePath)throw new Error('起点路径已变化，请先读取所选起点，核对角度后再保存。');
}
async function loadStartPose(path){
  const ticket=++resetPoseLoad,pose=await api('/api/reset-profile?path='+encodeURIComponent(path));if(ticket!==resetPoseLoad)return;
  loadedStartPosePath=pose.path;$('cfg-reference').value=pose.path;resetPoseDraft={};
  jointKeys.forEach((name,index)=>{
    const input=$('reset-pose-'+index),value=pose.positions[name];
    resetPoseDraft[name]={original:value,edited:false};input.value=value.toFixed(2);
    input.title='文件原值：'+String(value)+(index===5?' %':' °')+'；未编辑时保留原精度';
  });
  $('reset-pose-note').textContent='角度和夹爪百分比显示两位小数；未编辑的数值仍保留文件原精度，不改写原文件。';
  $('reset-source').textContent=pose.source_dataset?`当前起点来源：${pose.source_dataset}，第 ${pose.source_episode_zero_based+1} 条实测首帧。不是舵机中位。`:'当前起点来源：'+pose.source+'。保存目标不代表复位路径已通过碰撞验证。';
}
installResetSettings();
installFeedbackHistory();
installJointControls();
initPortableSettings();
window.platformRequestClose=()=>handle((async()=>{
  if(!state?.resources_active){if(window.pywebview?.api)await window.pywebview.api.close_window();return;}
  if(await confirmOperation('停止后关闭','当前任务仍在运行。将先请求正常停止并保存，待后台确认退出后再关闭窗口。不会强杀进程，也不会自动释放从臂力矩。')){
    await command('stop');
    while(state?.resources_active){await new Promise(resolve=>setTimeout(resolve,500));await poll();}
    if(window.pywebview?.api)await window.pywebview.api.close_window();
  }
})());
jointNames.forEach((name,index)=>{
  const row=document.createElement('tr');row.id='joint-'+index;const title=document.createElement('td');title.innerHTML=`<span class="joint-index">${index+1}</span><span class="joint-name"></span>`;title.querySelector('.joint-name').textContent=name;row.append(title);
  for(const key of ['state','target','temp','torque','status','current']){const cell=document.createElement('td');cell.dataset.col=key;cell.textContent='—';row.append(cell);}$('joint-table').append(row);
  $('jog-joint').add(new Option(name,index));
  const label=document.createElement('label');label.className='joint-color-'+index;const input=document.createElement('input');input.type='checkbox';input.value=index;input.checked=true;input.addEventListener('change',scheduleDraw);label.append(input,document.createTextNode(name));$('joint-checkboxes').append(label);
});
setView('capture');handle(poll());setInterval(poll,750);setInterval(updatePhase,100);setInterval(()=>{handle(refreshFrame('top'));handle(refreshFrame('wrist'));},200);
