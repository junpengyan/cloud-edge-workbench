"use strict";
const $=id=>document.getElementById(id);
let token="",project=null,plan=null,dirty=false,busy=false,mapPage=0,allJobs=[],candidate={},timer=null;
const gatewayFields=["projectPrefix","maxProjectPoints","maxForwardTags","strategy","forwardMode","mqttHost","mqttPort","mqttQos","mqttPub","mqttSub","changePeriod","pubTimer","allowFill","templateDbPath","templateBmsCsvPath","sharedDevicePrefix","mqttSn"];
const cloudFields=["base_url","device_name_prefix","asset_name_prefix","asset_template","edge_mode","edge_names","edge_count","enable_rule_chain_creation"];
const authFields=["existing_tenant_admin_username","existing_tenant_admin_password"];
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=n=>Number(n).toLocaleString("zh-CN");
const stepViews=['config','gateway','cloud','preview'];
const viewNames={home:'设备池与轮次',config:'设备与测点',gateway:'采集与转发',cloud:'平台设备准备',preview:'生成网关工程',history:'历史记录'};
let currentView='home',checkedKey='',reviewRevision=null;
let templatePage=0,templateTotal=0,templateItems=[],gatewayDefaults={},sharedSpecs=null;
const lookupAttempts=new Set();
function canContinueTemplates(){return !!project&&(!project.bindings.length||project.templateMode||devicesReady()||lookupAttempts.has(project.id)||allJobs.some(j=>j.projectId===project.id&&j.reuseExistingDevices&&j.state!=='running'));}

function notice(text,error=false,scope=currentView){if(error&&$('roundPanel').open){$('roundError').hidden=false;$('roundError').textContent=text;return;}const el=$('notice-'+scope);el.hidden=!text;el.textContent=text;el.className='module-notice'+(error?' error':'');}
async function api(url,body){
 const response=await fetch("/api"+url,body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json","X-Workspace-Token":token},body:JSON.stringify(body)});
 let data;try{data=await response.json();}catch{throw Error('服务返回异常（HTTP '+response.status+'），请查看工作台服务日志');}
 if(!response.ok){const message=typeof data.message==='string'?data.message:typeof data.detail==='string'?data.detail:'';if(response.status===404&&url==='/template-points')throw Error('测点查询接口未加载，请重启工作台服务后刷新页面');throw Error(message||'请求失败（HTTP '+response.status+'），请检查输入');}return data;
}
async function guarded(fn,scope=currentView){if(busy)return;busy=true;document.body.setAttribute("aria-busy","true");try{await fn();}catch(e){notice(e.message,true,scope);}finally{busy=false;document.body.removeAttribute("aria-busy");}}
function change(){dirty=true;plan=null;$("dirtyLabel").textContent="有未保存的修改";$("generateBtn").disabled=true;reviewRevision=null;$("resourceReview").hidden=true;$("provisionBtn").hidden=true;updateWorkflow();}
function requireProject(){if(!project)throw Error("请先新建工程或导入历史记录");}
function clean(){requireProject();if(dirty)throw Error("请先保存当前配置");}
function switchView(name){
 if(!viewNames[name])return;
 if(stepViews.includes(name)&&!project){notice('先选择或新建一个工程。',true);return;}
 if(stepViews.indexOf(name)>0&&(!project.pointsConfirmed||dirty)){notice('请先确认设备与测点并保存配置。',true);return;}
 if(['cloud','preview'].includes(name)&&checkedKey!==configurationKey()){notice('请先在第 2 步检查采集与转发配置。',true);return;}
 if(name==='preview'&&!devicesReady()){notice('请在第 3 步点击继续，准备本地模板。',true);return;}
 currentView=name;
 document.querySelectorAll('.view').forEach(v=>v.hidden=v.id!==name);
 document.querySelectorAll('.rail nav button').forEach(b=>b.classList.toggle('selected',b.dataset.view===name||(b.dataset.view==='config'&&stepViews.includes(name))));
 $('title').textContent=viewNames[name];$('workflowSteps').hidden=!project||!stepViews.includes(name);
 if(name==='cloud')renderMapping();
 if(name==='history')refreshJobs().catch(e=>notice(e.message,true,'history'));
 updateWorkflow();
}
function typeRows(){return [...$("typeRows").rows].map(tr=>{
 const r={type:tr.dataset.kind};tr.querySelectorAll("input").forEach(i=>r[i.dataset.field]=i.value===""?null:Number(i.value));if(project?.profile==="microgrid")r.fastPercent=15;return r;
});}
function metrics(){
 const rows=typeRows();renderMicrogridStats(rows);$("deviceTotal").textContent=fmt(rows.reduce((a,r)=>a+r.total,0));
 const missing=rows.some(r=>r.total>0&&!r.points);$("pointTotal").textContent=missing?"待补齐":fmt(rows.reduce((a,r)=>a+r.total*(r.points||0),0));
 const stacks=rows.find(r=>r.type==="BMS")?.total||0;
 $("collectTotal").textContent=fmt(rows.filter(r=>!["GROUPBMS","CELL","BMS"].includes(r.type)).reduce((a,r)=>a+r.total,0)+stacks);
 [...$("typeRows").rows].forEach((tr,i)=>tr.lastElementChild.textContent=rows[i].points?fmt(rows[i].total*rows[i].points):"待填写");
}
function render(p){
 if(project?.id!==p.id){checkedKey='';reviewRevision=null;sharedSpecs=null;}
 project=p;dirty=false;plan=null;mapPage=0;$("projectBar").hidden=false;$("projectName").value=p.name;$("version").textContent="版本 "+p.revision;$("dirtyLabel").textContent="已保存";
 const micro=p.profile==="microgrid";
 setupCapacity(p);
 $("profileLabel").textContent=micro?"微电网专用":"储能方案";
 $("microgridSettings").hidden=!micro;$("stackSection").hidden=micro;
 $("stationCount").value=p.microgrid?.stationCount??"";
 $("asset_template").readOnly=micro;
 $("fastHeader").textContent=micro?"单设备秒级点数":"秒级点比例 %";
 $("candidateHint").textContent=micro?"微电网模板点数：356 / 4597 / 5621 / 296；秒级点数：54 / 690 / 843 / 44。测点为参考文件中的模拟点，生成类型均为递增1，采集周期需确认。":"候选点数为 290 / 290 / 100 / 900 / 140 / 130，仅用于帮助填写。采集周期与比例也需核对，修改后需要重新确认。";
 $("collectHint").textContent=micro?"微电网 PCS、ESS、INV、BOX 各设备独立模拟采集与转发。":"网关使用内置模拟采集。BMS、GROUPBMS、CELL 共用采集通道，分为三段转发。";
 $("templateHint").textContent=micro?"微电网使用从 tb_gateway.json 提取的 PCS、ESS、INV、BOX 模拟测点模板，生成类型均为递增1。超过模板点数时才需允许占位补点。":"模板使用第 1 步选定的 XML 与 BMS CSV，告警规则最后覆盖；每次计算固定模板快照。";
 $("pointsConfirmed").checked=p.pointsConfirmed;
 $("typeRows").innerHTML=p.types.map(r=>'<tr data-kind="'+r.type+'"><td><strong>'+r.type+'</strong></td>'+["total","points",micro?"fastPoints":"fastPercent","fastMs","slowMs"].map(f=>'<td><input aria-label="'+r.type+' '+({total:"设备数量",points:"单设备点数",fastPercent:"秒级点比例",fastPoints:"单设备秒级点数",fastMs:"快速周期",slowMs:"其他周期"}[f])+'" type="number" data-field="'+f+'" min="'+(["points","fastMs","slowMs"].includes(f)?1:0)+'" value="'+esc(r[f]??"")+'" '+(f==="total"&&p.bindings.length&&!p.templateMode?'max="'+p.bindings.filter(b=>b.businessType===r.type).length+'"':"")+(f==="fastPercent"?' max="100"':"")+'></td>').join("")+'<td></td></tr>').join("");
 for(const k of gatewayFields){if($(k).type==="checkbox")$(k).checked=p.gateway[k];else $(k).value=p.gateway[k]??gatewayDefaults[k]??"";}
 renderAlarmRules(p.gateway.alarmRules||[]);$('templateSettings').hidden=micro;
 $('forwardMode').querySelector('option[value="aggregate"]').disabled=micro;
 $('templatePoints').innerHTML='';$('templateSourceReport').hidden=true;templateItems=[];templatePage=0;templateTotal=0;updateSharedSettings();
 for(const k of cloudFields){if($(k).type==="checkbox")$(k).checked=!!p.cloud[k];else $(k).value=p.cloud[k]??"";}
 $("provisionBtn").disabled=!!p.bindings.length;
 $("previewResourcesBtn").disabled=!!p.bindings.length;
 $("generateBtn").disabled=true;$("planSummary").className="empty";$("planSummary").textContent="还没有计算计划。完成点数确认并保存后计算。";$("templatesReport").innerHTML="";$("planRows").innerHTML="";
 if(p.testRound){$('typeRows').querySelectorAll('[data-field="total"]').forEach(el=>el.readOnly=true);}
 $('capacityBox').hidden=!!p.testRound;
 metrics();renderMapping();renderStacks();updateWorkflow();
}
function values(fields){return Object.fromEntries(fields.map(k=>[k,$(k).type==="checkbox"?$(k).checked:$(k).value]));}
async function save(){
 requireProject();const body={revision:project.revision,name:$("projectName").value,types:typeRows(),microgrid:{stationCount:$("stationCount").value},gateway:gatewayValues(),cloud:values(cloudFields),pointsConfirmed:$("pointsConfirmed").checked};
 render(await api("/projects/"+project.id+"/save",body));notice("已保存为新版本。");await refreshProjects();
}
async function refreshProjects(){
 overviewRows=await api('/overview');renderOverview();
 const trash=await api("/deleted-projects");$("deletedProjectList").innerHTML=trash.length?trash.map(p=>'<div class="list-item"><div><strong>'+esc(p.name)+'</strong><p>'+fmt(p.devices)+' 台设备 · 版本 '+p.revision+'</p></div><div class="actions"><button data-restore-project="'+p.id+'" data-revision="'+p.revision+'">恢复</button><button class="delete-project" data-purge-project="'+p.id+'" data-revision="'+p.revision+'">永久删除</button></div></div>').join(""):'<div class="empty">回收站为空。</div>';
}
function bindingSelected(b){
 const count=project.types.find(r=>r.type===b.businessType)?.total||0;
 const index=Number(b.collectAlias.slice(b.businessType.length));
 return index>=1&&index<=count;
}
function renderMapping(){
 if(!project)return;
 const q=$("mappingSearch").value.toLowerCase();
 const rows=project.bindings.filter(b=>(b.platformName+" "+b.platformId+" "+b.businessType+" "+b.collectAlias).toLowerCase().includes(q));
 const pages=Math.max(1,Math.ceil(rows.length/25));mapPage=Math.min(mapPage,pages-1);
 $("mappingRows").innerHTML=rows.slice(mapPage*25,mapPage*25+25).map(b=>'<tr><td>'+b.businessType+'</td><td>'+esc(b.collectAlias)+'</td><td>'+esc(b.platformName)+'</td><td>'+esc(b.platformId)+'</td><td>'+(bindingSelected(b)?"本次使用":"本次不使用 · 保留绑定")+' · 未联机验证</td></tr>').join("");
 $("mapPage").textContent=(mapPage+1)+" / "+pages+" · "+rows.length+" 台";$("mapPrev").disabled=mapPage===0;$("mapNext").disabled=mapPage>=pages-1;
 $("mappingIssues").innerHTML=project.mappingIssues.length?'<div class="note warning">'+project.mappingIssues.map(esc).join("<br>")+"</div>":'<div class="note">'+(project.bindings.length?"设备池已绑定。本次按各类型采集编号选取前 N 台，未选设备保留，可恢复数量后再次生成；无需重复创建平台资源。":"尚未绑定设备，请先创建平台资源。")+"</div>";
}
function renderStacks(){
 if(!project)return;
 if(!$('mapping').open||!$('stackSection').open){$('stacks').innerHTML='';return;}
 const bs=project.bindings.filter(b=>b.businessType==="BMS").sort((a,b)=>a.stackIndex-b.stackIndex);
 const candidates={GROUPBMS:project.bindings.filter(b=>b.businessType==='GROUPBMS'),CELL:project.bindings.filter(b=>b.businessType==='CELL')};
 const byIndex={GROUPBMS:new Map(candidates.GROUPBMS.map(b=>[b.stackIndex,b])),CELL:new Map(candidates.CELL.map(b=>[b.stackIndex,b]))};
 const selected=(kind,index)=>byIndex[kind].get(index)||candidates[kind][0];
 const selector=(kind,index)=>{const b=selected(kind,index);return '<select data-kind="'+kind+'" aria-label="对应 '+kind+'">'+(b?'<option value="'+esc(b.logicalId)+'">'+esc(b.platformName)+'</option>':'')+'</select>';};
 $("stacks").innerHTML=bs.map(b=>'<tr data-bms="'+esc(b.logicalId)+'"><td>'+esc(b.platformName)+'</td><td>'+selector('GROUPBMS',b.stackIndex)+'</td><td>'+selector('CELL',b.stackIndex)+'</td></tr>').join("");
}
function loadStackOptions(event){
 const select=event.target.closest('select[data-kind]');
 if(!select||select.dataset.loaded||!project)return;
 const value=select.value,kind=select.dataset.kind;
 select.innerHTML=project.bindings.filter(b=>b.businessType===kind).map(b=>'<option value="'+esc(b.logicalId)+'">'+esc(b.platformName)+'</option>').join('');
 select.value=value;select.dataset.loaded='true';
}
function showPlan(p){
 plan=p;$("planSummary").className="metrics";
 $("planSummary").innerHTML=[["工程",p.summary.projectCount],["采集通道",p.summary.collectChannels],["转发通道",p.summary.forwardChannels],["业务点数",p.summary.totalPoints]].map(([k,v])=>'<div><span>'+k+'</span><strong>'+fmt(v)+'</strong></div>').join("");
 const kinds=project.profile==='microgrid'?['PCS','ESS','INV','BOX']:['PCS','BMS','GROUPBMS','CELL','CL','BOX'];
 $("planHead").innerHTML=['工程',...kinds,'设备总数','采集通道','转发通道','业务点数'].map(label=>'<th>'+label+'</th>').join('');
 $("planRows").innerHTML=p.projects.map(r=>'<tr><td>'+esc(r.name)+'</td>'+kinds.map(kind=>'<td>'+fmt(r.counts?.[kind]??0)+'</td>').join('')+'<td>'+fmt(r.devices)+'</td><td>'+fmt(r.collectChannels)+'</td><td>'+fmt(r.forwardChannels)+'</td><td>'+fmt(r.points)+'</td></tr>').join("");
 $("templatesReport").innerHTML='<div class="note">'+p.templates.map(t=>p.profile==='microgrid'?t.type+"：模拟模板共 "+t.templatePoints+" 点，占位补点 "+t.fillPoints+" 点/台":t.type+"：真实 "+t.realPoints+" 点，占位 "+t.fillPoints+" 点/台").map(esc).join("　")+'<br>'+(p.templateMode?'含模板设备或凭据，平台未验证；需先导入 CSV 并核对凭据。':'实际设备身份已绑定；凭据与平台上报由人工检查。')+'</div>';
 $('templatesReport').innerHTML+=p.templates.map(t=>'<details><summary>'+esc(t.type)+' 最终测点与告警规则</summary><p>'+esc((t.overrides||[]).map(r=>r.name+'：类型 '+r.generatorType+'，周期 '+r.interval+' ms').join('；')||'未启用告警模拟规则')+'</p><table><thead><tr><th>点名</th><th>描述</th><th>生成类型</th><th>最终周期 ms</th></tr></thead><tbody>'+(t.sample||[]).map(r=>'<tr><td>'+esc(r.tag)+'</td><td>'+esc(r.description)+'</td><td>'+esc(r.generator)+'</td><td>'+esc(r.interval)+'</td></tr>').join('')+'</tbody></table><p>最多展示前 100 个最终测点。</p></details>').join('');
 $("generateBtn").disabled=false;updateWorkflow();
}
function jobMarkup(j){return '<div class="list-item"><div><strong>'+esc(j.projectName||j.id.slice(0,8))+'</strong><p>'+esc(new Date(j.created).toLocaleString())+' · '+esc(j.message)+'</p><span class="job-state">'+esc({running:'执行中',completed:'已完成',failed:'失败',interrupted:'已中断'}[j.state]||j.state)+(j.total?' · '+j.done+'/'+j.total:'')+'</span></div>'+(j.retrySafe?'<button data-retry-project="'+esc(j.projectId)+'">修正配置后重试</button>':'')+(j.kind==='gateway'&&j.state==='completed'?'<div class="actions"><a href="/api/jobs/'+j.id+'/download">下载工程包（ZIP，含 CSV）</a><a class="button-link" href="/deployment?job='+j.id+'">部署本轮</a></div>':'')+'</div>';}
function renderJobs(){const kind=$('historyKind').value;const rows=allJobs.filter(j=>kind==='all'||j.kind===kind);$('jobs').innerHTML=rows.length?rows.map(jobMarkup).join(''):'<div class="empty">暂无此类记录。</div>';}
async function refreshJobs(){
 allJobs=await api('/jobs');renderJobs();
 const id=project?.id;
 if(id&&!dirty&&!allJobs.some(j=>j.projectId===id&&j.state==='running')){
  const fresh=await api('/projects/'+id);
  if(project?.id===id&&!dirty&&fresh.revision!==project.revision){render(fresh);notice('设备身份已更新。',false,'cloud');await refreshProjects();}
 }
 updateWorkflow();clearTimeout(timer);
 if(allJobs.some(j=>j.state==='running'))timer=setTimeout(()=>refreshJobs().catch(e=>notice(e.message,true)),1800);
}
document.querySelectorAll(".rail nav button").forEach(b=>b.onclick=()=>{if(!busy)switchView(b.dataset.view);});
$("saveBtn").onclick=()=>guarded(save);
$("newBtn").onclick=()=>guarded(async()=>{if(dirty&&!confirm("当前修改尚未保存，是否切换？"))return;render(await api("/projects",{name:"新建联调工程"}));await refreshProjects();switchView("config");notice("先设置设备数量与测点，按步骤继续配置采集与转发。");});
$("microgridBtn").onclick=()=>guarded(async()=>{if(dirty&&!confirm("当前修改尚未保存，是否切换？"))return;render(await api("/projects",{name:"微电网专用工程",profile:"microgrid"}));await refreshProjects();switchView("config");notice("已创建独立微电网方案，请确认电站数、秒级点数及采集周期。");});
$("stationCount").oninput=()=>{if(project?.profile!=="microgrid")return;$("asset_template").value=$("stationCount").value?"电站,"+$("stationCount").value+",powerStation":"";$("pointsConfirmed").checked=false;metrics();change();};
$("importBtn").onclick=()=>guarded(async()=>{if(dirty&&!confirm("当前修改尚未保存，是否切换？"))return;const id=$("sourceRun").value;if(!id)throw Error("没有可导入记录");render(await api("/import",{runId:id,profile:$("importProfile").value}));await refreshProjects();switchView("config");notice("已只读导入 "+project.bindings.length+" 台设备。点数需要补充确认，未进行平台写入。");});
$("projectList").onclick=e=>guarded(async()=>{const button=e.target.closest("button");if(!button)return;if(button.dataset.newRound){if(dirty)throw Error('请先保存当前配置');render(await api('/projects/'+button.dataset.newRound));await openRound();return;}const deleted=button.dataset.deleteProject;if(deleted){if(!confirm("将工程「"+button.dataset.name+"」移入回收站？可以恢复；平台资源和历史工程包保留。"+(project?.id===deleted&&dirty?"当前未保存的修改将丢弃。":"")))return;await api("/projects/"+deleted+"/delete",{revision:Number(button.dataset.revision)});if(project?.id===deleted){project=null;plan=null;dirty=false;$("projectBar").hidden=true;switchView("home");}await refreshProjects();notice("工程已移入回收站，可随时恢复。平台设备、资产和历史工程包未删除。");return;}const id=button.dataset.project;if(!id)return;if(dirty&&!confirm("当前修改尚未保存，是否切换？"))return;render(await api("/projects/"+id));switchView("config");});
$("candidateBtn").onclick=()=>{if(!project)return;[...$("typeRows").rows].forEach(tr=>tr.querySelector('[data-field="points"]').value=(project.profile==="microgrid"?{PCS:356,ESS:4597,INV:5621,BOX:296}:candidate)[tr.dataset.kind]);$("pointsConfirmed").checked=false;metrics();change();notice("已填入候选值，请核对点数和周期后勾选确认并保存。");};
$("typeRows").oninput=e=>{if(!e.target.dataset.field)return;const tr=e.target.closest("tr");if(e.target.dataset.field==="total"&&tr.dataset.kind==="BMS"){for(const k of ["GROUPBMS","CELL"])document.querySelector('tr[data-kind="'+k+'"] [data-field="total"]').value=e.target.value;}$("pointsConfirmed").checked=false;metrics();change();};
for(const k of ["projectName","pointsConfirmed",...gatewayFields,...cloudFields])$(k).addEventListener("input",change);
$("mappingSearch").oninput=()=>{mapPage=0;renderMapping();};
$("mapPrev").onclick=()=>{mapPage--;renderMapping();};$("mapNext").onclick=()=>{mapPage++;renderMapping();};
$("stacksBtn").onclick=()=>guarded(async()=>{clean();const mapping=[...$("stacks").rows].map(tr=>({BMS:tr.dataset.bms,GROUPBMS:tr.querySelectorAll("select")[0].value,CELL:tr.querySelectorAll("select")[1].value}));render(await api("/projects/"+project.id+"/stacks",{revision:project.revision,mapping}));notice("组合绑定已确认，已生成新版本。");});
$("planBtn").onclick=()=>guarded(async()=>{clean();showPlan(await api("/projects/"+project.id+"/plan",{revision:project.revision}));notice("配置和映射校验完成，可以生成工程包。");});
$("generateBtn").onclick=()=>guarded(async()=>{clean();if(!plan)throw Error("请先计算计划");await api("/projects/"+project.id+"/generate",{planId:plan.planId,key:"gen-"+plan.planId});await refreshJobs();notice("已开始生成，下方显示进度和下载入口。");});
$("refreshJobs").onclick=()=>guarded(refreshJobs);

$("previewResourcesBtn").onclick=()=>guarded(async()=>{
 if(project?.templateMode){
  await saveIfNeeded();
  if(!confirm('将按设备映射中的实际名称查询整个模板设备池及本轮共享通道，读取真实 ID、凭据和关系。全部查询成功后更新本地绑定，不创建或修改平台资源。请先完成 CSV 导入；绑定成功后需重新生成工程包。确认查询？'))return;
  try{await api('/projects/'+project.id+'/bind-template-pool',{revision:project.revision,auth:values(authFields)});await refreshJobs();}
  finally{authFields.forEach(k=>$(k).value='');updateWorkflow();}
  return;
 }
 await ensureCloudReady();
 const total=project.types.reduce((n,r)=>n+r.total,0);
 const names=project.types.filter(r=>r.total).map(r=>r.type+"："+project.cloud.device_name_prefix+r.type.toLowerCase()+"1 ～ "+project.cloud.device_name_prefix+r.type.toLowerCase()+r.total).join("\n");
 const message="查询并绑定已有设备\n目标平台："+project.cloud.base_url+"\n匹配已有设备："+total+"台\n"+names+"\n\n只读查询已有设备、凭据和关联关系，保存到本地工程。\n不创建或修改平台设备、资产、关系、规则链和边缘。\n设备缺失时停止，不自动补建。确认查询并复用？";
 if(!confirm(message))return;
 lookupAttempts.add(project.id);
 try{await api("/projects/"+project.id+"/provision",{revision:project.revision,auth:values(authFields),reuseExistingDevices:true});await refreshJobs();}
 finally{authFields.forEach(k=>$(k).value="");updateWorkflow();}
});

$('provisionBtn').onclick=()=>guarded(async()=>{
 clean();if(reviewRevision!==project.revision)throw Error('请先预览当前版本的创建清单');
 await ensureCloudReady();
 if(!confirm('确认在 '+project.cloud.base_url+' 创建清单中的设备、资产及关系？此操作会写入平台。'))return;
 await api('/projects/'+project.id+'/provision',{revision:project.revision,auth:values(authFields)});
 authFields.forEach(k=>$(k).value='');reviewRevision=null;$('provisionBtn').hidden=true;await refreshJobs();
});
window.addEventListener("beforeunload",e=>{if(dirty){e.preventDefault();e.returnValue="";}});
guarded(async()=>{const b=await api("/bootstrap");token=b.token;candidate=b.candidate;gatewayDefaults=b.defaults;$("sourceRun").innerHTML=b.history.map(r=>'<option value="'+esc(r.runId)+'">'+esc(r.runId)+' · '+r.devices+' 台 · '+esc(r.status)+'</option>').join("");if(b.history.some(r=>r.runId==="run-20260912150402-c8f2aa"))$("sourceRun").value="run-20260912150402-c8f2aa";await refreshProjects();await refreshJobs();});

function renderMicrogridStats(rows){
 if(project?.profile!=="microgrid")return;
 const seconds=rows.reduce((a,r)=>a+r.total*(r.fastPoints||0),0),points=rows.reduce((a,r)=>a+r.total*(r.points||0),0);
 $("microgridStats").textContent="秒级点总数："+fmt(seconds)+" · 总点数突变率："+(points?(seconds/points*100).toFixed(6):"0")+"% · 参考文件中的模拟测点模板";
 const n=Number($("stationCount").value);
 if(!Number.isInteger(n)||n<1||n>10000){$("stationPreview").textContent="填写电站数量后显示平均分配预览。";return;}
 const stationRows=Array.from({length:Math.min(n,50)},(_,i)=>{const counts=rows.map(r=>Math.floor(r.total/n)+(i<r.total%n?1:0));return "<tr><td>电站 "+(i+1)+"</td>"+counts.map(x=>"<td>"+fmt(x)+"</td>").join("")+"<td>"+fmt(counts.reduce((a,x)=>a+x,0))+"</td></tr>";}).join("");
 $("stationPreview").innerHTML="<table><thead><tr><th>电站</th>"+rows.map(r=>"<th>"+esc(r.type)+"</th>").join("")+"<th>设备合计</th></tr></thead><tbody>"+stationRows+"</tbody></table>"+(n>50?"<p>显示前50个电站，全部电站按相同规则分配。</p>":"");
}

for(const id of ['jobs','cloudJob','gatewayJob'])$(id).addEventListener('click',e=>guarded(async()=>{const id=e.target.dataset.retryProject;if(!id)return;if(dirty&&!confirm("当前修改尚未保存，是否切换？"))return;render(await api("/projects/"+id));switchView("config");notice("请按步骤检查配置。上次在连接预检失败，未创建资源。请核对平台地址和端口，保存后重新提交；登录凭据需重新填写。");}));

$("deletedProjectList").onclick=e=>guarded(async()=>{
 const button=e.target.closest("button");if(!button)return;
 const revision=Number(button.dataset.revision);
 if(button.dataset.restoreProject){await api("/projects/"+button.dataset.restoreProject+"/restore",{revision});await refreshProjects();notice("工程已恢复，可从继续已有工程中打开。");return;}
 if(button.dataset.purgeProject){
  const name=button.closest('.list-item').querySelector('strong').textContent;
  const confirmation=prompt('永久删除本地工程“'+name+'”及其关联记录和生成包。不会删除平台设备或服务器文件。请输入完整工程名称确认：');
  if(confirmation===null)return;
  await api('/projects/'+button.dataset.purgeProject+'/purge',{revision,confirmName:confirmation});
  await refreshProjects();notice('工程及关联的本地文件已永久删除。');
 }
});

let capacityPreview=null;
function capacitySignature(){return JSON.stringify({id:project?.id,revision:project?.revision,rows:typeRows(),target:$("capacityTarget").value,mode:$("capacityMode").value});}
function setupCapacity(p){
 capacityPreview=null;$("capacityApply").disabled=true;
 const storage=p.profile==='storage';
 $("capacityModeLabel").hidden=storage;
 $("capacityTarget").type=storage?'number':'text';
 $("capacityTarget").value=storage?'50':'10万';
 $("capacityTarget").parentElement.firstChild.textContent=storage?'项目容量（MWh）':'目标业务点数';
 const kinds=p.profile==="microgrid"?["PCS","ESS","INV","BOX"]:["PCS","BMS","CL","BOX"];
 $("capacityMode").innerHTML='<option value="ratio">按当前设备数量比例</option>'+kinds.map(k=>'<option value="'+k+'">'+(k==="BMS"?'BMS 组合（BMS + GROUPBMS + CELL）':'仅 '+k)+'</option>').join('');
 const enabled=p.types.filter(r=>r.total>0).map(r=>r.type);
 $("capacityMode").value=storage?'ratio':enabled.length===1&&kinds.includes(enabled[0])?enabled[0]:(enabled.length>1?'ratio':'PCS');
 $("capacityCalculate").disabled=false;
 $("capacityResult").textContent=storage?'填写容量和六类单台点数后计算；按 50 MWh 基准寻找最接近目标点数的数量，并显示储能单元个数。':p.bindings.length&&!p.templateMode?'可按目标点数选择本次生成数量；各类型不能超过已绑定设备池上限。':'选择分配方式后计算。应用后仍需确认点数并保存配置。';
}
$("capacityCalculate").onclick=()=>guarded(async()=>{
 requireProject();const signature=capacitySignature();
 const storage=project.profile==='storage';
 const body={revision:project.revision,types:typeRows()};
 if(storage)body.capacityMwh=$("capacityTarget").value;
 else{body.target=$("capacityTarget").value;body.mode=$("capacityMode").value;}
 const result=await api('/projects/'+project.id+'/capacity',body);
 if(signature!==capacitySignature())throw Error('输入已变化，请重新计算');
 capacityPreview={signature,result};$("capacityApply").disabled=false;
 $("capacityResult").textContent=(storage?'容量 '+result.capacityMwh+' MWh，储能单元 '+fmt(result.unitCount)+' 个，目标 ':'目标 ')+fmt(result.target)+' 点，实际 '+fmt(result.actual)+' 点，差值 '+fmt(result.difference)+' 点；共 '+fmt(result.devices)+' 台。'+Object.entries(result.totals).filter(([k,n])=>n>0).map(([k,n])=>k+' '+fmt(n)+'台').join('，')+'。'+result.note;
});
$("capacityApply").onclick=()=>guarded(async()=>{
 requireProject();
 if(!capacityPreview||capacityPreview.signature!==capacitySignature())throw Error('输入已变化，请重新计算');
 for(const tr of $("typeRows").rows)tr.querySelector('[data-field="total"]').value=capacityPreview.result.totals[tr.dataset.kind];
 if(project.profile==='storage'){
  const lines=$("asset_template").value.split(/\r?\n/);
  const index=lines.findIndex(line=>line.split(',')[0].trim()==='储能单元');
  if(index<0)lines.push('储能单元,'+capacityPreview.result.unitCount+',energyStationUnit');
  else{const columns=lines[index].split(',');columns[1]=String(capacityPreview.result.unitCount);lines[index]=columns.join(',');}
  $("asset_template").value=lines.join('\n');
 }
 $("pointsConfirmed").checked=false;metrics();change();$("capacityApply").disabled=true;capacityPreview=null;
 notice('本次设备数量已填入表格，请核对并勾选确认，再保存配置。');
});

function configurationKey(){
 if(!project)return '';
 return JSON.stringify({id:project.id,profile:project.profile,types:typeRows(),gateway:gatewayValues(),sharedPlatform:$("forwardMode").value==="aggregate"?[$("base_url").value,$("device_name_prefix").value]:null,confirmed:$('pointsConfirmed').checked,station:$('stationCount').value});
}
function businessDevicesReady(){return !!project?.bindings.length&&['SUCCESS','TEMPLATE'].includes(project.sourceStatus)&&!project.mappingIssues.length;}
function sharedBindingReady(item){return project.sharedBindings?.some(b=>b.platformName===item.platformName&&b.platformUrl===project.cloud.base_url.replace(/\/+$/,'')&&b.credentialRef&&!b.credentialsPending);}
function devicesReady(){return businessDevicesReady()&&(project.gateway.forwardMode!=='aggregate'||sharedSpecs!==null&&sharedSpecs.every(sharedBindingReady));}
function updateWorkflow(){
 $('workflowSteps').hidden=!project||!stepViews.includes(currentView);
 if(!project)return;
 $('newRoundBtn').disabled=dirty||!businessDevicesReady()||project.templateMode||allJobs.some(j=>j.projectId===project.id&&j.state==='running');
 $('poolRefreshPanel').hidden=!project.bindings.length||!!project.testRound||!!project.templateMode;
 $('refreshPoolBtn').disabled=dirty||allJobs.some(j=>j.projectId===project.id&&j.state==='running');
 renderSharedDevices();
 const checked=checkedKey!==''&&checkedKey===configurationKey();
 if(!checked)$('configurationCheck').hidden=true;
 const total=project.types.reduce((n,r)=>n+r.total,0),pool=project.testRound?.poolDevices??project.bindings.length;
 const jobs=allJobs.filter(j=>j.projectId===project.id);
 jobs.sort((a,b)=>String(b.created).localeCompare(String(a.created)));
 const cloud=jobs.find(j=>j.kind==='cloud'),gateway=jobs.find(j=>j.kind==='gateway');
 const running=jobs.some(j=>j.state==='running');
 const state=[project.pointsConfirmed&&!dirty?'已保存':'待确认',checked?'已检查':'待检查',devicesReady()?'已有设备可复用':project.mappingIssues.length?'待修正设备映射':cloud?.state==='running'?'准备中':cloud?.state==='failed'?'准备失败':'待准备',gateway?.state==='running'?'生成中':gateway?.state==='completed'&&gateway.planId===plan?.planId?'已生成':plan?'可生成':'待计算'];
 stepViews.forEach((v,i)=>{$('stepState-'+v).textContent=state[i];const button=document.querySelector('[data-step="'+v+'"]');button.classList.toggle('current',v===currentView);button.setAttribute('aria-current',v===currentView?'step':'false');});
 $('platformSetup').hidden=!!pool&&!project.templateMode;
 $('platformAuthTitle').textContent=project.templateMode?'绑定真实设备池':'准备平台资源';
 $('platformAuthDescription').textContent=project.templateMode?'CSV 导入完成后，使用租户管理员账号查询真实设备身份与关系。查询失败保留原模板绑定，可修正平台数据后重试。凭据仅用于本次查询。':'创建操作会在指定平台创建资产、设备及关系；查询操作只读取已有资源。使用已有租户管理员账号和密码，凭据仅用于本次任务。';
 if(project.mappingIssues.length)$('mapping').open=true;
 $('devicePoolSummary').textContent=pool?'设备池 '+fmt(pool)+' 台，本轮使用 '+fmt(total)+' 台，保留 '+fmt(Math.max(0,pool-total))+' 台。复用已有设备，无需重复创建平台资源。':'本次需要 '+fmt(total)+' 台设备。配置完成后，可创建平台资源，或查询并绑定平台上已有的同名设备。';
 if(project.templateMode)$('devicePoolSummary').textContent='当前为模板设备池。请先将 CSV 导入平台，再查询并绑定真实设备。成功后重新生成工程包，即可使用真实身份进行验收和后续轮次测试。';
 const blockedCloud=cloud&&!(cloud.state==='failed'&&cloud.retrySafe)&&!pool;
 $('reviewResourcesBtn').disabled=running||!!blockedCloud||!!pool;
 $('previewResourcesBtn').disabled=running||(!project.templateMode&&(!!blockedCloud||!!pool));
 $('previewResourcesBtn').textContent=project.templateMode?'查询并绑定模板池真实设备':'查询并绑定已有设备';
 $('provisionBtn').disabled=running||!!blockedCloud||!!pool;
 $('cloudNext').disabled=!canContinueTemplates()||dirty||!checked||running;
 $('cloudNext').textContent=devicesReady()&&!project.templateMode?'继续生成工程与 CSV 模板':project.templateMode?'继续生成工程与 CSV 模板（平台未验证）':'本地生成工程与 CSV（无需创建设备）';
 $('configNext').disabled=running;$('gatewayNext').disabled=running;
 $('planBtn').disabled=!devicesReady()||dirty||!checked||running;
 $('generateBtn').disabled=!plan||dirty||!checked||running;
 for(const [job,id] of [[cloud,'cloudJob'],[gateway,'gatewayJob']]){
  const failed=job&&['failed','interrupted'].includes(job.state);
  $(id).hidden=!job;$(id).classList.toggle('task-failed',!!failed);
  $(id).setAttribute('role',failed?'alert':'status');
  const markup=job?'<p class="task-caption">'+(failed?'任务失败，请先处理以下问题':'最近一次'+(job.kind==='gateway'?'网关生成':job.reuseExistingDevices?'已有设备绑定':'平台资源')+'任务')+'</p>'+jobMarkup(job):'';
  if($(id).innerHTML!==markup)$(id).innerHTML=markup;
 }
 $('generationOverview').innerHTML='<strong>本次生成清单</strong><p>设备池 '+fmt(pool)+' 台 · 本轮使用 '+fmt(total)+' 台 · 保留 '+fmt(Math.max(0,pool-total))+' 台 · 配置版本 '+project.revision+'</p><p>'+project.types.filter(r=>r.total).map(r=>esc(r.type)+'：'+fmt(r.total)+' 台 × '+fmt(r.points||0)+' 点，周期 '+fmt(r.fastMs)+' / '+fmt(r.slowMs)+' ms').join('；')+'</p><p>MQTT：'+esc(project.gateway.mqttHost)+':'+esc(project.gateway.mqttPort)+'；全量发布周期 '+esc(project.gateway.pubTimer)+' 秒</p>';
}
async function saveIfNeeded(){if(dirty)await save();}
async function checkConfiguration(){
 clean();const id=project.id,key=configurationKey();
 const result=await api('/projects/'+id+'/configuration-check',{revision:project.revision});
 if(project.id!==id||key!==configurationKey())throw Error('配置已变化，请重新检查');
 sharedSpecs=result.sharedDevices||[];checkedKey=key;$('configurationCheck').hidden=false;
 $('configurationCheck').textContent='配置检查通过：'+fmt(result.summary.totalDevices)+' 台设备，'+fmt(result.summary.totalPoints)+' 个业务点；模板点数和周期已检查。';
 updateWorkflow();return result;
}
async function ensureCloudReady(){
 await saveIfNeeded();
 if(!project.pointsConfirmed)throw Error('请先完成第 1 步设备与测点确认');
 if(project.bindings.length)throw Error('已有设备绑定，请直接确认复用');
 await checkConfiguration();
}
$('configNext').onclick=()=>guarded(async()=>{
 requireProject();if(!$('pointsConfirmed').checked)throw Error('请勾选确认各类型点数与采集周期');
 if(!typeRows().some(r=>r.total>0))throw Error('至少选择一台设备');
 await saveIfNeeded();switchView('gateway');notice('设备与测点已保存，请配置采集与转发。');
});
$('gatewayNext').onclick=()=>guarded(async()=>{await saveIfNeeded();await checkConfiguration();switchView('cloud');notice('采集配置检查通过，请准备平台设备。');});
$('cloudNext').onclick=()=>guarded(async()=>{
 clean();
 render(await api('/projects/'+project.id+'/continue-templates',{revision:project.revision}));
 await checkConfiguration();switchView('preview');
 notice(project.templateMode?'当前使用本地模板凭据，无需先创建设备；计算并生成后，可在历史记录下载包含网关工程和 CSV 的 ZIP 包。':'设备已绑定；计算并生成后，可在历史记录下载包含网关工程和 CSV 的 ZIP 包。',false,'preview');
});
$('reviewResourcesBtn').onclick=()=>guarded(async()=>{
 await ensureCloudReady();reviewRevision=project.revision;
 $('resourceReview').hidden=false;
 $('resourceReview').textContent='目标平台：'+project.cloud.base_url+'；设备：'+project.types.filter(r=>r.total).map(r=>r.type+' '+r.total+' 台').join('，')+'；共享通道设备：'+(sharedSpecs?.length||0)+' 台（已有复用、缺少补建）；设备前缀：'+project.cloud.device_name_prefix+'；资产模板：'+project.cloud.asset_template+'；边缘模式：'+project.cloud.edge_mode+'；默认规则链：'+(project.cloud.enable_rule_chain_creation?'创建':'不创建')+'。确认后将写入平台。';
 $('provisionBtn').hidden=false;notice('请核对下方创建清单，确认后再创建平台资源。');
});
$('historyKind').onchange=renderJobs;
document.querySelectorAll('[data-step]').forEach(b=>b.onclick=()=>{if(!busy)switchView(b.dataset.step);});
document.querySelectorAll('[data-back]').forEach(b=>b.onclick=()=>{if(busy)return;const name=b.dataset.back;if(dirty){currentView=name;document.querySelectorAll('.view').forEach(v=>v.hidden=v.id!==name);$('title').textContent=viewNames[name];updateWorkflow();}else switchView(name);});
$('mapping').addEventListener('toggle',()=>{if($('mapping').open){renderMapping();renderStacks();}});
$('stackSection').addEventListener('toggle',renderStacks);
$('stacks').addEventListener('pointerdown',loadStackOptions);
$('stacks').addEventListener('focusin',loadStackOptions);


function generatorOptions(){return ['递增1（按1递增）','最大最小值之间随机变化','静态值','递增2（条件递增）','0和1随机切换','静态字符串','布尔值互相切换'];}
function renderAlarmRules(rules){
 $('alarmRows').innerHTML=['PCS','BMS'].map(kind=>{const r=rules.find(x=>x.type===kind)||{enabled:false,description:'',generatorType:6,interval:1000,var1:'',var2:''};return '<tr data-kind="'+kind+'"><td><input type="checkbox" data-rule="enabled" '+(r.enabled?'checked':'')+' aria-label="启用 '+kind+' 规则"></td><td>'+kind+'</td><td><input data-rule="description" value="'+esc(r.description)+'" aria-label="'+kind+' 采集点描述"></td><td><select data-rule="generatorType" aria-label="生成类型">'+generatorOptions().map((label,i)=>'<option value="'+i+'" '+(Number(r.generatorType)===i?'selected':'')+'>'+esc(label)+'</option>').join('')+'</select></td><td><input data-rule="interval" type="number" min="1" max="2147483647" value="'+esc(r.interval)+'" aria-label="周期毫秒"></td><td><input data-rule="var1" value="'+esc(r.var1)+'" aria-label="参数1"></td><td><input data-rule="var2" value="'+esc(r.var2)+'" aria-label="参数2"></td></tr>';}).join('');
}
function alarmValues(){return [...$('alarmRows').rows].map(tr=>{const r={type:tr.dataset.kind};tr.querySelectorAll('[data-rule]').forEach(el=>r[el.dataset.rule]=el.type==='checkbox'?el.checked:el.value);return r;});}
function gatewayValues(){return {...values(gatewayFields),alarmRules:project?.profile==='microgrid'?[]:alarmValues()};}
function updateSharedSettings(){$('sharedSettings').hidden=$('forwardMode').value!=='aggregate';}
function templateGuard(fn){return guarded(fn,'template');}
async function loadTemplatePoints(page=0){
 notice('',false,'template');requireProject();const result=await api('/template-points',{gateway:gatewayValues(),type:$('templateKind').value,search:$('templateSearch').value,page});
 templatePage=result.page;templateTotal=result.total;templateItems=result.points;
 $('templateSourceReport').hidden=false;$('templateSourceReport').textContent=Object.entries(result.counts).map(([k,v])=>k+'：'+fmt(v)+' 点').join('；')+'。来源：'+result.sources.map(s=>s.name+'（'+s.sha256.slice(0,12)+'）').join('、');
 $('templatePoints').innerHTML=result.points.map((p,i)=>'<tr><td>'+esc(p.tag)+'</td><td>'+esc(p.description)+'</td><td>'+esc(generatorOptions()[Number(p.generator)]||p.generator)+'</td><td>'+esc(p.interval)+'</td><td>'+(['PCS','BMS'].includes($('templateKind').value)?'<button data-alarm-point="'+i+'">用于告警规则</button>':'—')+'</td></tr>').join('');
 $('templatePage').textContent=(templatePage+1)+' / '+Math.max(1,Math.ceil(templateTotal/100))+' · '+fmt(templateTotal)+' 个测点';$('templatePrev').disabled=templatePage===0;$('templateNext').disabled=(templatePage+1)*100>=templateTotal;
}
$('alarmRows').addEventListener('input',change);
$('forwardMode').addEventListener('change',updateSharedSettings);
$('builtinTemplates').onclick=()=>templateGuard(async()=>{requireProject();for(const k of ['templateDbPath','templateBmsCsvPath'])$(k).value=gatewayDefaults[k];change();await loadTemplatePoints();});
$('reloadTemplates').onclick=()=>templateGuard(async()=>{change();checkedKey='';await loadTemplatePoints();notice('已重新读取模板；保存后重新计算生成计划，历史工程包不变。',false,'template');});
$('templateSearchBtn').onclick=()=>templateGuard(()=>loadTemplatePoints());
$('templateKind').onchange=()=>templateGuard(()=>loadTemplatePoints());
$('templatePrev').onclick=()=>templateGuard(()=>loadTemplatePoints(Math.max(0,templatePage-1)));
$('templateNext').onclick=()=>templateGuard(()=>loadTemplatePoints(templatePage+1));
$('templatePoints').onclick=e=>{const button=e.target.closest('[data-alarm-point]');if(!button)return;const point=templateItems[Number(button.dataset.alarmPoint)],kind=$('templateKind').value;if(!point)return;const row=[...$('alarmRows').rows].find(r=>r.dataset.kind===kind);if(!row)return;row.querySelector('[data-rule="description"]').value=point.description;row.querySelector('[data-rule="enabled"]').checked=true;change();notice('已选择 '+kind+' 测点，请设置生成类型与周期后保存。',false,'template');};


function renderSharedDevices(){
 const shared=project.gateway.forwardMode==='aggregate';$('sharedDeviceSetup').hidden=!shared;if(!shared)return;
 const rows=sharedSpecs||[],ready=rows.filter(sharedBindingReady).length;
 $('sharedDeviceSummary').textContent=sharedSpecs===null?'请先在第 2 步检查配置，计算所需通道设备。':'本次需要 '+rows.length+' 台共享通道设备，已绑定 '+ready+' 台，待准备 '+(rows.length-ready)+' 台。业务设备数量不包含这些通道设备。';
 $('sharedDeviceList').textContent=rows.slice(0,100).map(x=>x.platformName+(sharedBindingReady(x)?' · 已绑定':' · 待准备')).join('；')+(rows.length>100?'；仅展示前 100 台':'');
 const job=allJobs.find(j=>j.projectId===project.id&&j.kind==='shared');$('sharedDeviceJob').innerHTML=job?jobMarkup(job):'';
 const blocked=!businessDevicesReady()||dirty||checkedKey!==configurationKey()||allJobs.some(j=>j.state==='running');$('sharedRead').disabled=blocked;$('sharedCreate').disabled=blocked;
}
async function prepareShared(createMissing){
 clean();if(!businessDevicesReady())throw Error('请先准备业务设备');
 if(createMissing&&!confirm('在 '+project.cloud.base_url+' 补建缺少的共享通道设备并绑定凭据？共需 '+(sharedSpecs?.length||0)+' 台，已有设备复用。'))return;
 await api('/projects/'+project.id+'/shared-devices',{revision:project.revision,createMissing,auth:{existing_tenant_admin_username:$('sharedUsername').value,existing_tenant_admin_password:$('sharedPassword').value}});
 $('sharedUsername').value='';$('sharedPassword').value='';await refreshJobs();
}
$('sharedRead').onclick=()=>guarded(()=>prepareShared(false));
$('sharedCreate').onclick=()=>guarded(()=>prepareShared(true));

let roundPreviewBody=null, roundSelection=[], overviewRows=[];
const stateLabel=s=>({running:'运行中',starting:'启动中',recovering:'恢复中',recovery_required:'需人工恢复',prepared:'待部署',completed:'已完成',failed:'失败',interrupted:'已中断'}[s]||s);
function renderOverview(){
 const q=$("projectSearch").value.trim().toLowerCase();
 const pools=new Map();for(const p of overviewRows){const id=p.testRound?.poolId||p.id;if(!pools.has(id))pools.set(id,{pool:null,rounds:[]});if(p.testRound)pools.get(id).rounds.push(p);else pools.get(id).pool=p;}
 const row=p=>'<tr><td><strong>'+esc(p.name)+'</strong><small>版本 '+p.revision+'</small></td><td data-label="设备数">'+fmt(p.devices)+'</td><td data-label="生成">'+esc(p.generation?(p.generation.current?stateLabel(p.generation.state):'历史版本 · '+stateLabel(p.generation.state)):'待生成')+'</td><td data-label="部署">'+esc(p.deployment?stateLabel(p.deployment.state):'未部署')+'</td><td data-label="验收">'+(p.deployment?'<a href="/deployment?deployment='+p.deployment.id+'">'+esc(({passed:'阈值通过',failed:'未达标',incomplete:'证据不完整'})[p.deployment.acceptanceState]||(p.deployment.assessmentAt?'人工观测 '+fmt(p.deployment.observedDevices)+' 台':'待核验'))+'</a><small>执行版本 '+p.deployment.revision+'</small>':'待验收')+'</td><td><div class="actions"><button data-project="'+p.id+'">打开</button><button data-new-round="'+p.id+'">复制为下一轮</button>'+(p.generation?.state==='completed'?'<a class="button-link" href="/deployment?job='+p.generation.id+'">部署</a>':'')+'<details><summary>更多</summary><button class="delete-project" data-delete-project="'+p.id+'" data-revision="'+p.revision+'" data-name="'+esc(p.name)+'">移入回收站</button></details></div></td></tr>';

 $("projectList").innerHTML=[...pools.values()].filter(g=>!q||[g.pool,...g.rounds].some(p=>p?.name.toLowerCase().includes(q))).map(g=>{
 const p=g.pool,meta=g.rounds[0]?.testRound;
 return '<article class="pool-card"><div class="section-head"><div><h3>'+esc(p?.name||meta.poolName)+'</h3><p>'+fmt(p?.boundDevices??meta.poolDevices)+' 台已绑定 · '+g.rounds.length+' 个测试轮次'+(!p?' · 原设备池已移入回收站':'')+'</p></div><div class="actions">'+(p?'<button data-project="'+p.id+'">配置设备池</button><button class="primary" data-new-round="'+p.id+'" '+(!p.boundDevices?'disabled':'')+'>新建测试轮次</button>':'')+'</div></div>'+(g.rounds.length?'<div class="table-wrap"><table><thead><tr><th>轮次</th><th>设备数</th><th>生成</th><th>部署</th><th>验收</th><th>操作</th></tr></thead><tbody>'+g.rounds.map(row).join('')+'</tbody></table></div>':'<p class="empty">'+(p?.boundDevices?'设备池已就绪，点击“新建测试轮次”开始。':'先配置并绑定平台设备，再创建测试轮次。')+'</p>')+(p?'<details><summary>设备池管理与历史生成</summary><div class="actions">'+(p.generation?.state==='completed'?'<a class="button-link" href="/deployment?job='+p.generation.id+'">部署已有工程包</a>':'')+'<button class="delete-project" data-delete-project="'+p.id+'" data-revision="'+p.revision+'" data-name="'+esc(p.name)+'">移入回收站</button></div></details>':'')+'</article>';
 }).join('')||'<div class="empty">'+(q?'没有匹配的设备池或轮次。':'暂无设备池，请展开下方“新建设备池 / 导入历史设备”。')+'</div>';
}
$("projectSearch").oninput=renderOverview;
$("refreshOverview").onclick=()=>guarded(refreshProjects);
$("navToggle").onclick=()=>{const open=document.body.classList.toggle('nav-open');$("navToggle").setAttribute('aria-expanded',String(open));$("navToggle").textContent=open?'收起导航':'展开导航';};
let selectedPoolRevision=null;
function roundBody(){return {poolMode:$("roundPoolMode").value,poolRevision:selectedPoolRevision,revision:project.revision,name:$("roundName").value,counts:Object.fromEntries([...$("roundCounts").querySelectorAll("input")].map(i=>[i.dataset.kind,Number(i.value)]))};}
async function openRound(){
 clean();$('roundPoolMode').value='snapshot';const pool=await api('/projects/'+project.id+'/round-pool');selectedPoolRevision=pool.poolRevision;$('roundTarget').value=project.types.reduce((n,r)=>n+r.total,0);$('roundAllocation').textContent='';
 $("roundName").value=project.name+' · 下一轮';
 $("roundPoolInfo").textContent=pool.poolName+' · 设备池版本 '+pool.poolRevision+' · 共 '+fmt(pool.poolDevices)+' 台';
 $("roundCounts").innerHTML=project.types.map(r=>'<label>'+esc(r.type)+'（可用 '+fmt(pool.counts[r.type]||0)+'）<input type="number" min="0" max="'+(pool.counts[r.type]||0)+'" data-kind="'+r.type+'" value="'+r.total+'" '+(project.profile!=='microgrid'&&['GROUPBMS','CELL'].includes(r.type)?'readonly':'')+'></label>').join('');
 $("roundPreview").hidden=true;$("roundDetails").hidden=true;$("roundDetails").open=false;$("roundError").hidden=true;
 $("roundCreateBtn").hidden=true;$("roundPreviewBtn").hidden=false;roundPreviewBody=null;roundSelection=[];
 $("roundPanel").showModal();$("roundName").focus();
}
$("newRoundBtn").onclick=()=>guarded(openRound);
$("roundCounts").oninput=e=>{
 if(e.target.dataset.kind==='BMS')for(const kind of ['GROUPBMS','CELL'])$("roundCounts").querySelector('[data-kind="'+kind+'"]').value=e.target.value;
};
$("roundPanel").oninput=e=>{
 if(e.target.id==='roundSearch')return;
 $("roundCreateBtn").hidden=true;$("roundPreviewBtn").hidden=false;$("roundPreview").hidden=true;$("roundDetails").hidden=true;$("roundError").hidden=true;roundPreviewBody=null;
};
$("roundCancelBtn").onclick=()=>$("roundPanel").close();
function renderRoundDevices(){const q=$("roundSearch").value.toLowerCase();const rows=roundSelection.filter(b=>(b.name+' '+b.type+' '+b.id).toLowerCase().includes(q));$("roundMatchCount").textContent='匹配 '+fmt(rows.length)+' 台，显示前 100 台；可搜索定位具体设备。';$("roundDeviceRows").innerHTML=rows.slice(0,100).map(b=>'<tr><td>'+esc(b.type)+'</td><td>'+esc(b.name)+'</td><td>'+esc(b.id)+'</td></tr>').join('');}
$("roundSearch").oninput=renderRoundDevices;
$("roundPreviewBtn").onclick=()=>guarded(async()=>{
 const body=roundBody(),signature=JSON.stringify(body),r=await api('/projects/'+project.id+'/round-preview',body);
 if(signature!==JSON.stringify(roundBody()))throw Error('输入已变化，请重新预览');
 roundPreviewBody=signature;roundSelection=r.selection;$("roundSearch").value='';renderRoundDevices();
 $("roundPreview").innerHTML='<p>设备池版本 '+r.poolRevision+' · 新增选用 '+r.changes.added+' 台 · 不再选用 '+r.changes.removed+' 台 · 凭据变化 '+r.changes.credentialsChanged+' 台</p><div class="metrics">'+[['设备池',r.poolDevices],['本轮选用',r.devices],['未选用',r.poolDevices-r.devices],['业务点数',r.points]].map(([label,value])=>'<div><span>'+label+'</span><strong>'+fmt(value)+'</strong></div>').join('')+'</div><div class="table-wrap"><table><thead><tr><th>设备类型</th><th>来源轮次 / 工程</th><th>本轮</th><th>变化</th></tr></thead><tbody>'+Object.keys(r.counts).map(k=>'<tr><td>'+esc(k)+'</td><td>'+fmt(r.previousCounts[k])+'</td><td>'+fmt(r.counts[k])+'</td><td>'+fmt(r.counts[k]-r.previousCounts[k])+'</td></tr>').join('')+'</tbody></table></div>';
 $("roundPreview").hidden=false;$("roundDetails").hidden=false;$("roundPreviewBtn").hidden=true;$("roundCreateBtn").hidden=false;$("roundCreateBtn").focus();
});
$("roundCreateBtn").onclick=()=>guarded(async()=>{
 const body=roundBody();if(JSON.stringify(body)!==roundPreviewBody)throw Error('配置已变化，请重新预览');
 const next=await api('/projects/'+project.id+'/rounds',body);$('roundPanel').close();render(next);await refreshProjects();switchView('config');
 notice('轮次已创建，请配置本轮测点与转发参数。');
});

$('refreshPoolBtn').onclick=()=>guarded(async()=>{
 clean();if(project.testRound)throw Error('请打开原设备池刷新凭据');
 const refreshed=await api('/projects/'+project.id+'/refresh-pool',{revision:project.revision,auth:{existing_tenant_admin_username:$('poolUsername').value,existing_tenant_admin_password:$('poolPassword').value}});
 $('poolUsername').value='';$('poolPassword').value='';render(refreshed);await refreshProjects();notice('设备池凭据已更新，已有轮次保持原快照。',false,'cloud');
});

$('roundPoolMode').onchange=()=>guarded(async()=>{
 const mode=$('roundPoolMode').value, pool=await api('/projects/'+project.id+'/round-pool?mode='+mode);
 if(mode!==$('roundPoolMode').value)return;
 selectedPoolRevision=pool.poolRevision;
 $('roundPoolInfo').textContent=pool.poolName+' · 设备池版本 '+pool.poolRevision+' · 共 '+fmt(pool.poolDevices)+' 台';
 for(const input of $('roundCounts').querySelectorAll('input')){input.max=pool.counts[input.dataset.kind]||0;input.value=Math.min(Number(input.value),Number(input.max));input.parentElement.firstChild.textContent=input.dataset.kind+'（可用 '+fmt(Number(input.max))+'）';}
 $('roundPanel').oninput({target:{id:'roundPoolMode'}});
});
$('roundAllocate').onclick=()=>guarded(async()=>{
 const body={...roundBody(),targetDevices:Number($('roundTarget').value)},signature=JSON.stringify(body);
 const result=await api('/projects/'+project.id+'/round-allocation',body);
 if(signature!==JSON.stringify({...roundBody(),targetDevices:Number($('roundTarget').value)}))throw Error('输入已变化，请重新分配');
 for(const input of $('roundCounts').querySelectorAll('input'))input.value=result.counts[input.dataset.kind];
 $('roundPanel').oninput({target:{id:'roundTarget'}});
 $('roundAllocation').textContent='目标 '+fmt(result.targetDevices)+' 台 → 实际 '+fmt(result.actualDevices)+' 台，差额 '+result.difference+' 台。'+result.note;
});
