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
  w.HTMLMediaElement.prototype.pause=function(){this.__qaPaused=true;};w.HTMLMediaElement.prototype.load=function(){};
  w.HTMLMediaElement.prototype.play=function(){this.__qaPaused=false;return Promise.resolve();};
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
  for(const name of ['capture_pause.js','dataset_batch.js','dataset_browser.js','naming.js','history.js','joint_control.js','portable.js','capture_groups.js','app.js']){
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
function roots(f){return Array.from(f.$('dataset-browser-list').querySelectorAll('[data-dataset-root]'),o=>o.dataset.datasetRoot);}
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
  f.input('dataset-search','no-match');assert.deepEqual(roots(f),[]);assert.match(f.$('dataset-browser-list').textContent,/没有匹配/);
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
test('browsing filters and closing picker preserves the open dataset and its review',async t=>{
  const f=await fixture(t);loadItems(f,[dataset('raw','raw','pending',1),dataset('train','processed','train',2)]);
  f.run('revealDatasetChoice("QA/raw");library={root:"QA/raw",episodes:[]};');category(f,'train');await pause(0);
  assert.equal(f.$('dataset-select').value,'QA/raw');assert.equal(f.run('library.root'),'QA/raw');
  f.$('dataset-picker-open').click();assert.equal(f.$('dataset-browser-dialog').open,true);
  f.$('dataset-browser-dialog').querySelector('[data-close-dataset-dialog]').click();assert.equal(f.$('dataset-browser-dialog').open,false);
  assert.equal(f.$('dataset-select').value,'QA/raw');assert.equal(f.posted().length,0);
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

test('wide dataset browser renders complete long names safely and exposes editing beside selection',async t=>{
  const f=await fixture(t),item=dataset('long-name','raw','pending',1);
  item.naming.simple_spec.description='娃娃和目标位置变化'.repeat(12)+'<img src=x onerror=alert(1)>';
  loadItems(f,[item]);f.$('dataset-picker-open').click();
  assert.equal(f.$('dataset-browser-dialog').open,true);
  assert.ok(f.$('dataset-browser-list').textContent.includes(item.naming.simple_spec.description));
  assert.equal(f.$('dataset-browser-list').querySelector('img'),null);
  assert.equal(f.$('dataset-edit-info').closest('.dataset-current-panel')!==null,true);
  assert.equal(f.$('dataset-select').closest('label').hidden,true);
  assert.equal(f.posted().length,0);
});

async function datasetInformationFixture(t){
  const f=await fixture(t);await initializeNaming(f);
  const item=dataset('original-folder','raw','pending',1);item.naming.dataset_id='stable-dataset-id';
  const fetch=f.w.fetch;
  f.w.fetch=async(url,opts={})=>{
    if(url==='/api/dataset-name'){const body=JSON.parse(opts.body);return {ok:true,json:async()=>({spec:body.spec,name:'display-name',root:'QA/display-name'})};}
    if(url==='/api/dataset-management'){
      const body=JSON.parse(opts.body);f.calls.push({url,body});
      item.naming={...item.naming,spec:body.spec,simple_spec:body.spec,display_name:'把娃娃放进盒子 · '+body.spec.description};
      return {ok:true,json:async()=>({root:item.root,naming:item.naming,data_modified:false,physical_folder_renamed:false})};
    }
    if(url==='/api/datasets')return {ok:true,json:async()=>[item]};
    if(url==='/api/capture-collections'||url==='/api/archives')return {ok:true,json:async()=>[]};
    return fetch(url,opts);
  };
  f.snapshot.active=false;f.snapshot.resources_active=false;f.render();loadItems(f,[item]);
  f.run('revealDatasetChoice("QA/original-folder");library={root:"QA/original-folder",episodes:[]};selectedEpisode={index:4};renderDatasetNaming(datasetItems[0]);');
  f.calls.length=0;return {...f,item};
}

test('visible information button opens a full editor with read-only identity and editable scene',async t=>{
  const f=await datasetInformationFixture(t);f.$('dataset-edit-info').click();await until(()=>f.$('dataset-info-dialog').open);
  assert.match(f.$('dataset-info-identity').textContent,/stable-dataset-id/);
  assert.match(f.$('dataset-info-identity').textContent,/QA\/original-folder/);
  for(const key of ['domain','stage','review','instruction','description','note'])assert.ok(f.run('editNameForm.get("'+key+'")'));
  assert.equal(f.run('editNameForm.get("description").readOnly'),false);
  assert.equal(f.calls.some(c=>c.url==='/api/jobs'),false);
});

test('scene save updates selected name without a redundant modal, episode reload or identity change',async t=>{
  const f=await datasetInformationFixture(t);await f.run('openDatasetInformation()');
  f.run('editNameForm.get("description").value="位置变化";editNameForm.get("note").value="娃娃位置有变化";');
  f.$('save-dataset-label').click();await until(()=>f.$('toast').textContent.includes('名称与资料已保存'));
  const mutations=f.posted().filter(c=>c.url==='/api/dataset-management');
  assert.equal(mutations.length,1);assert.equal(mutations[0].body.root,'QA/original-folder');
  assert.equal(mutations[0].body.spec.description,'位置变化');assert.equal(mutations[0].body.spec.review,'pending');
  assert.equal(f.$('confirm-dialog').open,false);assert.equal(f.$('dataset-select').value,'QA/original-folder');
  assert.match(f.$('dataset-current-title').textContent,/位置变化/);
  assert.equal(f.run('library.naming.dataset_id'),'stable-dataset-id');assert.equal(f.run('selectedEpisode.index'),4);
  assert.equal(f.calls.some(c=>c.url.startsWith('/api/dataset?')||c.url==='/api/jobs'),false);
});

test('new training approval still needs confirmation and cancellation saves nothing',async t=>{
  const f=await datasetInformationFixture(t);await f.run('openDatasetInformation()');
  f.run('editNameForm.get("review").value="train";');f.$('save-dataset-label').click();
  await until(()=>f.$('confirm-dialog').open);f.$('confirm-dialog').close('cancel');await pause(0);
  assert.equal(f.posted().some(c=>c.url==='/api/dataset-management'),false);
});

async function batchFixture(t){
  const f=await fixture(t),item=dataset('raw-batch','raw','pending',1);loadItems(f,[item]);
  f.snapshot.active=false;f.snapshot.resources_active=false;f.render();
  f.w.__library={root:item.root,fps:30,episodes:[0,1,2].map(index=>({index,tasks:['抓娃娃'],duration:10,frames:300})),annotations:{0:{result:'success',note:'note 0'},1:{result:'failure',note:'note 1'},2:{result:'success',note:'note 2'}}};
  f.run('revealDatasetChoice("QA/raw-batch");resetEpisodeSelection("QA/raw-batch");library=window.__library;renderEpisodeList();updateDatasetBatchControls();');
  return f;
}

async function reviewFixture(t){
  const f=await batchFixture(t);
  f.w.__review={index:0,duration:30,tasks:['抓娃娃'],videos:{'observation.images.top':{exists:true,url:'/qa-top.mp4',start:100},'observation.images.wrist':{exists:true,url:'/qa-wrist.mp4',start:200}}};
  f.run('selectedEpisode=window.__review;reviewTime=0;$("review-timeline").max=30;$("crop-start").value=16;$("crop-end").value=28;cropSelectionChanged();');
  for(const [name,offset] of [['top',100],['wrist',200]]){
    const video=f.$('review-'+name);Object.defineProperty(video,'readyState',{configurable:true,value:4});
    video.src='/qa-'+name+'.mp4';video.currentTime=offset;video.playbackRate=1;
  }
  return f;
}

test('chart selection button plays both cameras from selected start with independent file offsets, without writes',async t=>{
  const f=await reviewFixture(t);f.$('play-selection').click();await until(()=>f.run('playbackActive'));
  assert.equal(f.$('review-top').currentTime,116);assert.equal(f.$('review-wrist').currentTime,216);
  assert.equal(f.$('review-scope').value,'selection');assert.equal(f.$('play-pause').textContent,'暂停');
  assert.match(f.$('selection-playback-hint').textContent,/16.00–28.00/);assert.match(f.$('review-playback-hint').textContent,/不会保存/);
  assert.equal(f.calls.length,0);assert.equal(f.$('confirm-dialog').open,false);f.run('pausePlayback()');
});

test('ordinary play respects the selection; pause/resume retains range and endpoint stops both videos before next episode',async t=>{
  const f=await reviewFixture(t);f.$('play-pause').click();await until(()=>f.run('playbackActive'));
  assert.equal(f.$('review-top').currentTime,116);
  f.$('review-top').currentTime=117;f.$('review-top').dispatchEvent(new f.w.Event('timeupdate'));
  f.$('play-pause').click();assert.equal(f.run('playbackActive'),false);assert.equal(f.$('review-scope').value,'selection');
  f.$('play-pause').click();await until(()=>f.run('playbackActive'));assert.equal(f.$('review-top').currentTime,117);
  f.$('review-top').currentTime=128.2;f.$('review-top').dispatchEvent(new f.w.Event('timeupdate'));
  assert.equal(f.run('playbackActive'),false);assert.equal(f.$('review-top').__qaPaused,true);assert.equal(f.$('review-wrist').__qaPaused,true);
  assert.ok(Math.abs(f.run('reviewTime')-(28-1/30))<1e-7);
  assert.ok(Math.abs(f.$('review-wrist').currentTime-(228-1/30))<1e-7);
  f.$('play-pause').click();await until(()=>f.run('playbackActive'));assert.equal(f.$('review-top').currentTime,116);
  assert.equal(f.calls.length,0);f.run('pausePlayback()');
});

test('whole episode is an explicit alternative and ignores selection endpoints',async t=>{
  const f=await reviewFixture(t);f.$('review-scope').value='episode';f.$('review-scope').dispatchEvent(new f.w.Event('change'));
  f.$('play-pause').click();await until(()=>f.run('playbackActive'));assert.equal(f.$('review-top').currentTime,100);
  f.$('review-top').currentTime=129;f.$('review-top').dispatchEvent(new f.w.Event('timeupdate'));assert.equal(f.run('playbackActive'),true);
  f.$('review-top').currentTime=130.1;f.$('review-top').dispatchEvent(new f.w.Event('timeupdate'));assert.equal(f.run('playbackActive'),false);
  assert.ok(f.run('reviewTime')<30);assert.equal(f.$('review-scope').value,'episode');assert.equal(f.calls.length,0);
});

test('changing the selection pauses playback, updates caption and rejects blank, reversed or outside ranges',async t=>{
  const f=await reviewFixture(t);await f.run('play()');f.input('crop-start',17);
  assert.equal(f.run('playbackActive'),false);assert.match(f.$('selection-playback-hint').textContent,/17.00–28.00/);
  for(const [start,end] of [['',28],[17,''],[29,28],[17,31]]){
    f.input('crop-start',start);f.input('crop-end',end);assert.equal(f.$('play-selection').disabled,true);assert.equal(f.$('play-pause').disabled,true);
    await assert.rejects(f.run('playSelection()'),/保留范围/);assert.equal(f.run('playbackActive'),false);
  }
  assert.equal(f.calls.length,0);
});

test('wrist-only playback follows the same range and a failed second camera pauses both',async t=>{
  const f=await reviewFixture(t);f.run('selectedEpisode.videos["observation.images.top"].exists=false');
  await f.run('playSelection()');assert.equal(f.$('review-wrist').currentTime,216);
  f.$('review-wrist').currentTime=228;f.$('review-wrist').dispatchEvent(new f.w.Event('timeupdate'));assert.equal(f.run('playbackActive'),false);
  f.run('selectedEpisode.videos["observation.images.top"].exists=true');f.$('review-wrist').play=()=>Promise.reject(Error('test decoder failure'));
  await assert.rejects(f.run('playSelection()'),/decoder failure/);
  assert.equal(f.run('playbackActive'),false);assert.equal(f.$('review-top').__qaPaused,true);assert.equal(f.$('review-wrist').__qaPaused,true);
  assert.equal(f.calls.length,0);
});

test('dragging a chart boundary pauses, changes the playback window and leaves click-to-seek working',async t=>{
  const f=await reviewFixture(t),chart=f.$('trajectory-chart');chart.getBoundingClientRect=()=>({left:0,width:1087,height:225});chart.setPointerCapture=()=>{};
  await f.run('playSelection()');
  function pointer(type,time){chart.dispatchEvent(new f.w.MouseEvent(type,{bubbles:true,clientX:45+time/30*1000}));}
  pointer('pointerdown',16);pointer('pointermove',17);pointer('pointerup',17);
  assert.equal(f.run('playbackActive'),false);assert.equal(Number(f.$('crop-start').value),17);assert.match(f.$('selection-playback-hint').textContent,/17.00–28.00/);
  pointer('pointerdown',20);assert.ok(Math.abs(f.run('reviewTime')-20)<1e-7);
  await f.run('play()');assert.ok(Math.abs(f.$('review-top').currentTime-120)<1e-7);f.run('pausePlayback()');assert.equal(f.calls.length,0);
});

test('boundary timer stops at the selected endpoint even without timeupdate or animation frames, including double speed',async t=>{
  const f=await reviewFixture(t);f.w.requestAnimationFrame=()=>123;
  f.input('crop-end',16.2);f.$('playback-speed').value='2';f.$('playback-speed').dispatchEvent(new f.w.Event('change'));
  await f.run('playSelection()');assert.equal(f.$('review-top').playbackRate,2);
  f.$('review-top').currentTime=116.2;await until(()=>!f.run('playbackActive'));
  assert.ok(f.run('reviewTime')<16.2);assert.equal(f.$('review-wrist').__qaPaused,true);assert.equal(f.calls.length,0);
});

test('a pause while media play is pending cannot restart the playback loop',async t=>{
  const f=await reviewFixture(t);let resolve;f.$('review-top').play=()=>new Promise(done=>resolve=done);
  const pending=f.run('playSelection()');f.run('pausePlayback()');resolve();await pending;
  assert.equal(f.run('playbackActive'),false);assert.equal(f.run('playbackTick'),0);assert.equal(f.run('playbackBoundaryTimer'),0);
  assert.equal(f.$('play-pause').textContent,'播放');assert.equal(f.calls.length,0);
});

test('batch episode checkboxes select all or filtered rows without opening videos or commands',async t=>{
  const f=await batchFixture(t);
  f.$('batch-select-all').click();assert.equal(f.run('episodeSelection.size'),3);
  assert.equal(f.$('episode-list').querySelectorAll('input:checked').length,3);
  f.$('batch-clear').click();assert.equal(f.run('episodeSelection.size'),0);
  f.input('episode-search','2');f.$('batch-select-filtered').click();
  assert.deepEqual(Array.from(f.run('[...episodeSelection]')),[1]);
  assert.match(f.$('batch-selection-count').textContent,/1 \/ 3/);assert.equal(f.posted().length,0);
  assert.equal(f.$('confirm-dialog').open,false);
});

test('episode list has a visible select-all checkbox, mixed state and clear action that never opens videos',async t=>{
  const f=await batchFixture(t),master=f.$('episode-select-all');
  assert.equal(master.disabled,false);assert.equal(master.checked,false);assert.equal(master.indeterminate,false);
  assert.equal(f.$('episode-selection-toolbar').nextElementSibling.id,'episode-list');
  master.click();assert.equal(f.run('episodeSelection.size'),3);assert.equal(master.checked,true);assert.equal(master.indeterminate,false);
  assert.equal(f.$('episode-list').querySelectorAll('input:checked').length,3);assert.match(f.$('episode-selected-count').textContent,/3 \/ 3/);
  f.$('episode-list').querySelector('input').click();assert.equal(master.checked,false);assert.equal(master.indeterminate,true);
  master.click();assert.equal(f.run('episodeSelection.size'),3);master.click();assert.equal(f.run('episodeSelection.size'),0);
  f.$('batch-select-all').click();assert.equal(master.checked,true);f.$('episode-clear-selection').click();assert.equal(master.checked,false);
  assert.equal(f.$('episode-clear-selection').disabled,true);assert.equal(f.calls.length,0);
});

test('list select-all clearly scopes to search results, preserves hidden selections and disables for zero results or a loading dataset',async t=>{
  const f=await batchFixture(t),master=f.$('episode-select-all');
  f.$('episode-list').querySelector('input').click();f.input('episode-search','2');
  assert.equal(f.$('episode-select-all-label').textContent,'全选筛选结果');master.click();
  assert.deepEqual(Array.from(f.run('[...episodeSelection].sort()')),[0,1]);
  master.click();assert.deepEqual(Array.from(f.run('[...episodeSelection]')),[0]);
  f.input('episode-search','不存在');assert.equal(master.disabled,true);assert.equal(master.checked,false);
  f.input('episode-search','');assert.equal(master.indeterminate,true);
  f.run('resetEpisodeSelection("QA/next");updateDatasetBatchControls();');assert.equal(master.disabled,true);assert.equal(master.indeterminate,false);
  assert.equal(f.calls.length,0);
});

test('batch mark posts once without a confirmation dialog and preserves notes',async t=>{
  const f=await batchFixture(t),fetch=f.w.fetch;
  f.w.fetch=async(url,opts={})=>{
    if(url==='/api/annotations/batch'){const body=JSON.parse(opts.body);f.calls.push({url,body});return {ok:true,json:async()=>({changed:body.episodes.length,annotations:{0:{result:body.result,note:'note 0'},1:{result:body.result,note:'note 1'},2:{result:body.result,note:'note 2'}}})};}
    return fetch(url,opts);
  };
  f.$('batch-select-all').click();f.w.document.querySelector('[data-batch-result="exclude"]').click();
  await until(()=>f.$('toast').textContent.includes('已将 3 条'));
  assert.equal(f.posted().filter(c=>c.url==='/api/annotations/batch').length,1);
  assert.equal(f.run('library.annotations[0].note'),'note 0');assert.equal(f.run('library.annotations[1].result'),'exclude');
  assert.equal(f.$('confirm-dialog').open,false);assert.equal(f.calls.some(c=>c.url==='/api/jobs'),false);
});

test('batch controls cannot act on the old library while a different dataset is loading',async t=>{
  const f=await batchFixture(t);f.$('batch-select-all').click();
  f.run('resetEpisodeSelection("QA/next");updateDatasetBatchControls();');
  assert.equal(f.$('batch-auto-trim').disabled,true);assert.equal(f.$('batch-select-all').disabled,true);
  await assert.rejects(f.run('batchMark("success")'),/加载完成/);assert.equal(f.posted().length,0);
});

test('select-all delete is one request with no confirmations; deleted view supports batch restore of prior states and notes',async t=>{
  const f=await batchFixture(t),fetch=f.w.fetch;let saved=JSON.parse(f.run('JSON.stringify(library.annotations)'));
  f.w.fetch=async(url,options={})=>{
    if(url==='/api/episodes/archive'){
      const body=JSON.parse(options.body);f.calls.push({url,body});let changed=0;
      for(const ep of body.episodes){const row=saved[ep];if(body.action==='delete'&&!row.deleted){row.before_delete_result=row.result;row.result='exclude';row.deleted=true;changed++;}else if(body.action==='restore'&&row.deleted){row.result=row.before_delete_result;delete row.before_delete_result;row.deleted=false;changed++;}}
      return {ok:true,json:async()=>({annotations:structuredClone(saved),changed,selected:body.episodes.length})};
    }return fetch(url,options);
  };
  f.$('episode-select-all').click();assert.equal(f.$('batch-delete-episodes').disabled,false);f.$('batch-delete-episodes').click();await until(()=>!f.run('batchReviewBusy'));
  assert.equal(f.posted().length,1);assert.deepEqual(f.posted()[0].body.episodes,[0,1,2]);assert.equal(f.posted()[0].body.action,'delete');
  assert.equal(f.$('confirm-dialog').open,false);assert.equal(f.$('episode-list').children.length,0);assert.equal(f.run('episodeSelection.size'),0);
  assert.match(f.$('episode-trash-count').textContent,/3 条/);assert.equal(f.run('library.annotations[1].note'),'note 1');
  f.$('episode-visibility').value='deleted';f.$('episode-visibility').dispatchEvent(new f.w.Event('change'));
  assert.equal(f.$('episode-list').children.length,3);f.$('episode-select-all').click();assert.equal(f.$('batch-restore-episodes').disabled,false);
  assert.equal(f.w.document.querySelector('[data-batch-result="success"]').disabled,true);
  f.$('batch-restore-episodes').click();await until(()=>!f.run('batchReviewBusy'));
  assert.equal(f.posted().length,2);assert.equal(f.posted()[1].body.action,'restore');assert.equal(f.run('library.annotations[1].result'),'failure');
  assert.equal(f.run('library.annotations[1].note'),'note 1');assert.equal(f.$('episode-list').children.length,0);
  f.$('episode-visibility').value='active';f.$('episode-visibility').dispatchEvent(new f.w.Event('change'));
  assert.equal(f.$('episode-list').children.length,3);assert.equal(f.calls.some(c=>c.url==='/api/jobs'),false);
});

test('archive failure keeps selections and annotations; empty, mismatched and active task states cannot dispatch deletion',async t=>{
  const f=await batchFixture(t);assert.equal(f.$('batch-delete-episodes').disabled,true);
  f.$('episode-select-all').click();const before=f.run('JSON.stringify(library.annotations)');
  const fetch=f.w.fetch;f.w.fetch=async(url,options={})=>url==='/api/episodes/archive'?{ok:false,json:async()=>({error:'保存失败，整批未修改'})}:fetch(url,options);
  f.$('batch-delete-episodes').click();await until(()=>!f.run('batchReviewBusy'));
  assert.equal(f.run('episodeSelection.size'),3);assert.equal(f.run('JSON.stringify(library.annotations)'),before);assert.match(f.$('toast').textContent,/整批未修改/);
  f.snapshot.active=true;f.snapshot.resources_active=true;
  for(const [kind,phase] of [['record','RECORDING'],['record','RESET'],['crop','CROPPING'],['act','RUNNING']]){
    f.snapshot.job={kind,phase,id:'blocked'};f.render();assert.equal(f.$('batch-delete-episodes').disabled,true);
  }
  f.snapshot.job={kind:'record',phase:'PAUSED',id:'paused'};f.render();assert.equal(f.$('batch-delete-episodes').disabled,false);
  f.run('resetEpisodeSelection("QA/other");updateDatasetBatchControls()');assert.equal(f.$('batch-delete-episodes').disabled,true);
  await assert.rejects(f.run('batchArchiveEpisodes("delete")'),/加载完成/);assert.equal(f.posted().length,0);
});

test('changing normal/deleted/all views clears hidden selections and select-all stays within that view',async t=>{
  const f=await batchFixture(t);f.run('library.annotations[1]={...library.annotations[1],deleted:true,result:"exclude"};renderEpisodeList()');
  assert.equal(f.$('episode-list').children.length,2);f.$('batch-select-all').click();assert.deepEqual(Array.from(f.run('[...episodeSelection]')),[0,2]);
  f.$('episode-visibility').value='deleted';f.$('episode-visibility').dispatchEvent(new f.w.Event('change'));assert.equal(f.run('episodeSelection.size'),0);
  f.$('batch-select-all').click();assert.deepEqual(Array.from(f.run('[...episodeSelection]')),[1]);assert.equal(f.$('batch-delete-episodes').disabled,true);
  f.$('episode-visibility').value='all';f.$('episode-visibility').dispatchEvent(new f.w.Event('change'));f.$('batch-select-all').click();assert.equal(f.run('episodeSelection.size'),3);
  assert.equal(f.$('batch-delete-episodes').disabled,false);assert.equal(f.$('batch-restore-episodes').disabled,false);assert.equal(f.calls.length,0);
});

test('motion analysis requests all successful scope without applying crops automatically',async t=>{
  const f=await batchFixture(t);f.$('batch-auto-trim').click();assert.equal(f.$('motion-trim-dialog').open,true);
  f.$('motion-analyze').click();await until(()=>f.posted().some(c=>c.url==='/api/jobs'));
  const job=f.posted().find(c=>c.url==='/api/jobs').body;
  assert.equal(job.kind,'crop');assert.equal(job.options.mode,'motion_preview');assert.equal(job.options.episodes,null);
  assert.equal(job.options.settings.arm_delta,.5);assert.equal(job.options.settings.gripper_delta,1);
  assert.equal(f.posted().filter(c=>c.url==='/api/jobs').length,1);assert.equal(f.$('motion-apply').disabled,true);
  await until(()=>f.run('requestBusy')===false);await pause(0);
});

test('preview selects only ready rows and batch apply uses that exact source plan',async t=>{
  const f=await batchFixture(t);
  f.w.__plan={dataset_root:'QA/raw-batch',rows:[{episode:0,status:'ready',start:1,end:9,duration:10,reason:'建议'}, {episode:2,status:'skip',duration:10,reason:'未检出持续运动'}]};
  f.run('motionPreview=window.__plan;motionPreviewJob="preview-1";renderMotionPreview();');
  assert.equal(f.$('motion-preview-rows').querySelectorAll('input:checked').length,1);
  assert.equal(f.$('motion-preview-rows').querySelectorAll('input:disabled').length,1);
  f.$('motion-apply').click();await until(()=>f.posted().some(c=>c.url==='/api/jobs'));
  const request=f.posted().find(c=>c.url==='/api/jobs').body.options;
  assert.equal(request.mode,'motion_apply');assert.equal(request.preview_job_id,'preview-1');assert.deepEqual(request.episodes,[0]);
  assert.equal(f.$('confirm-dialog').open,false);
  await until(()=>f.run('requestBusy')===false);await pause(0);
});

test('changing motion parameters invalidates an older preview',async t=>{
  const f=await batchFixture(t);f.w.__plan={dataset_root:'QA/raw-batch',rows:[{episode:0,status:'ready',start:1,end:9,duration:10,reason:'建议'}]};
  f.run('motionPreview=window.__plan;motionPreviewJob="preview-1";renderMotionPreview();');
  f.input('motion-arm_delta',1);
  assert.equal(f.run('motionPreviewJob'),null);assert.equal(f.$('motion-apply').disabled,true);assert.equal(f.posted().length,0);
});

test('motion dialog exposes progress and cancellation sends only a stop command',async t=>{
  const f=await batchFixture(t);
  f.snapshot.active=true;f.snapshot.resources_active=true;f.snapshot.job={id:'crop-preview',kind:'crop',phase:'MOTION_ANALYZING',message:'只读分析'};
  f.snapshot.telemetry={analysis_done:2,analysis_total:3};f.render();
  assert.equal(f.$('motion-stop').disabled,false);assert.match(f.$('motion-job-status').textContent,/2\/3/);
  f.$('motion-stop').click();await pause(0);
  assert.equal(f.posted().length,1);assert.equal(f.posted()[0].url,'/api/command');assert.equal(f.posted()[0].body.command,'stop');
});

function addGroup(f,task,count){
  f.$('add-capture-group').click();const row=f.$('capture-group-rows').lastElementChild;
  const taskInput=row.querySelector('[data-group-task]'),countInput=row.querySelector('[data-group-count]');
  taskInput.value=task;countInput.value=String(count);taskInput.dispatchEvent(new f.w.Event('input',{bubbles:true}));return row;
}
test('blank grouping leaves old single-dataset behavior and removes per-episode textarea',async t=>{
  const f=await fixture(t);assert.equal(f.$('record-tasks'),null);
  assert.deepEqual(JSON.parse(f.run('JSON.stringify(captureGroupPlan())')),[]);
  addGroup(f,'',5);assert.equal(f.$('record-count').value,'5');assert.equal(f.$('record-count').readOnly,false);
  assert.deepEqual(JSON.parse(f.run('JSON.stringify(captureGroupPlan())')),[]);assert.match(f.$('capture-group-hint').textContent,/不创建小组/);
});
test('group plan merges identical instructions, totals counts and restores ungrouped count on removal',async t=>{
  const f=await fixture(t),a=addGroup(f,'抓娃娃',4),b=addGroup(f,'抓布块',3);
  assert.equal(f.$('record-count').value,'7');assert.equal(f.$('record-count').readOnly,true);assert.equal(f.run('capturePlanInstruction()'),'分组采集');
  b.querySelector('[data-group-task]').value='抓娃娃';b.dispatchEvent(new f.w.Event('input',{bubbles:true}));
  assert.deepEqual(JSON.parse(f.run('JSON.stringify(captureGroupPlan())')),[{instruction:'抓娃娃',episodes:7}]);
  assert.equal(f.run('capturePlanInstruction()'),'抓娃娃');a.querySelector('[data-group-remove]').click();b.querySelector('[data-group-remove]').click();
  assert.equal(f.$('record-count').value,'5');assert.equal(f.$('record-count').readOnly,false);
});
test('invalid group counts are rejected and existing flat datasets do not gain nested groups',async t=>{
  const f=await fixture(t);addGroup(f,'抓娃娃',0);addGroup(f,'抓布块',20);
  assert.throws(()=>f.run('captureGroupPlan()'),/正整数/);assert.equal(f.$('capture-group-hint').classList.contains('error'),true);
  loadItems(f,[dataset('flat','raw','pending',1)]);f.run('refreshRecordTargets();');
  f.$('record-target-mode').value='existing';f.$('record-existing').value='QA/flat';f.run('recordTargetChanged();');
  assert.equal(f.$('add-capture-group').disabled,true);assert.deepEqual(JSON.parse(f.run('JSON.stringify(captureGroupPlan())')),[]);
});
test('existing instruction group locks its task, but collection permits multi-group append',async t=>{
  const f=await fixture(t),item=dataset('g001','raw','pending',1,{capture_group:{id:'g001',instruction:'固定组指令',collection_root:'QA/collection',collection_name:'一次采集'}});
  loadItems(f,[item]);f.w.__collections=[{root:'QA/collection',name:'一次采集',groups:[{id:'g001'}]}];f.run('captureCollections=window.__collections;refreshRecordTargets();');
  f.$('record-target-mode').value='existing';f.$('record-existing').value='QA/g001';f.run('recordTargetChanged();');
  assert.equal(f.$('record-task').readOnly,true);assert.equal(f.$('record-task').value,'固定组指令');
  f.$('record-existing').value='QA/collection';f.run('recordTargetChanged();');assert.equal(f.$('record-task').readOnly,false);assert.equal(f.$('add-capture-group').disabled,false);
  assert.equal(f.w.document.querySelector('#dataset-select optgroup').label,'分组采集 · 一次采集');
});
test('group status shows current instruction and explicit next-group start without modal',async t=>{
  const f=await fixture(t);f.snapshot.job={id:'QA-record',kind:'record',phase:'WAITING_START',group_count:2,group_index:2,group_requested:5,group_saved:0,group_task:'抓布块'};
  f.render();assert.equal(f.$('start-session').textContent,'开始第 2 组（5 条）');assert.match(f.$('phase-message').textContent,/抓布块/);
  assert.match(f.$('phase-count').textContent,/小组 2\/2/);f.$('start-session').click();await pause(0);assert.equal(f.$('confirm-dialog').open,false);
  assert.equal(f.posted()[0].body.command,'start');
});

test('single total and each instruction group accept 50, 200 and larger plans',async t=>{
  const f=await fixture(t);
  assert.equal(f.$('record-count').hasAttribute('max'),false);
  for(const count of [31,50,200,10000]){
    f.$('record-count').value=String(count);assert.equal(f.$('record-count').checkValidity(),true);
    assert.equal(f.run('captureEpisodeCount($("record-count").value)'),count);
  }
  f.$('record-count').value='200';
  const a=addGroup(f,'抓娃娃',50),b=addGroup(f,'抓布块',150);
  assert.equal(a.querySelector('[data-group-count]').hasAttribute('max'),false);
  assert.equal(f.$('record-count').value,'200');assert.equal(f.$('capture-group-hint').classList.contains('error'),false);
  assert.deepEqual(JSON.parse(f.run('JSON.stringify(captureGroupPlan())')),[{instruction:'抓娃娃',episodes:50},{instruction:'抓布块',episodes:150}]);
  b.querySelector('[data-group-task]').value='抓娃娃';b.dispatchEvent(new f.w.Event('input',{bubbles:true}));
  assert.deepEqual(JSON.parse(f.run('JSON.stringify(captureGroupPlan())')),[{instruction:'抓娃娃',episodes:200}]);
  assert.equal(f.posted().length,0);
});

test('invalid single count is rejected before plan request or device handoff',async t=>{
  const f=await fixture(t);
  for(const value of ['','0','-1','1.2','9007199254740992']){
    f.$('record-count').value=value;f.$('prepare-record').disabled=false;f.$('prepare-record').click();await pause(0);
    assert.match(f.$('toast').textContent,/正整数/);assert.equal(f.calls.length,0);
  }
});

test('record preparation submits 50 and 200 unchanged to the fake backend',async t=>{
  for(const count of [50,200]){
    const f=await fixture(t);await initializeNaming(f);
    f.snapshot.active=false;f.snapshot.resources_active=false;f.render();f.run('setView("capture")');
    f.$('record-count').value=String(count);f.$('prepare-record').click();
    await until(()=>f.posted().some(c=>c.url==='/api/jobs'));
    const jobs=f.posted().filter(c=>c.url==='/api/jobs');
    assert.equal(jobs.length,1);assert.equal(jobs[0].body.kind,'record');
    assert.equal(jobs[0].body.options.episodes,count);assert.deepEqual(jobs[0].body.options.instruction_groups,[]);
    assert.equal(jobs[0].body.options.manual_advance,true);assert.equal('record_seconds' in jobs[0].body.options,false);assert.equal('reset_seconds' in jobs[0].body.options,false);
  }
});

test('group total rejects integer precision overflow instead of rounding',async t=>{
  const f=await fixture(t);addGroup(f,'抓娃娃',Number.MAX_SAFE_INTEGER);addGroup(f,'抓布块',1);
  assert.throws(()=>f.run('captureGroupPlan()'),/整数范围/);assert.equal(f.$('capture-group-hint').classList.contains('error'),true);
});

async function capturePauseFixture(t,phase='RECORDING'){
  const f=await fixture(t);f.snapshot.job={id:'QA-record-pause',kind:'record',phase};f.snapshot.telemetry.episode_frames=5;
  f.run('setView("capture")');f.render();const fetch=f.w.fetch;
  const data={job_id:'QA-record-pause',datasets:[{root:'QA/raw',name:'数采',total:2,annotations:{0:{result:'success',note:'旧备注'},1:{result:'exclude',note:'中断',interrupted:true}}}]};
  f.w.fetch=async(url,options={})=>{
    const body=options.body?JSON.parse(options.body):null;
    if(url.startsWith('/api/capture-review')){
      f.calls.push({url,body});
      if(body){const row=data.datasets[0].annotations[body.episode];if(body.action==='edit'){row.note=body.note;row.result=body.result;}else if(body.action==='delete'){row.before_delete_result=row.result;row.result='exclude';row.deleted=true;}else if(body.action==='restore'){row.result=row.before_delete_result;row.deleted=false;}}
      return {ok:true,json:async()=>structuredClone(data)};
    }
    if(url==='/api/command'&&body.command==='pause_capture')f.snapshot.job.phase='PAUSED';
    if(url==='/api/command'&&body.command==='resume_capture')f.snapshot.job.phase='RESUMING';
    return fetch(url,options);
  };
  return f;
}

test('capture is Space-driven with no duration inputs or timer payloads; pause and resume are explicit, no confirmation modal',async t=>{
  const f=await capturePauseFixture(t);assert.equal(f.$('record-duration'),null);assert.equal(f.$('reset-duration'),null);
  assert.match(f.$('phase-time').textContent,/不限时/);assert.equal(f.$('pause-capture').disabled,false);assert.equal(f.$('resume-capture').disabled,true);
  f.$('pause-capture').click();await pause(0);f.render();assert.equal(f.$('resume-capture').disabled,false);
  f.$('resume-capture').click();await until(()=>f.run('state.job.phase')==='RESUMING'&&!f.run('pollBusy'));
  assert.deepEqual(f.posted().map(c=>c.body.command),['pause_capture','resume_capture']);assert.equal(f.posted()[1].body.confirmed,true);
  assert.equal(f.$('confirm-dialog').open,false);assert.equal(f.$('resume-capture').disabled,true);
});

test('processing first pauses capture, waits for acknowledgment and opens latest-first notes and recoverable deletion',async t=>{
  const f=await capturePauseFixture(t);f.$('process-capture').click();await until(()=>f.$('capture-review-dialog').open);
  assert.equal(f.posted()[0].body.command,'pause_capture');assert.equal(f.$('capture-review-episode').value,'1');assert.match(f.$('capture-review-entry-hint').textContent,/中断片段/);
  f.$('capture-review-note').value='临时离开';f.$('capture-review-save').click();await until(()=>!f.run('captureReviewBusy'));
  assert.equal(f.posted().at(-1).body.note,'临时离开');assert.equal(f.$('capture-review-note').value,'临时离开');
  f.$('capture-review-delete').click();await until(()=>!f.run('captureReviewBusy'));assert.equal(f.$('capture-review-delete').textContent,'恢复此条');
  assert.equal(f.$('capture-review-result').disabled,true);f.$('capture-review-delete').click();await until(()=>!f.run('captureReviewBusy'));
  assert.equal(f.$('capture-review-delete').textContent,'删除此条（可恢复）');assert.equal(f.$('confirm-dialog').open,false);
  assert.equal(f.posted().some(c=>c.url==='/api/jobs'),false);
});

test('Space cannot resume a paused session or leak through processing inputs; Esc still requests a batch stop',async t=>{
  const f=await capturePauseFixture(t,'PAUSED');f.w.document.dispatchEvent(new f.w.KeyboardEvent('keydown',{code:'Space',key:' ',bubbles:true}));
  assert.equal(f.posted().length,0);await f.run('loadCaptureReview()');
  f.$('capture-review-note').dispatchEvent(new f.w.KeyboardEvent('keydown',{code:'Space',key:' ',bubbles:true}));assert.equal(f.posted().length,0);
  f.w.document.dispatchEvent(new f.w.KeyboardEvent('keydown',{code:'Escape',key:'Escape',bubbles:true}));await pause(0);
  assert.equal(f.posted()[0].body.command,'stop');
});

test('record and reset Space advance their matching phase only; key repeat is ignored',async t=>{
  const f=await capturePauseFixture(t);
  for(const phase of ['RECORDING','RESET']){
    f.snapshot.job.phase=phase;f.render();
    f.w.document.dispatchEvent(new f.w.KeyboardEvent('keydown',{code:'Space',key:' ',bubbles:true}));
    f.w.document.dispatchEvent(new f.w.KeyboardEvent('keydown',{code:'Space',key:' ',repeat:true,bubbles:true}));
  }
  await pause(0);assert.deepEqual(f.posted().map(c=>c.body.phase),['RECORDING','RESET']);
});
