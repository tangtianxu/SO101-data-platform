"""Optional native WebView QA using synthetic JPEGs, with backend offline-locked.

No device calls, job requests or saved user configuration changes are made.
The renderer is restored to its real offline state before this function returns.
"""
import time

from common import atomic_json


def run(window,request,output):
    before=request('api/state')
    if not before['offline'] or before['resources_active']:
        raise RuntimeError('相机 UI 验收只允许在无会话的离线后台执行')
    script=r"""
window.__cameraUIQA=null;
(async()=>{
  const savedState=state,savedFetch=window.fetch,savedPollBusy=pollBusy;
  const savedInputs=[...document.querySelectorAll('#joint-control-panel input')].map(input=>[input,input.value,input.checked,{...input.dataset}]);
  const checks={hardware_accessed:false,errors:[]},samples={};
  const required=(value,label)=>{if(!value)throw Error(label);};
  pollBusy=true;
  try{
    setView('control');
    const profileDeadline=Date.now()+3000;
    while(!jointControlProfile&&Date.now()<profileDeadline)await new Promise(resolve=>setTimeout(resolve,50));
    required(!!jointControlProfile,'software-limit profile loaded');
    const unchanged=softwareLimitPlan();
    checks.software_limits={six_joints:Object.keys(unchanged.limits).length===6,
      unchanged_precision:jointControlProfile.joints.every(j=>unchanged.limits[j.name].min===j.min&&unchanged.limits[j.name].max===j.max)};
    const elbowMax=$('software-max-2');elbowMax.value=String(jointControlProfile.joints[2].calibrated_max+1);elbowMax.dataset.dirty='true';
    try{softwareLimitPlan();checks.software_limits.widening_rejected=false;}catch{checks.software_limits.widening_rejected=true;}
    applyJointProfile(jointControlProfile);
    required(Object.values(checks.software_limits).every(Boolean),'software-limit precision/validation');
    for(const [name,color] of [['top','#dcefeb'],['wrist','#e0eaf8']]){
      const canvas=document.createElement('canvas');canvas.width=640;canvas.height=480;
      const context=canvas.getContext('2d');context.fillStyle=color;context.fillRect(0,0,640,480);
      context.fillStyle='#18394a';context.font='24px sans-serif';context.fillText('OFFLINE UI TEST - '+name.toUpperCase(),32,160);
      context.font='18px sans-serif';context.fillText('SYNTHETIC IMAGE / NO DEVICE CONNECTED',32,200);
      samples[name]=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg'));
      required(samples[name]?.type==='image/jpeg','native JPEG encoder');
    }
    const now=Date.now()/1000,owner='offline-ui-camera';
    const previewJob={id:owner,kind:'preview',phase:'PREVIEW',message:'OFFLINE UI TEST'};
    const cameraState={...savedState,offline:false,active:false,job:null,preview_active:true,
      preview_job:previewJob,resources_active:true,camera_active:true,camera_owner_id:owner,
      frame_owner:owner,frames:{top:now,wrist:now},telemetry:{}};
    state=cameraState;updateControls();updatePhase();
    state={...cameraState,job:{id:'failed-control',kind:'control',phase:'ERROR',message:'TEST PREFLIGHT FAILURE'}};updatePhase();
    checks.robot_error_not_hidden_by_camera=$('phase-message').textContent.includes('TEST PREFLIGHT FAILURE')&&document.querySelector('.phase-card').classList.contains('error');
    required(checks.robot_error_not_hidden_by_camera,'robot error remains visible with preview');
    state={...cameraState,active:true,job:{id:'test-control',kind:'control',phase:'CONTROL'},
      telemetry:{state:[0,0,0,0,0,50],health:{torque_enabled:Object.fromEntries(jointKeys.map(n=>[n,0]))}}};
    $('target-check-0').checked=true;$('target-position-0').value='0';updateControls();
    checks.execute_requires_torque=$('joint-execute').disabled&&$('joint-execution-note').textContent.includes('力矩未全部开启');
    state.telemetry.health.torque_enabled=Object.fromEntries(jointKeys.map(n=>[n,1]));updateControls();
    checks.execute_after_hold=!$('joint-execute').disabled;
    $('target-position-0').value='999';updateControls();
    checks.bad_target_explained=$('joint-execute').disabled&&$('joint-execution-note').textContent.includes('超出软件限位');
    required(checks.execute_requires_torque&&checks.execute_after_hold&&checks.bad_target_explained,'execute button reasons');
    $('target-check-0').checked=false;$('target-position-0').value='';
    state=cameraState;updateControls();updatePhase();
    checks.camera_first={monitor_enabled:!$('monitor-device').disabled,control_enabled:!$('connect-control').disabled,
      camera_close_enabled:!$('preview-cameras').disabled,global_stop_enabled:!$('stop-all').disabled};
    required(Object.values(checks.camera_first).every(Boolean),'camera-first buttons');
    for(const kind of ['monitor','control']){
      state={...cameraState,active:true,job:{id:'offline-ui-robot',kind,phase:kind==='monitor'?'MONITOR':'CONTROL',message:'OFFLINE UI TEST'}};
      updateControls();updatePhase();
      checks[kind+'_with_camera']={camera_close_enabled:!$('preview-cameras').disabled,
        disconnect_enabled:!$('disconnect-device').disabled,second_robot_blocked:$('monitor-device').disabled&&$('connect-control').disabled,
        global_stop_enabled:!$('stop-all').disabled};
      required(Object.values(checks[kind+'_with_camera']).every(Boolean),kind+' coexistence');
      state={...state,preview_active:false,preview_job:null,camera_active:false,camera_owner_id:null,frame_owner:null,frames:{}};
      updateControls();
      checks[kind+'_first_camera_enabled']=!$('preview-cameras').disabled;
      required(checks[kind+'_first_camera_enabled'],kind+'-first preview button');
    }
    state=cameraState;updateControls();updatePhase();
    let frameFetches=0;
    window.fetch=async(input,options)=>{
      if(typeof input==='string'&&input.startsWith('/frame/')){
        frameFetches++;
        const name=input.split('/frame/')[1].split('?')[0];
        return new Response(samples[name],{status:200});
      }
      return savedFetch(input,options);
    };
    checks.images={};
    for(const name of ['top','wrist']){
      await refreshFrame(name);
      const image=$('frame-'+name);
      checks.images[name]={width:image.naturalWidth,height:image.naturalHeight,visible:!image.hidden,blob:image.src.startsWith('blob:')};
      required(image.naturalWidth===640&&image.naturalHeight===480&&!image.hidden&&image.src.startsWith('blob:'),name+' JPEG/blob decode under page CSP');
    }
    state={...cameraState,camera_owner_id:'different-session'};
    const oldCount=frameFetches;await refreshFrame('top');
    checks.stale_frame_blocked=frameFetches===oldCount;
    required(checks.stale_frame_blocked,'stale camera frame');
    checks.passed=true;
  }catch(error){checks.passed=false;checks.errors.push(String(error.stack||error));}
  finally{
    window.fetch=savedFetch;
    for(const [input,value,checked,data] of savedInputs){input.value=value;input.checked=checked;for(const key of Object.keys(input.dataset))delete input.dataset[key];Object.assign(input.dataset,data);}
    for(const name of ['top','wrist']){
      const image=$('frame-'+name);image.onload=null;image.onerror=null;image.removeAttribute('src');
      image.hidden=true;image.nextElementSibling.hidden=false;
      if(frameUrls[name])URL.revokeObjectURL(frameUrls[name]);
      delete frameUrls[name];delete frameJob[name];frameBusy[name]=false;
    }
    state=savedState;pollBusy=savedPollBusy;updateControls();updatePhase();
    document.querySelector('main').scrollTop=0;document.querySelector('.inspector-content').scrollTop=0;
    checks.restored_offline=state.offline&&!state.resources_active;
    window.__cameraUIQA=checks;
  }
})();true
"""
    # Layout class names differ between old/new native snapshots: avoid making
    # a cleanup scroll optionality failure hide an otherwise completed QA report.
    script=script.replace("document.querySelector('.inspector-content').scrollTop=0;",
                          "if(document.querySelector('.inspector-content'))document.querySelector('.inspector-content').scrollTop=0;")
    window.evaluate_js(script)
    deadline=time.monotonic()+15
    while True:
        checks=window.evaluate_js('window.__cameraUIQA')
        if checks is not None:break
        if time.monotonic()>deadline:raise TimeoutError('离线 JPEG/按钮验收未完成')
        time.sleep(.1)
    after=request('api/state')
    checks.update(build=after['identity']['build'],backend_offline=after['offline'],
                  backend_has_no_workers=not after['resources_active'],backend_job=after['job'],
                  backend_preview_job=after['preview_job'])
    checks['passed']=bool(checks.get('passed') and checks['restored_offline'] and checks['backend_offline']
                          and checks['backend_has_no_workers'] and after['job'] is None and after['preview_job'] is None)
    atomic_json(output,checks)
    if not checks['passed']:raise RuntimeError('离线相机 UI 验收失败，请查看 '+str(output))
    return checks
