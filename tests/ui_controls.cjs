// Isolated DOM + fake HTTP only. No server, USB, camera, Python or model is started.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {JSDOM,VirtualConsole}=require('jsdom');
const root=process.env.SO101_UI_TEST_ROOT||path.resolve(__dirname,'..');
const keys=['shoulder_pan','shoulder_lift','elbow_flex','wrist_flex','wrist_roll','gripper'];
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(predicate){const end=Date.now()+3000;while(!predicate()){if(Date.now()>end)throw Error('UI state timed out');await pause(10);}}
const profile={calibration_id:'fake-cal',limits_id:'fake-limits',joints:keys.map((name,i)=>({name,min:i===5?0:-90,max:i===5?100:90,calibrated_min:i===5?0:-90,calibrated_max:i===5?100:90,speed_min:.1,speed_max:i===5?100:20,default_speed:i===5?15:5,unit:i===5?'%':'°'}))};
async function fixture(t){
  const errors=[],calls=[];let release=true,previewRelease=true;
  const snapshot={active:true,preview_active:false,resources_active:true,camera_active:false,frames:{},events:[],offline:false,restart_required:false,config:{calibration:'',dataset_home:'QA/datasets'},
    identity:{build:'__SO101_BUILD__',version:'test',source_changed:false},capabilities:Object.fromEntries(['control','teleop','record','preview','monitor'].map(k=>[k,{ready:true}])),
    job:{id:'QA-control-001',kind:'control',phase:'CONTROL'},telemetry:{state:[0,1,2,3,4,25],health:{torque_enabled:Object.fromEntries(keys.map(n=>[n,1]))}}};
  const vc=new VirtualConsole();vc.on('jsdomError',e=>errors.push(e.message));
  const dom=new JSDOM(fs.readFileSync(path.join(root,'dist/index.html'),'utf8'),{url:'http://127.0.0.1:19999/',runScripts:'outside-only',pretendToBeVisual:true,virtualConsole:vc});
  t.after(()=>{const uiErrors=Array.from(dom.window.__qaErrors||[]);dom.window.close();assert.deepEqual(errors,[]);assert.deepEqual(uiErrors,[]);});
  const w=dom.window,$=id=>w.document.getElementById(id),context=dom.getInternalVMContext();
  const run=code=>vm.runInContext(code,context);
  w.ResizeObserver=class{observe(){}disconnect(){}};
  w.HTMLElement.prototype.scrollIntoView=function(){};
  w.HTMLMediaElement.prototype.pause=function(){};w.HTMLMediaElement.prototype.load=function(){};
  const canvas=new Proxy({measureText:()=>({width:0})},{get:(o,k)=>o[k]||(()=>{})});w.HTMLCanvasElement.prototype.getContext=()=>canvas;
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
  w.HTMLDialogElement.prototype.close=function(value){this.returnValue=value;this.open=false;this.dispatchEvent(new w.Event('close'));};
  w.fetch=async(url,options={})=>{
    const body=options.body?JSON.parse(options.body):null;calls.push({url,body});let value={};
    if(url==='/api/state')value=structuredClone(snapshot);
    else if(url==='/api/joint-control-profile')value=structuredClone(profile);
    else if(url==='/api/command'){
      if(body.command==='stop_device'&&release){snapshot.active=false;snapshot.resources_active=snapshot.preview_active;}
      if(body.command==='stop_preview'&&previewRelease){snapshot.preview_active=false;snapshot.resources_active=snapshot.active;}
      if(body.command==='stop'){snapshot.active=false;snapshot.preview_active=false;snapshot.resources_active=false;}
    }else if(url==='/api/jobs'){
      assert.equal(snapshot.active,false,'new serial owner must wait until old owner exits');
      assert.equal(snapshot.preview_active,false,'task camera handoff must wait until preview exits');
      snapshot.job={id:'QA-teleop-002',kind:body.kind,phase:'SCENE_CHECK'};snapshot.active=true;snapshot.resources_active=true;value=snapshot.job;
    }
    return {ok:true,json:async()=>value};
  };
  for(const name of ['naming.js','history.js','joint_control.js','portable.js','app.js']){
    const file=path.join(root,'dist',name);if(!fs.existsSync(file))continue;
    let code=fs.readFileSync(file,'utf8');
    // Suppress only the polling boot line; test the actual installed handlers and all control functions.
    if(name==='app.js')code=code.replace(/^setView\('capture'\);handle\(poll\(\)\);setInterval\(poll,750\);.*$/m,'');
    vm.runInContext(code,context,{filename:name});
  }
  w.__snapshot=structuredClone(snapshot);w.__profile=structuredClone(profile);
  run('state=window.__snapshot;initialized=true;applyJointProfile(window.__profile);setView("control");updateControls();');
  await pause(0);calls.length=0;
  function render(){w.__snapshot=structuredClone(snapshot);run('state=window.__snapshot;updateControls();updatePhase();');}
  function input(id,value){$(id).value=String(value);$(id).dispatchEvent(new w.Event('input',{bubbles:true}));}
  function posted(){return calls.filter(c=>c.body);}
  return {w,$,run,snapshot,calls,render,input,posted,setRelease:value=>{release=value;},setPreviewRelease:value=>{previewRelease=value;}};
}

test('12 sliders edit targets locally, select joint and do not dispatch on drag',async t=>{
  const f=await fixture(t);
  assert.equal(f.w.document.querySelectorAll('.target-slider').length,12);
  for(let i=0;i<250;i++)f.input('target-position-slider-0',i%80);
  assert.equal(f.$('target-position-0').value,'9');assert.equal(f.$('target-check-0').checked,true);
  assert.equal(f.$('joint-execute').disabled,false);assert.equal(f.calls.length,0);
  assert.equal(f.$('confirm-dialog').open,false);
});
test('numeric edits sync thumbs but retain invalid drafts; empty target never becomes zero',async t=>{
  const f=await fixture(t);f.input('target-check-0','');f.$('target-check-0').checked=true;
  f.input('target-position-0',24.35);assert.ok(Math.abs(Number(f.$('target-position-slider-0').value)-24.35)<.11);
  f.input('target-position-0',120);assert.equal(f.$('target-position-0').value,'120');assert.equal(f.$('joint-execute').disabled,true);
  f.input('target-position-0','');assert.equal(f.$('target-position-0').value,'');assert.equal(f.$('joint-execute').disabled,true);
  assert.equal(f.posted().length,0);
});
test('arm and gripper speed/position ranges and fill-current values use their own units',async t=>{
  const f=await fixture(t);
  assert.equal(f.$('target-speed-slider-0').max,'20');assert.equal(f.$('target-speed-slider-5').max,'100');
  assert.equal(f.$('target-position-slider-5').min,'0');assert.equal(f.$('target-position-slider-5').max,'100');
  f.input('target-speed-slider-5',65);assert.equal(f.$('target-speed-5').value,'65');
  f.$('joint-fill-current').click();assert.equal(f.$('target-position-5').value,'25.00');assert.equal(Number(f.$('target-position-slider-5').value),25);
  assert.equal(f.$('target-position-0').value,'');assert.equal(f.posted().length,0);
});
test('execute sends selected targets once without a redundant modal',async t=>{
  const f=await fixture(t);f.input('target-position-slider-0',12);f.input('target-speed-slider-0',6);
  f.$('joint-execute').click();await pause(0);
  const calls=f.posted();assert.equal(calls.length,1);assert.equal(calls[0].body.command,'joint_target');
  assert.deepEqual(calls[0].body.joints,{shoulder_pan:{target:12,speed:6}});assert.equal(calls[0].body.confirmed,true);
  assert.equal(f.$('confirm-dialog').open,false);
});
test('execute still requires holding, basic control, valid bounds and matching versions',async t=>{
  const f=await fixture(t);f.input('target-position-slider-0',12);
  for(const mutate of [()=>f.snapshot.offline=true,()=>f.snapshot.identity.source_changed=true,()=>f.snapshot.restart_required=true,()=>f.snapshot.telemetry.health.torque_enabled.gripper=0,()=>f.snapshot.job.target_pending=true,()=>f.snapshot.job.kind='monitor']){
    const before=structuredClone(f.snapshot);mutate();f.render();assert.equal(f.$('joint-execute').disabled,true);f.$('joint-execute').click();
    Object.assign(f.snapshot,before);f.render();assert.equal(f.$('joint-execute').disabled,false);
  }
  assert.equal(f.posted().length,0);
});
test('control page exposes synchronization and start buttons through teleop phases',async t=>{
  const f=await fixture(t);assert.equal(f.$('control-teleop').disabled,false);
  for(const phase of ['SCENE_CHECK','SYNCING','WAITING_START','FOLLOWING']){
    f.snapshot.job={id:'QA-teleop',kind:'teleop',phase};f.render();
    assert.equal(f.$('approve-scene').hidden,false);assert.equal(f.$('start-session').hidden,false);
    assert.equal(f.$('approve-scene').disabled,phase!=='SCENE_CHECK');assert.equal(f.$('start-session').disabled,phase!=='WAITING_START');
    assert.equal(f.$('end-episode').hidden,true);assert.equal(f.$('end-reset').hidden,true);
  }
  f.run('setView("capture");setView("control");');assert.equal(f.$('start-session').hidden,false);
});
test('teleop waits for both old control and preview release before starting',async t=>{
  const f=await fixture(t);f.snapshot.preview_active=true;f.snapshot.preview_job={id:'QA-preview',kind:'preview'};f.render();
  f.$('control-teleop').click();await until(()=>f.calls.some(c=>c.url==='/api/jobs'));
  await until(()=>!f.run('requestBusy'));
  assert.deepEqual(f.posted().map(c=>c.url==='/api/jobs'?'teleop':c.body.command),['stop_device','stop_preview','teleop']);
  assert.equal(f.$('approve-scene').hidden,false);assert.equal(f.$('confirm-dialog').open,false);
});
test('busy motion/recording cannot be replaced by a teleop click',async t=>{
  const f=await fixture(t);
  for(const [kind,phase,pending] of [['record','RECORDING',false],['control','JOINT_MOVING',true],['control','RESETTING',false]]){
    f.snapshot.job={id:'QA-busy',kind,phase,target_pending:pending};f.render();assert.equal(f.$('control-teleop').disabled,true);
    f.$('control-teleop').click();await pause(0);
  }
  assert.equal(f.posted().length,0);
});
test('Esc cancels a waiting serial handoff; no new teleop job is spawned',async t=>{
  const f=await fixture(t);f.setRelease(false);f.$('control-teleop').click();await until(()=>f.posted().length===1);
  f.w.document.dispatchEvent(new f.w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  await until(()=>!f.run('requestBusy'));
  assert.equal(f.calls.some(c=>c.url==='/api/jobs'),false);assert.ok(f.posted().some(c=>c.body.command==='stop'));
});
test('Esc also cancels between owners even when no resource is active',async t=>{
  const f=await fixture(t);f.snapshot.active=false;f.snapshot.resources_active=false;f.render();f.run('requestBusy=true;updateControls();');
  const epoch=f.run('operationEpoch');assert.equal(f.$('stop-all').disabled,false);
  f.w.document.dispatchEvent(new f.w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  assert.equal(f.run('operationEpoch'),epoch+1);assert.equal(f.posted().length,0);
});
test('hold, jog, scene approval and teleop start use explicit buttons without modal loops',async t=>{
  const f=await fixture(t);f.$('hold-position').click();f.$('jog-plus').click();await pause(0);
  f.snapshot.job={id:'QA-teleop',kind:'teleop',phase:'SCENE_CHECK'};f.render();f.$('approve-scene').click();
  f.snapshot.job.phase='WAITING_START';f.render();f.$('start-session').click();await pause(0);
  assert.deepEqual(f.posted().map(c=>c.body.command),['hold','jog','scene','start']);assert.equal(f.$('confirm-dialog').open,false);
});
test('release and reset keep support/path confirmations; cancel sends nothing',async t=>{
  const f=await fixture(t);
  for(const id of ['release-torque','reference-reset']){
    f.$(id).click();await pause(0);assert.equal(f.$('confirm-dialog').open,true);f.$('confirm-dialog').close('cancel');await pause(0);
  }
  assert.equal(f.posted().length,0);
});
test('new control-page entry respects missing portable teleop configuration',async t=>{
  const f=await fixture(t);if(!f.$('configuration-readiness'))return;
  f.snapshot.capabilities.teleop={ready:false,reason:'missing leader binding'};f.render();
  assert.equal(f.$('control-teleop').disabled,true);assert.match(f.$('control-teleop').title,/missing leader binding/);
});

function dataset(name,stage,review,time,extra={}){
  const spec={stage,review,domain:'real',started_at:'2026-10-16T10:10',instruction:'把娃娃放进盒子',description:name,note:''};
  return {name,root:'QA/'+name,ready:true,episodes:3,sort_time:time,naming:{spec,simple_spec:spec,display_name:name,append_allowed:stage==='raw'},...extra};
}
function loadItems(f,items){f.w.__items=structuredClone(items);f.run('datasetItems=sortDatasetItems(window.__items);renderDatasetChoices();');}
function roots(f){return Array.from(f.$('dataset-select').options,o=>o.value).filter(Boolean);}
function category(f,key){f.w.document.querySelector('[data-dataset-category="'+key+'"]').click();}

test('dataset picker separates raw, approved training and processed/other without changing labels',async t=>{
  const f=await fixture(t),items=[dataset('raw','raw','pending',1),dataset('train','processed','train',2),dataset('pending','processed','pending',3),dataset('excluded','raw','exclude',4),dataset('raw-approved','raw','train',5),dataset('unknown','','',6,{naming:{}})];
  loadItems(f,items);assert.deepEqual(roots(f),['QA/raw']);
  category(f,'train');assert.deepEqual(roots(f),['QA/raw-approved','QA/train']);
  category(f,'other');assert.deepEqual(roots(f),['QA/unknown','QA/excluded','QA/pending']);
  category(f,'all');assert.equal(roots(f).length,6);
  assert.deepEqual(JSON.parse(f.run('JSON.stringify(datasetItems)')),items.slice().sort((a,b)=>b.sort_time-a.sort_time));
  assert.equal(f.posted().length,0);
});
test('dataset choices are newest first within every category, not filename sorted',async t=>{
  const f=await fixture(t);loadItems(f,[dataset('z-old','raw','pending',1),dataset('a-new','raw','pending',100),dataset('b-mid','raw','pending',50)]);
  assert.deepEqual(roots(f),['QA/a-new','QA/b-mid','QA/z-old']);assert.match(f.$('dataset-filter-summary').textContent,/时间倒序/);
  assert.equal(f.$('dataset-select').value,'');
});
test('dataset search matches task, description, notes and physical name',async t=>{
  const f=await fixture(t),item=dataset('CustomName','raw','pending',1);item.naming.simple_spec.note='变化娃娃位置';
  loadItems(f,[item]);
  for(const query of ['customname','娃娃','变化娃娃位置']){f.input('dataset-search',query);assert.deepEqual(roots(f),['QA/CustomName']);}
  f.input('dataset-search','no-match');assert.deepEqual(roots(f),[]);assert.equal(f.$('dataset-select').options[0].text,'没有匹配的数据集');
  f.input('dataset-search','');assert.equal(roots(f).length,1);assert.equal(f.posted().length,0);
});
test('crop/source navigation reveals the destination category and clears blocking search',async t=>{
  const f=await fixture(t);loadItems(f,[dataset('raw','raw','pending',1),dataset('crop','processed','pending',2)]);
  f.input('dataset-search','raw');assert.equal(f.run('revealDatasetChoice("QA/crop")'),true);
  assert.equal(f.run('datasetCategory'),'other');assert.equal(f.$('dataset-search').value,'');assert.equal(f.$('dataset-select').value,'QA/crop');
  assert.equal(f.run('revealDatasetChoice("QA/raw")'),true);assert.equal(f.run('datasetCategory'),'raw');assert.equal(f.$('dataset-select').value,'QA/raw');
  assert.equal(f.run('revealDatasetChoice("QA/missing")'),false);assert.equal(f.posted().length,0);
});
test('refresh keeps selected dataset after reclassification and new count/order is updated',async t=>{
  const f=await fixture(t),item=dataset('reviewed','raw','pending',1);loadItems(f,[item]);f.run('revealDatasetChoice("QA/reviewed");');
  const approved=dataset('reviewed','processed','train',1),next=dataset('new-training','processed','train',2),fetch=f.w.fetch;
  f.w.fetch=async(url,opts)=>url==='/api/datasets'?{ok:true,json:async()=>[approved,next]}:url==='/api/archives'?{ok:true,json:async()=>[]}:fetch(url,opts);
  await f.run('refreshDatasets()');assert.equal(f.run('datasetCategory'),'train');assert.equal(f.$('dataset-select').value,'QA/reviewed');
  assert.deepEqual(roots(f),['QA/new-training','QA/reviewed']);assert.match(f.w.document.querySelector('[data-dataset-category="train"]').textContent,/2/);
});
test('clearing a filtered-out selection clears stale review and archive state',async t=>{
  const f=await fixture(t);loadItems(f,[dataset('raw','raw','pending',1),dataset('train','processed','train',2)]);
  f.run('revealDatasetChoice("QA/raw");library={root:"QA/raw",episodes:[]};');category(f,'train');await pause(0);
  assert.equal(f.$('dataset-select').value,'');assert.equal(f.run('library'),null);assert.equal(f.$('archive-dataset').disabled,true);
});

async function initializeNaming(f){
  const base={schema_version:2,domain:'real',stage:'raw',review:'pending',started_at:'2026-10-16T10:10',instruction:'把娃娃放进盒子',description:'固定位置',note:'',sequence:1};
  const fetch=f.w.fetch;
  f.w.fetch=async(url,opts={})=>{
    if(url==='/api/naming-options')return {ok:true,json:async()=>({domains:{real:'实机',sim:'仿真'},stages:{raw:'原始',processed:'处理后'},reviews:{pending:'待审核',train:'可训练',exclude:'排除'},simple_defaults:base})};
    if(url==='/api/capture-plan'){
      f.calls.push({url,body:JSON.parse(opts.body)});
      return {ok:true,json:async()=>({name:'QA-new-capture',root:'QA/datasets/QA-new-capture',parent:'QA/datasets',spec:base})};
    }
    return fetch(url,opts);
  };
  await f.run('ensureDatasetNaming()');
}
test('only capture workbench has new-dataset planning; dataset page has no duplicate creation entry',async t=>{
  const f=await fixture(t);await initializeNaming(f);
  for(const id of ['create-dataset','new-dataset-name','manager-new'])assert.equal(f.$(id),null);
  assert.equal(f.w.document.querySelector('[data-manager-tab="new"]'),null);
  assert.ok(f.$('record-name-form').querySelector('.dataset-name-form'));
  assert.ok(f.$('record-target-mode').querySelector('option[value="new"]'));
  assert.ok(f.$('record-target-mode').querySelector('option[value="existing"]'));
  assert.ok(f.$('prepare-record'));assert.equal(f.calls.filter(c=>c.url==='/api/capture-plan').length,1);
  assert.equal(f.calls.some(c=>c.url==='/api/jobs'||c.url==='/api/dataset-management'),false);
});
test('dataset management defaults to labels and archive tab remains functional',async t=>{
  const f=await fixture(t);await initializeNaming(f);
  assert.equal(f.$('manager-label').classList.contains('hidden'),false);
  assert.equal(f.$('manager-archive').classList.contains('hidden'),true);
  f.w.document.querySelector('[data-manager-tab="archive"]').click();
  assert.equal(f.$('manager-label').classList.contains('hidden'),true);
  assert.equal(f.$('manager-archive').classList.contains('hidden'),false);
  assert.equal(f.$('archive-select').closest('#manager-archive'),f.$('manager-archive'));
  f.w.document.querySelector('[data-manager-tab="label"]').click();
  assert.equal(f.$('manager-label').classList.contains('hidden'),false);
  assert.equal(f.$('manager-archive').classList.contains('hidden'),true);
  assert.ok(f.$('save-dataset-label'));assert.ok(f.$('export-crop'));assert.ok(f.$('archive-dataset'));
});
