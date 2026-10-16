'use strict';
let portableProfileKey='';
function initPortableSettings(){
  const grid=document.querySelector('.settings-grid');
  const card=document.createElement('section');card.className='panel settings-card wide';card.id='portable-settings';
  card.innerHTML=`<h2>首次配置与运行环境</h2><p class="field-help">保存配置不会连接设备。先配置工作区与 LeRobot Python，再绑定主从臂和各自标定；模型与任务起点只在 ACT/复位时需要。更换工作区或端口后需关闭并重新打开本平台。</p>
    <label>工作区目录<div class="input-with-button"><input id="cfg-workspace"><button class="button" data-resource="workspace" data-field="cfg-workspace">选择目录</button></div></label>
    <label>LeRobot Python 解释器<div class="input-with-button"><input id="cfg-python" placeholder="选择已有隔离环境的 python.exe"><button class="button" data-resource="python" data-field="cfg-python">选择文件</button></div></label>
    <div class="form-grid"><label>日志目录<input id="cfg-log-home"></label><label>ACT 回流目录<input id="cfg-feedback-home"></label></div>
    <label>主臂标定 JSON<div class="input-with-button"><input id="cfg-leader-calibration"><button class="button" data-resource="calibration" data-field="cfg-leader-calibration">选择文件</button></div></label>
    <div class="form-grid"><label>主臂端口（auto 按 USB 身份查找）<input id="cfg-leader-port"></label><label>从臂端口（auto 按 USB 身份查找）<input id="cfg-follower-port"></label></div>
    <div class="form-grid"><label>本地服务端口<input id="cfg-server-port" type="number" min="1024" max="65535"></label><label>夹爪单独报警 32<select id="cfg-overload"><option value="stop">停止（新设备默认）</option><option value="log_gripper_only">仅记录（需自行验收）</option></select></label></div>
    <div class="button-row"><button id="check-environment" class="button">检查所选环境（不连接设备）</button></div><pre id="environment-result" class="field-help">环境尚未检查；此处不会预热 GPU、打开相机或串口。</pre><div id="configuration-readiness" class="field-help"></div>`;
  grid.prepend(card);
  for(const id of ['cfg-leader','cfg-follower','cfg-calibration'])$(id).readOnly=false;
  $('cfg-leader').placeholder='主臂 USB serial_number；未配置可留空';$('cfg-follower').placeholder='从臂 USB serial_number；不能与主臂相同';
  const calButton=document.createElement('button');calButton.className='button';calButton.textContent='选择从臂标定 JSON';calButton.dataset.resource='calibration';calButton.dataset.field='cfg-calibration';$('cfg-calibration').parentElement.append(calButton);
  const pathHeading=$('cfg-datasets').closest('section').querySelector('h2');pathHeading.textContent='工作区与只读资源路径';
  $('cfg-top').max='63';$('cfg-wrist').max='63';
  document.querySelectorAll('[data-resource]').forEach(button=>button.addEventListener('click',()=>handle((async()=>{
    if(!window.pywebview?.api?.choose_resource){toast('浏览器中请粘贴路径；原生独立窗口支持文件/目录选择。');return;}
    const field=$(button.dataset.field),path=await window.pywebview.api.choose_resource(button.dataset.resource,field.value);
    if(path){field.value=path;field.dispatchEvent(new Event('change'));}
  })())));
  $('cfg-workspace').addEventListener('change',()=>{
    const base=$('cfg-workspace').value.replace(/[\\/]$/,'');
    if(base&&base!==state?.config.workspace_root){$('cfg-datasets').value=base+'/datasets';$('cfg-log-home').value=base+'/logs/data_platform';$('cfg-feedback-home').value=base+'/data_feedback';}
  });
  $('check-environment').addEventListener('click',()=>handle((async()=>{
    $('environment-result').textContent='检查中；使用已保存的解释器配置…';
    const result=await api('/api/environment');
    $('environment-result').textContent=`Python：${result.python}\n版本：${result.version}\n`+Object.entries(result.modules).map(([name,found])=>`${found?'✓':'缺少'} ${name}`).join('\n')+'\n仅检查模块是否可发现，未连接设备或预热 GPU；实际兼容性仍需逐项验收。';
  })()));
  const banner=document.createElement('section');banner.className='notice';banner.id='portable-notice';banner.textContent='首次使用可先进入“配置”绑定自己的工作区、解释器、设备和标定。未配置模型不影响打开界面或准备采集。';document.querySelector('main').prepend(banner);
}
function populatePortableConfig(cfg){
  const fields={'cfg-workspace':'workspace_root','cfg-python':'python','cfg-log-home':'log_home','cfg-feedback-home':'feedback_home','cfg-leader-calibration':'leader_calibration','cfg-leader-port':'leader_port','cfg-follower-port':'follower_port','cfg-server-port':'server_port','cfg-overload':'gripper_overload_policy'};
  Object.entries(fields).forEach(([id,key])=>{$(id).value=cfg[key]??'';});
}
function portableConfigValues(){
  return {workspace_root:$('cfg-workspace').value.trim(),python:$('cfg-python').value.trim(),
    leader_usb_serial:$('cfg-leader').value.trim(),follower_usb_serial:$('cfg-follower').value.trim(),
    leader_port:$('cfg-leader-port').value.trim()||'auto',follower_port:$('cfg-follower-port').value.trim()||'auto',
    calibration:$('cfg-calibration').value.trim(),leader_calibration:$('cfg-leader-calibration').value.trim(),
    log_home:$('cfg-log-home').value.trim(),feedback_home:$('cfg-feedback-home').value.trim(),
    server_port:Number($('cfg-server-port').value),gripper_overload_policy:$('cfg-overload').value};
}
async function confirmBindingChanges(cfg){
  const keys=['leader_usb_serial','follower_usb_serial','leader_port','follower_port','calibration','leader_calibration'];
  const changed=keys.some(k=>cfg[k]!==state.config[k]);
  if(!changed)return true;
  return await confirmOperation('保存设备绑定（不连接、不运动）','请核对主从角色、USB 身份和各自标定文件。更换从臂标定会清除旧软件限位与未更换的旧起点关联，旧文件不会删除。不要使用其他机械臂的标定或靠放宽限位跳过检查。');
}
function acceptPortableConfig(saved){
  const {_restart_required,...cfg}=saved;
  if(_restart_required){state.restart_required=true;$('portable-notice').textContent='配置已保存。请关闭本平台后重新打开，新的工作区/端口才会生效；当前已禁止新操作。';updateControls();toast('配置已保存，需要重启本平台；未连接设备。');return;}
  state.config=cfg;portableProfileKey='';populateConfig(cfg);toast('配置已保存，未连接或移动设备。');
}
function applyPortableState(){
  if(!state)return;
  const caps=state.capabilities||{};
  const modes={'preview-cameras':'preview','monitor-device':'monitor','connect-control':'control','prepare-record':'record','prepare-teleop':'teleop','prepare-act':'act','prepare-replay':'replay','act-reset-only':'control'};
  Object.entries(modes).forEach(([id,kind])=>{if(caps[kind]&&!caps[kind].ready&&!(id==='preview-cameras'&&state.preview_active)){$(id).disabled=true;$(id).title=caps[kind].reason+'；请到配置页完成设置';}});
  if(!caps.control?.ready)for(const id of ['quick-hold','quick-release'])$(id).disabled=true;
  $('configuration-readiness').replaceChildren(...Object.entries(caps).map(([kind,item])=>{const p=document.createElement('p');p.textContent=`${kind}：${item.ready?'配置检查通过（不代表设备已验收）':item.reason}`;return p;}));
  if(state.restart_required){$('portable-notice').textContent='配置已保存，需关闭并重新打开本平台；当前仅允许停止或正常关闭。';return;}
  $('portable-notice').hidden=!!caps.control?.ready;
  const key=state.config.calibration;
  if(key&&portableProfileKey!==key){portableProfileKey=key;handle(api('/api/joint-control-profile').then(applyJointProfile));}
}
async function showPortablePorts(){
  const ports=await api('/api/ports');$('ports-list').replaceChildren();
  if(!ports.length){$('ports-list').textContent='未枚举到串口；没有打开或连接任何串口。';return;}
  ports.forEach(p=>{
    const item=document.createElement('div'),label=document.createElement('span');label.textContent=`${p.role} · ${p.port} · ${p.serial||'无唯一序列号'} · ${p.name}`;item.append(label);
    for(const [role,name] of [['leader','设为主臂'],['follower','设为从臂']]){const button=document.createElement('button');button.className='button subtle';button.textContent=name;button.disabled=!p.serial;button.addEventListener('click',()=>{$('cfg-'+role).value=p.serial;$('cfg-'+role+'-port').value='auto';toast('已填入，点击保存配置并核对角色后才生效；未连接设备。');});item.append(button);}
    $('ports-list').append(item);
  });
}
