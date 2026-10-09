"use strict";
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let token='', selected=null, busy=false, timer, assessmentDirty=false, deploymentRows=[];
const stateLabel=s=>({prepared:'待部署',starting:'启动中',running:'运行中',recovering:'恢复中',recovery_required:'需人工恢复',completed:'已结束',failed:'失败'}[s]||s);
const durationSeconds=()=>Number($('durationValue').value)*Number($('durationUnit').value);

const targetLabels={minConnectedChannels:'最低 MQTT 连接成功数 条',minReportingPercent:'最低上报比例 %',minRatePerSecond:'最低上报速率 条/秒',maxP95LatencyMs:'最高 P95 延迟 ms',maxLossPercent:'最高丢失率 %',maxErrorPercent:'最高错误率 %'};
const targetKeys=['minConnectedChannels','minReportingPercent','minRatePerSecond','maxP95LatencyMs','maxLossPercent','maxErrorPercent'];
const metricKeys=['connectedChannels','ratePerSecond','p95LatencyMs','lossPercent','errorPercent'];
const nullableNumber=id=>$(id).value===''?null:Number($(id).value);
const testOptions=()=>({warmupSeconds:Number($('warmupSeconds').value),criteria:Object.fromEntries(targetKeys.map(k=>[k,nullableNumber(k)]))});
const credentials=()=>Object.fromEntries(['sshUsername','sshPassword'].map(k=>[k,$(k).value]));
function notice(t,scope='prepare',error=false){const el=$(scope+'Notice');el.hidden=!t;el.textContent=t;el.classList.toggle('error',error);}
async function api(path,body){const r=await fetch('/api'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Workspace-Token':token},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.message||'请求失败');return d;}
async function act(fn,scope='prepare'){if(busy)return;busy=true;notice('',scope);document.body.setAttribute('aria-busy','true');try{await fn();}catch(e){notice(e.message,scope,true);}finally{busy=false;document.body.removeAttribute('aria-busy');}}
function show(j){const changed=selected?.id!==j.id;if(selected?.id!==j.id||selected?.state!==j.state)notice('','deploy');selected=j;$('summary').hidden=false;$('deploySection').hidden=false;$('assessmentSection').hidden=!j.remoteStatus?.startedAtMs;$('assessmentContext').textContent=j.sourceName+' · 本轮 '+j.devices.length+' 台';$('observedDevices').max=j.devices.length;$('summary').className='';$('summary').innerHTML='<p><strong>'+esc(j.sourceName)+'</strong> · 版本 '+j.revision+' · '+esc(j.options.host)+':'+j.options.port+'</p><p>平台：'+esc(j.platformUrl)+'；MQTT：'+esc(j.mqttHost)+':'+j.mqttPort+'</p><p>部署目录：'+esc(j.options.remotePath)+'</p><p>'+j.devices.slice(0,8).map(d=>esc(d.type)+'：'+esc(d.name)+'（'+d.points+'点）').join('　')+(j.devices.length>8?' … 共 '+j.devices.length+' 台设备':'')+'</p><a href="/api/deployments/'+j.id+'/download">下载部署工程</a> · <a href="/api/deployments/'+j.id+'/report" target="_blank" rel="noopener">下载报告</a>';
 $('summary').insertAdjacentHTML('afterbegin','<p>执行清单：'+esc(j.manifestId||'历史记录')+' · 固定版本 '+j.revision+'</p><p>验收阈值：'+esc(Object.entries(j.options.criteria||{}).filter(([k,v])=>v!==null).map(([k,v])=>targetLabels[k]+'：'+v).join('；'))+'</p><p>预热 '+(j.options.warmupSeconds||0)+' 秒 · 正式采样 '+(j.options.sampleSeconds??j.options.durationSeconds)+' 秒</p><p>业务点数：'+(j.configuredPoints??j.devices.reduce((n,d)=>n+d.points,0))+' · 转发通道：'+(j.forwardChannels??'—')+'</p><p><strong>设备池：'+(j.poolDevices??'—')+' 台 · 本轮配置：'+j.devices.length+' 台</strong></p><p>子工程：'+esc((j.options.projectNames||[j.options.projectName]).join('、'))+(j.options.deploymentMode==='formal'?' · 验收观察时长：':' · 运行时长：')+j.options.durationSeconds+' 秒</p>');
 $('replacementSummary').hidden=!j.preflight;
 const switchPlan=j.preflight?.switchPlan;
 $('replacementSummary').innerHTML=switchPlan?'<strong>切换范围（配置目录，不代表连接成功）</strong><p>现有：'+esc(switchPlan.existing.map(r=>r.name+'（'+r.devices+'条设备配置）').join('、')||'无')+'</p><p>停用：'+esc(switchPlan.stop.join('、')||'无')+'</p><p>保留：'+esc(switchPlan.retain.join('、')||'无')+'</p><p>新增：'+esc(switchPlan.start.join('、'))+'</p><p>本轮 '+switchPlan.selectedDevices+' 台；切换后共 '+switchPlan.resultingDeviceEntries+' 条设备配置'+(switchPlan.totalKnown?'':'（存在无法识别的配置，实际总数未知）')+'。同一设备可能被多处配置，不能视为去重在线数。</p>':'';
 $('retainedLabel').hidden=!switchPlan?.retain.length;
 if(changed)$('acknowledgeRetained').checked=false;
 renderAcceptance(j,changed);
 $('check').classList.toggle('primary',!j.preflight);$('start').hidden=!j.preflight&&j.state==='prepared';
 $('start').textContent=j.options.deploymentMode==='formal'?'确认正式部署':'确认限时验证';$('stop').hidden=j.options.deploymentMode==='formal';$('check').disabled=j.state!=='prepared';$('start').disabled=j.state!=='prepared'||!j.preflight;$('stop').disabled=!['starting','running','recovering'].includes(j.state);$('deploymentStatus').hidden=false;$('deploymentStatus').textContent=j.message+(j.rollbackRestored?'；切换未成功，上一轮配置已回滚恢复':'');if(changed||!assessmentDirty){$('observedDevices').value=j.observedDevices??'';$('assessmentNotes').value=j.assessmentNotes||'';assessmentDirty=false;}}
async function refresh(){const rows=await api('/deployments');deploymentRows=rows;if(!selected){const requested=rows.find(j=>j.id===new URLSearchParams(location.search).get('deployment'));if(requested)show(requested);}renderPrevious();$('records').innerHTML=rows.map(j=>'<div class="list-item"><div><strong>'+esc(j.sourceName)+' · '+esc(j.options.projectName)+'</strong><p>'+esc(j.options.host)+' · '+esc(j.message)+'</p><p>'+esc(stateLabel(j.state))+' · 验收：'+esc(({passed:'阈值通过',failed:'未达标',incomplete:'证据不完整'})[j.acceptanceResult?.state]||'待核验')+'</p></div><button data-open="'+j.id+'">查看</button>'+(j.options.deploymentMode!=='formal'&&['running','starting','recovering','recovery_required'].includes(j.state)?'<button data-resume="'+j.id+'">恢复状态监测</button>':'')+'</div>').join('')||'<p>暂无部署记录</p>';if(selected){const fresh=rows.find(j=>j.id===selected.id);if(fresh)show(fresh);}clearTimeout(timer);if(rows.some(j=>['running','starting','recovering'].includes(j.state)||j.telemetryCheck?.state==='running'))timer=setTimeout(()=>refresh().catch(e=>notice(e.message,selected?'deploy':'records',true)),5000);}

let sources=[];
function clearPrepared(){
 selected=null;$('deploySection').hidden=true;$('assessmentSection').hidden=true;$('summary').hidden=true;notice('','deploy');$('deploymentStatus').hidden=true;$('deploymentStatus').textContent='';
 for(const id of ['check','start','stop'])$(id).disabled=true;
 $('summary').className='empty';$('summary').textContent='请选择工程并准备部署包。';
}
function sourceChanged(){
 clearPrepared();
 const generated=$('mode').value==='generated';
 $('memberLabel').hidden=!generated;
 const source=sources.find(x=>x.id===$('project').value);
 $('prepareFields').hidden=!$('project').value;$('sourceEmpty').hidden=!!$('project').value;
 $('member').innerHTML='<option value="*">整个生成批次（全部子工程）</option>'+(source?.testRound?[]:(source?.projects||[])).map(p=>'<option value="'+esc(p.name)+'">'+esc(p.name)+' · '+p.devices+'台 · '+p.points+'点</option>').join('');
 $('prepare').disabled=!$('project').value || (generated&&!$('member').value);
 $('remoteName').value=generated?'':'test4';
}
async function loadSources(){
 const previous=$('project').value;
 const generated=$('mode').value==='generated';
 if(generated){
   sources=await api('/deployment-sources');
   $('project').innerHTML=sources.map(p=>'<option value="'+p.id+'">'+esc(p.name)+' · 生成版本 '+p.revision+' · '+esc(new Date(p.created).toLocaleString())+'</option>').join('');
 }else{
   const projects=(await api('/projects')).filter(p=>p.profile==='microgrid');
   $('project').innerHTML=projects.map(p=>'<option value="'+p.id+'">'+esc(p.name)+'</option>').join('');
 }
 const preferred=previous||new URLSearchParams(location.search).get('job');if([...$('project').options].some(o=>o.value===preferred))$('project').value=preferred;
 sourceChanged();
 if(!$('project').value)notice(generated?'暂无已校验的生成工程，请先在工作台生成工程。':'暂无微电网工程。');
}
$('mode').onchange=()=>act(loadSources);
$('deploymentMode').onchange=clearPrepared;
$('reloadSources').onclick=()=>act(loadSources);
$('project').onchange=sourceChanged;
$('member').onchange=()=>{clearPrepared();$('remoteName').value='';};
for(const id of ['remoteName','durationValue','durationUnit','replaceProjects','warmupSeconds',...targetKeys])$(id).oninput=clearPrepared;
for(const id of ['host','port'])$(id).oninput=()=>{clearPrepared();renderPrevious();};
$('previousDeployment').onchange=()=>{clearPrepared();const previous=deploymentRows.find(r=>r.id===$('previousDeployment').value);$('replaceProjects').disabled=!!previous;if(previous)$('replaceProjects').value='';$('previousHint').textContent=previous?'将核对 '+(previous.options.projectNames||[previous.options.projectName]).join('、')+'；已归档的目录会自动跳过。':'只展示同一网关的记录；旧目录是否存在，以环境预检为准。';};
function renderPrevious(){const value=$('previousDeployment').value;const candidates=deploymentRows.filter(r=>(r.remoteRun||r.options.deploymentMode==='formal'&&r.state==='completed')&&r.options.host===$('host').value.trim()&&r.options.port===Number($('port').value));$('previousDeployment').innerHTML='<option value="">不替换历史工程</option>'+candidates.map(r=>'<option value="'+r.id+'" '+(['starting','running','recovering','recovery_required'].includes(r.state)?'disabled':'')+'>'+esc(r.sourceName)+' · '+esc(stateLabel(r.state))+' · '+esc(r.options.projectName)+'</option>').join('');if(candidates.some(r=>r.id===value&&!['starting','running','recovering','recovery_required'].includes(r.state)))$('previousDeployment').value=value;$('replaceProjects').disabled=!!$('previousDeployment').value;}
document.querySelectorAll('[data-duration]').forEach(button=>button.onclick=()=>{$('durationUnit').value=Number(button.dataset.duration)>=3600?'3600':'60';$('durationValue').value=Number(button.dataset.duration)/Number($('durationUnit').value);clearPrepared();});
$('navToggle').onclick=()=>{const open=document.body.classList.toggle('nav-open');$('navToggle').setAttribute('aria-expanded',String(open));$('navToggle').textContent=open?'收起导航':'展开导航';};

$('prepare').onclick=()=>act(async()=>{clearPrepared();if($('mode').value==='generated'){show(await api('/jobs/'+$('project').value+'/deployment',{...testOptions(),member:$('member').value,host:$('host').value,port:Number($('port').value),projectName:$('remoteName').value,deploymentMode:$('deploymentMode').value,durationSeconds:durationSeconds(),previousDeploymentId:$('previousDeployment').value,replaceProjects:$('replaceProjects').value.split(/\s+/).filter(Boolean)}));}else{const p=await api('/projects/'+$('project').value);show(await api('/projects/'+p.id+'/deployment',{...testOptions(),revision:p.revision,host:$('host').value,port:Number($('port').value),projectName:$('remoteName').value,deploymentMode:$('deploymentMode').value,durationSeconds:durationSeconds(),previousDeploymentId:$('previousDeployment').value,replaceProjects:$('replaceProjects').value.split(/\s+/).filter(Boolean)}));}notice('部署包已准备，请核对设备数量、生成版本和目标目录，再检查环境。');await refresh();});
$('check').onclick=()=>act(async()=>{notice('正在检查网关连接与环境…','deploy');$('acknowledgeRetained').checked=false;show(await api('/deployments/'+selected.id+'/check',credentials()));notice('网关连接与环境检查通过，可以开始部署。','deploy');},'deploy');
$('start').onclick=()=>act(async()=>{if(selected.preflight?.switchPlan?.retain.length&&!$('acknowledgeRetained').checked)throw Error('请先核对并确认保留工程');const formal=selected.options.deploymentMode==='formal';const effect=formal?'工程将长期保留在配置目录。':'运行 '+selected.options.durationSeconds+' 秒后归档。';if(!confirm('重启服务会短暂影响网关上的其他工程。\n将向 '+selected.options.host+' 部署 '+selected.options.remotePath+'，依次执行 systemctl stop sprixin-gateway 和 systemctl start sprixin-gateway。'+effect+'停用上一轮目录：'+(selected.verifiedReplaceProjects?.join('、')||'无')+'。保留工程：'+(selected.preflight?.switchPlan?.retain.join('、')||'无')+'；切换后设备配置条目：'+(selected.preflight?.switchPlan?.resultingDeviceEntries??'未知')+'。请在平台页面人工检查遥测。确认开始？'))return;notice('正在检查环境并启动部署…','deploy');show(await api('/deployments/'+selected.id+'/start',{...credentials(),switchPlanId:selected.preflight.switchPlanId,acknowledgeRetained:$('acknowledgeRetained').checked}));$('sshPassword').value='';notice('部署任务已启动，请在平台页面人工检查遥测。','deploy');await refresh();},'deploy');
$('stop').onclick=()=>act(async()=>{const r=await api('/deployments/'+selected.id+'/stop',{});notice(r.message,'deploy');},'deploy');
$('refresh').onclick=()=>act(refresh,'records');
$('records').onclick=e=>act(async()=>{const b=e.target.closest('button');if(b?.dataset.open)show(await api('/deployments/'+b.dataset.open));if(b?.dataset.resume){await api('/deployments/'+b.dataset.resume+'/resume',{});await refresh();notice('已恢复远端状态监测。','records');}},'records');
act(async()=>{const b=await api('/bootstrap');token=b.token;await loadSources();await refresh();});

$('saveAssessment').onclick=()=>act(async()=>{
 if(!selected)throw Error('请先打开一条部署记录');
 if($('observedDevices').value==='')throw Error('请填写实际观测设备数');
 const saved=await api('/deployments/'+selected.id+'/assessment',{observedDevices:Number($('observedDevices').value),notes:$('assessmentNotes').value,metrics:Object.fromEntries(metricKeys.map(k=>[k,nullableNumber(k)]))});assessmentDirty=false;show(saved);notice('人工验收记录已保存。','deploy');
},'deploy');

for(const id of ['observedDevices','assessmentNotes',...metricKeys])$(id).oninput=()=>{assessmentDirty=true;};

function renderAcceptance(j,changed){
 $('executionIdentity').textContent='执行版本 '+j.revision+' · 生成批次 '+(j.sourceJobId||'验证包')+' · 清单 '+(j.manifestId||'历史记录无清单')+'。结果始终关联此版本，不跟随编辑中的配置变化。';
 const started=j.remoteStatus?.startedAtMs, warmup=j.options.warmupSeconds||0;
 $('phaseStatus').textContent=j.options.deploymentMode==='formal'?'正式部署：工程长期保留 · 预热 '+warmup+' 秒 · 验收采样 '+(j.options.sampleSeconds??j.options.durationSeconds)+' 秒':'预热 '+warmup+' 秒 · 正式采样 '+(j.options.sampleSeconds??j.options.durationSeconds)+' 秒 · '+(!started?'待运行':Date.now()<started+warmup*1000?'预热中':['completed','failed','recovery_required'].includes(j.state)?'运行已结束':'正式采样中');
 const check=j.telemetryCheck;
 $('verifyTelemetry').disabled=!started||check?.state==='running'||!j.manifestId;
 $('telemetryResult').textContent=!check?'尚未执行遥测核验':check.state==='running'?'正在查询 '+check.checked+' / '+check.total+' 台':check.state==='failed'?check.message:'已上报 '+check.reported+' 台 · 缺失 '+check.missing+' 台 · 未查明 '+check.errors+' 台 · 上报比例 '+check.reportingPercent.toFixed(2)+'% · '+(check.window.complete?'完整采样窗口':'当前部分采样窗口')+'（'+new Date(check.window.startTs).toLocaleString()+' 至 '+new Date(check.window.endTs).toLocaleString()+'）';
 $('missingDevices').textContent=[...(check?.missingDevices||[]).map(r=>'缺失：'+r.name+' · '+r.id),...(check?.unknownDevices||[]).map(r=>'未查明：'+r.name+' · '+r.id)].slice(0,100).join('\n')||'暂无清单';
 $('acceptanceResult').textContent='验收：'+(({passed:'已达到所设阈值',failed:'未达到所设阈值',incomplete:'证据不完整，不能判定通过'})[j.acceptanceResult?.state]||'待核验')+'；MQTT 连接数及性能指标以人工测量记录为准。';
 if(changed||!assessmentDirty)for(const key of metricKeys)$(key).value=j.measuredMetrics?.[key]??'';
}
$('verifyTelemetry').onclick=()=>act(async()=>{
 const body={username:$('telemetryUsername').value,password:$('telemetryPassword').value};
 try{show(await api('/deployments/'+selected.id+'/telemetry-check',body));await refresh();}
 finally{$('telemetryPassword').value='';}
},'deploy');
