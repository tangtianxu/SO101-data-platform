'use strict';
let captureCollections=[],ungroupedCount=null,captureGroupTimer;
function groupingAllowed(){return $('record-target-mode').value==='new'||captureCollections.some(c=>c.root===$('record-existing').value);}
function captureGroupPlan(){
  if(!groupingAllowed())return [];
  const merged=new Map();
  for(const row of $('capture-group-rows').children){
    const task=row.querySelector('[data-group-task]').value.trim();if(!task)continue;
    const text=row.querySelector('[data-group-count]').value.trim(),count=Number(text);
    if(task.length>300)throw Error('每组指令最多 300 字');
    if(!text||!Number.isInteger(count)||count<1||count>30)throw Error('每组数量需为 1–30 的整数');
    merged.set(task,(merged.get(task)||0)+count);
  }
  if([...merged.values()].reduce((a,b)=>a+b,0)>30)throw Error('本次各组合计最多 30 条，请分批采集');
  return [...merged].map(([instruction,episodes])=>({instruction,episodes}));
}
function capturePlanInstruction(){const groups=captureGroupPlan();return groups.length>1?'分组采集':groups[0]?.instruction||$('record-task').value.trim();}
function updateCaptureGroupControls(){
  if(!$('capture-group-rows'))return;
  const allowed=groupingAllowed(),busy=!!(requestBusy||state?.active&&state.job?.kind==='record');
  $('add-capture-group').disabled=!allowed||busy||$('capture-group-rows').children.length>=30;
  $('capture-group-rows').querySelectorAll('input,textarea,button').forEach(el=>el.disabled=!allowed||busy);
  const selected=datasetItems.find(d=>d.root===$('record-existing').value),context=$('record-target-mode').value==='existing'?selected?.capture_group:null;
  $('record-task').readOnly=!!context;
  if(context)$('record-task').value=context.instruction;
  try{
    const groups=captureGroupPlan(),count=groups.reduce((sum,g)=>sum+g.episodes,0);
    if(groups.length){if(ungroupedCount===null)ungroupedCount=$('record-count').value;$('record-count').value=count;}
    else if(ungroupedCount!==null){$('record-count').value=ungroupedCount;ungroupedCount=null;}
    $('record-count').readOnly=!!groups.length;$('record-task').readOnly=!!context||!!groups.length;
    const collection=$('record-target-mode').value==='existing'&&captureCollections.some(c=>c.root===$('record-existing').value);
    $('capture-group-hint').textContent=!allowed?(context?'补录当前小组：指令固定，不嵌套分组；要录其他指令请选择分组总文件夹。':'普通数据集按单组补录，目录结构不变。分组请新建或选择已有分组总文件夹。'):groups.length?`${groups.length} 个指令小组，共 ${count} 条；相同指令合并数量。切组保存时保持当前位置，再低速跟随并等待手动开始，不重连设备。`:collection?'按默认指令补录到一个小组；没有对应指令时新建该小组。':'未分组：沿用默认指令和本次条数，不创建小组子文件夹。';
    $('capture-group-hint').classList.remove('error');
  }catch(error){$('record-count').readOnly=true;$('capture-group-hint').textContent=error.message;$('capture-group-hint').classList.add('error');}
}
function installCaptureGroupPlan(){
  const panel=$('record-groups-plan');
  panel.innerHTML='<summary>按语言指令分组（可选）</summary><p class="field-help">每组填写数量和指令。空指令行忽略，全部留空则不分组；本次各组合计上限仍为 30 条。</p><div id="capture-group-rows"></div><button id="add-capture-group" type="button" class="button subtle">＋ 添加指令小组</button><p id="capture-group-hint" class="field-help"></p>';
  function changed(){updateCaptureGroupControls();clearTimeout(captureGroupTimer);captureGroupTimer=setTimeout(()=>{if(recordNameForm&&$('record-target-mode').value==='new')recordNameForm.update();},200);}
  $('add-capture-group').addEventListener('click',()=>{
    const row=document.createElement('div');row.className='capture-group-row';
    row.innerHTML='<label>数量<input data-group-count type="number" min="1" max="30" step="1" value="5" aria-label="小组采集数量"></label><label>小组语言指令<textarea data-group-task rows="2" maxlength="300" placeholder="例如：把娃娃放进盒子" aria-label="小组语言指令"></textarea></label><button type="button" class="button subtle" data-group-remove aria-label="移除此小组">移除</button>';
    row.querySelector('[data-group-remove]').addEventListener('click',()=>{row.remove();changed();});
    row.addEventListener('input',changed);$('capture-group-rows').append(row);changed();row.querySelector('[data-group-task]').focus();
  });
  updateCaptureGroupControls();
}
