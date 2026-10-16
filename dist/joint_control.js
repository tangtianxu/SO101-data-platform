'use strict';
let jointControlProfile=null,nativeRuntimeIdentity=null,nativeIdentityBusy=false;
function installJointControls(){
  const panel=document.createElement('section');panel.id='joint-control-panel';panel.className='panel joint-control-panel hidden';
  panel.innerHTML=`<header class="panel-header"><div><h2>关节目标控制</h2><span class="field-help">勾选关节，填写目标与速度，点击执行后才运动。</span></div><span id="joint-target-status" class="badge neutral">未连接设备</span></header><div class="target-toolbar"><button id="joint-select-all" class="button subtle">全选</button><button id="joint-select-none" class="button subtle">清空选择</button><button id="joint-fill-current" class="button">填入选中关节的当前读数</button><span id="joint-selected-count" class="muted">已选 0 个关节</span></div><div class="table-scroll"><table class="joint-target-table"><thead><tr><th>执行</th><th>关节</th><th>实测位置</th><th>目标位置</th><th>目标速度</th><th>标定范围</th></tr></thead><tbody id="joint-target-rows"></tbody></table></div><div class="target-actions"><button id="joint-execute" class="button primary hardware" disabled>执行选中关节</button><button id="joint-stop" class="button danger-text" disabled>停止 · Esc</button><span id="joint-execution-note" class="field-help">离线可编辑，不会连接或移动机械臂。</span></div><p class="panel-foot">前五关节为 °、°/秒；夹爪为 %、百分点/秒。速度是目标插值速率，不是 FD 原始寄存器值。未勾选关节保持执行开始时的位置；路径不具备自动避障能力。</p>`;
  document.querySelector('.workspace-main').prepend(panel);
  panel.querySelector('thead th:last-child').textContent='软件限位';
  panel.querySelector('.field-help').textContent='勾选关节，拖动滑条或输入数值；点击执行后才运动。拖动不会实时下发。';
  jointNames.forEach((label,index)=>{
    const row=document.createElement('tr');row.innerHTML=`<td><input id="target-check-${index}" type="checkbox" aria-label="执行${label}"></td><td class="joint-color-${index}">${label}</td><td id="target-measured-${index}">—</td><td><div class="target-input"><input id="target-position-${index}" type="number" step="0.01" placeholder="输入目标" aria-label="${label}目标位置"><span>${index===5?'%':'°'}</span></div></td><td><div class="target-input"><input id="target-speed-${index}" type="number" step="0.1" min="0.1" aria-label="${label}目标速度"><span>${index===5?'百分点/s':'°/s'}</span></div></td><td id="target-limits-${index}" class="muted">读取配置中</td>`;
    $('joint-target-rows').append(row);
    for(const field of ['position','speed']){
      const number=$('target-'+field+'-'+index),cell=number.closest('td');
      cell.classList.add('target-editor-cell');
      const slider=document.createElement('input');slider.type='range';slider.id='target-'+field+'-slider-'+index;
      slider.className='target-slider';slider.step=field==='position'?'0.1':'0.1';slider.disabled=true;
      slider.setAttribute('aria-label',label+(field==='position'?'目标位置滑条':'目标速度滑条'));
      slider.title='只编辑目标；点击执行后才运动';cell.append(slider);
      slider.addEventListener('input',()=>{
        number.value=slider.value;$('target-check-'+index).checked=true;syncTargetSlider(index,field);updateJointControls();
      });
      number.addEventListener('input',()=>syncTargetSlider(index,field));
    }
    row.querySelectorAll('input:not([type=range])').forEach(input=>input.addEventListener('input',()=>updateJointControls()));
  });
  for(const [id,checked] of [['joint-select-all',true],['joint-select-none',false]])$(id).addEventListener('click',()=>{jointKeys.forEach((_,i)=>{$('target-check-'+i).checked=checked;});updateJointControls();});
  $('joint-fill-current').addEventListener('click',()=>{
    if(!state?.active||state.job.kind!=='control'||!Array.isArray(state.telemetry?.state))return;
    jointKeys.forEach((_,i)=>{if($('target-check-'+i).checked){$('target-position-'+i).value=Number(state.telemetry.state[i]).toFixed(2);syncTargetSlider(i,'position');}});updateJointControls();
  });
  $('joint-execute').addEventListener('click',()=>handle((async()=>{
    const payload=jointTargetPlan();
    await command('joint_target',{...payload,confirmed:true});
  })()));
  $('joint-stop').addEventListener('click',()=>handle(command('stop')));
  const version=document.querySelector('.sidebar-bottom .version');version.id='runtime-version';version.title='点击查看版本、实例与正式入口';version.tabIndex=0;
  version.addEventListener('click',()=>{setView('settings');$('runtime-identity-card').scrollIntoView({behavior:'smooth'});});
  const runtime=document.createElement('section');runtime.id='runtime-identity-card';runtime.className='panel settings-card wide';runtime.innerHTML='<h2>当前版本与正式入口</h2><div id="runtime-details" class="runtime-details">等待后台身份核对</div><p class="field-help">所有入口调用同一 当前工作区配置和同一份源码。前端、后台、原生窗口需为同一构建；源文件更新后需先停止并重开，不混用新旧控制代码。</p>';
  document.querySelector('.settings-grid').append(runtime);
  const notice=document.createElement('section');notice.id='runtime-mismatch';notice.className='notice runtime-warning hidden';notice.textContent='当前窗口与后台版本不一致，已禁止新操作。请正常停止后，从正式入口重新打开。';document.querySelector('main').prepend(notice);
  installSoftwareLimits();
  // Loaded after a follower calibration is explicitly configured, not on first-run boot.
}
function syncTargetSlider(index,field){
  const number=$('target-'+field+'-'+index),slider=$('target-'+field+'-slider-'+index);
  if(!slider||slider.disabled)return;
  // Never round/clamp the numeric draft: an invalid typed target must stay visible and fail validation.
  const draft=number.value.trim(),value=draft===''?NaN:Number(draft);
  const fallback=field==='position'?state?.telemetry?.state?.[index]:undefined;
  if(Number.isFinite(value))slider.value=String(value);
  else if(Number.isFinite(fallback))slider.value=String(fallback);
  slider.setAttribute('aria-valuetext',draft===''?'尚未设置目标':draft+(index===5?(field==='position'?' %':' 百分点/秒'):(field==='position'?' °':' °/秒')));
}
function applyJointProfile(value){
    jointControlProfile=value;value.joints.forEach((joint,i)=>{
      $('target-position-'+i).min=String(Math.ceil(joint.min*100)/100);$('target-position-'+i).max=String(Math.floor(joint.max*100)/100);
      $('target-speed-'+i).max=joint.speed_max;$('target-speed-'+i).value=joint.default_speed;
      for(const field of ['position','speed']){
        const slider=$('target-'+field+'-slider-'+i);
        slider.min=field==='position'?$('target-position-'+i).min:joint.speed_min;
        slider.max=field==='position'?$('target-position-'+i).max:joint.speed_max;
        slider.disabled=false;syncTargetSlider(i,field);
      }
      $('target-limits-'+i).textContent=`${joint.min.toFixed(2)} ～ ${joint.max.toFixed(2)} ${joint.unit}`;
      $('software-baseline-'+i).textContent=`${joint.calibrated_min.toFixed(2)} ～ ${joint.calibrated_max.toFixed(2)} ${joint.unit}`;
      for(const side of ['min','max'])setLimitInput($('software-'+side+'-'+i),joint[side]);
    });updateJointControls();
}
function jointTargetPlan(){
  if(!jointControlProfile)throw Error('关节标定配置尚未读取');const joints={};
  jointControlProfile.joints.forEach((joint,i)=>{
    if(!$('target-check-'+i).checked)return;
    const position=$('target-position-'+i).value.trim(),speed=$('target-speed-'+i).value.trim();
    if(!position||!speed||!Number.isFinite(Number(position))||!Number.isFinite(Number(speed)))throw Error(jointNames[i]+'：请填写有效的目标位置和速度');
    if(Number(position)<joint.min||Number(position)>joint.max)throw Error(jointNames[i]+` 目标超出软件限位（${joint.min.toFixed(2)} ～ ${joint.max.toFixed(2)}）`);
    if(Number(speed)<joint.speed_min||Number(speed)>joint.speed_max)throw Error(jointNames[i]+' 目标速度超出输入范围');
    joints[joint.name]={target:Number(position),speed:Number(speed)};
  });
  if(!Object.keys(joints).length)throw Error('请至少勾选一个关节');
  return {calibration_id:jointControlProfile.calibration_id,limits_id:jointControlProfile.limits_id,joints};
}
function updateJointControls(){
  if(!$('joint-control-panel'))return;
  const count=jointKeys.filter((_,i)=>$('target-check-'+i).checked).length;
  const control=!!(state?.active&&state.job?.kind==='control'&&state.job.phase==='CONTROL');
  const health=normalizeHealth(state?.telemetry),holding=jointKeys.every(n=>health.torque[n]===1);
  const blocked=!state||state.offline||runtimeMismatch||state.identity?.source_changed||state.restart_required;
  let invalid='';if(count&&jointControlProfile){try{jointTargetPlan();}catch(error){invalid=error.message;}}
  $('joint-selected-count').textContent='已选 '+count+' 个关节';
  $('joint-execute').disabled=!!(blocked||!control||!holding||!count||!jointControlProfile||invalid||requestBusy||state?.job?.target_pending);
  $('joint-fill-current').disabled=!control||!Array.isArray(state?.telemetry?.state)||!count;
  $('joint-stop').disabled=!(state?.active&&state.job?.kind==='control');
  const moving=state?.job?.phase==='JOINT_MOVING'||state?.job?.target_pending;
  $('joint-target-status').textContent=blocked?'离线 / 版本锁定':moving?'正在执行':control?(holding?'已保持 · 可执行':'已连接 · 需开启保持'):'未连接基本控制';
  jointKeys.forEach((_,i)=>{$('target-measured-'+i).textContent=showNumber(state?.telemetry?.state?.[i],2);});
  const result=state?.job?.target_result;
  const note=blocked?'离线或版本锁定，可编辑但不能执行。':moving?'正在运动；停止或 Esc 结束本次控制并保持当前位置。':!control?(state?.job?.phase==='ERROR'?'基本控制未连接：'+state.job.message:'请先连接「从臂，基本控制」。'):!holding?'力矩未全部开启：先可靠支撑，再点击「开启力矩 / 保持」。':!count?'请勾选至少一个关节。':invalid||result?.error||(result?.outcome==='reached'?'已由反馈确认到位；保持力矩。':result?.outcome==='target_sent_not_reached'?'目标已下发，尚未确认到位；请检查实测值与日志。':'就绪；点击执行才运动。');
  $('joint-execution-note').textContent=note;$('joint-execute').title=note;
  $('save-software-limits').disabled=!!(state?.active||requestBusy||runtimeMismatch||state?.identity?.source_changed||!jointControlProfile);
}
function setLimitInput(input,value){input.value=Number(value).toFixed(2);input.dataset.exact=String(value);input.dataset.dirty='false';}
function limitInputValue(input){return Number(input.dataset.dirty==='false'?input.dataset.exact:input.value.trim()===''?NaN:input.value);}
function softwareLimitPlan(){
  if(!jointControlProfile)throw Error('限位配置尚未读取');const limits={};
  jointControlProfile.joints.forEach((joint,i)=>{
    const min=limitInputValue($('software-min-'+i)),max=limitInputValue($('software-max-'+i));
    if(!Number.isFinite(min)||!Number.isFinite(max)||min>=max)throw Error(jointNames[i]+'：下限必须小于上限，且均为有效数字');
    if(min<joint.calibrated_min||max>joint.calibrated_max)throw Error(jointNames[i]+'：不能超出原标定区间；此操作不修改舵机内部限位');
    limits[joint.name]={min,max};
  });
  return {calibration_id:jointControlProfile.calibration_id,limits_id:jointControlProfile.limits_id,limits,confirmed:true};
}
function installSoftwareLimits(){
  const card=document.createElement('details');card.id='software-limits-card';card.className='software-limits-card';
  card.innerHTML='<summary>软件运动限位 · 仅基本控制</summary><div class="software-limits-body"><p class="field-help">只限制目标控制、点动和本页复位。不改原标定、舵机参数或角度零点；采集、遥操与 ACT 仍使用原边界。这里只能在原标定区间内收紧或恢复，不能借此放宽舵机限制。</p><div class="table-scroll"><table class="joint-target-table"><thead><tr><th>关节</th><th>软件下限</th><th>软件上限</th><th>原标定区间（只读）</th></tr></thead><tbody id="software-limit-rows"></tbody></table></div><div class="target-actions"><button id="restore-software-limits" class="button">填入原标定区间</button><button id="save-software-limits" class="button primary">保存软件限位（不运动）</button></div><p class="field-help">先结束机械臂会话再保存；相机预览可保留。保存后下次连接生效。保持当前位置、释放力矩不受内层软件限位阻拦；设置限位不具备避障功能。</p></div>';
  $('joint-control-panel').append(card);
  jointNames.forEach((label,i)=>{
    const row=document.createElement('tr');row.innerHTML=`<td class="joint-color-${i}">${label}</td><td><input id="software-min-${i}" type="number" step="0.01" aria-label="${label}软件下限"></td><td><input id="software-max-${i}" type="number" step="0.01" aria-label="${label}软件上限"></td><td id="software-baseline-${i}" class="muted">—</td>`;
    $('software-limit-rows').append(row);row.querySelectorAll('input').forEach(input=>input.addEventListener('input',()=>{input.dataset.dirty='true';}));
  });
  $('restore-software-limits').addEventListener('click',()=>{jointControlProfile?.joints.forEach((joint,i)=>{for(const side of ['min','max'])setLimitInput($('software-'+side+'-'+i),joint['calibrated_'+side]);});toast('已填入原标定区间，尚未保存。');});
  $('save-software-limits').addEventListener('click',()=>handle((async()=>{
    const plan=softwareLimitPlan();
    if(!await confirmOperation('保存基本控制软件限位','只更新 E 盘应用配置，不连接或移动机械臂，不写舵机内部限位或原标定。\n采集、遥操与 ACT 边界不变；下次连接基本控制生效。'))return;
    const saved=await api('/api/manual-limits',plan);state.config=saved.config;applyJointProfile(saved.profile);toast('软件限位已保存；未连接或移动设备。');
  })()));
}
async function updateRuntimeIdentity(identity){
  runtimeMismatch=!identity||identity.build!==htmlBuild;
  if(window.pywebview?.api?.get_identity&&!nativeRuntimeIdentity&&!nativeIdentityBusy){
    nativeIdentityBusy=true;try{nativeRuntimeIdentity=await window.pywebview.api.get_identity();}finally{nativeIdentityBusy=false;}
  }
  if(nativeRuntimeIdentity&&nativeRuntimeIdentity.build!==identity?.build)runtimeMismatch=true;
  if(nativeRuntimeIdentity&&nativeRuntimeIdentity.backend_instance!==identity?.instance)runtimeMismatch=true;
  $('runtime-mismatch').classList.toggle('hidden',!runtimeMismatch&&!identity?.source_changed);
  const version=$('runtime-version');version.textContent=identity?`v${identity.version} · ${identity.build}`:'版本未核实';
  const details=$('runtime-details');details.replaceChildren();
  for(const [name,value] of [['正式入口',identity?.formal_entry],['项目',identity?.project],['数据配置',identity?.state_dir],['界面构建',htmlBuild],['后台构建 / PID',identity?identity.build+' / '+identity.pid:'—'],['窗口构建 / PID',nativeRuntimeIdentity?nativeRuntimeIdentity.build+' / '+nativeRuntimeIdentity.pid:'浏览器 / 待核对'],['后台实例',identity?.instance],['源码状态',identity?.source_changed?'文件已更新，需停止后重开':'与启动时一致']]){
    const row=document.createElement('div'),label=document.createElement('span'),text=document.createElement('code');label.textContent=name;text.textContent=value||'—';row.append(label,text);details.append(row);
  }
}
