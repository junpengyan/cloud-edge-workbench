const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

async function run() {
 const base = path.resolve(__dirname, '..');
 const html = fs.readFileSync(path.join(base, 'static/index.html'), 'utf8');
 const source = fs.readFileSync(path.join(base, 'static/app.js'), 'utf8');
 const ids = [...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]);
 assert.equal(new Set(ids).size, ids.length, 'duplicate HTML id');
 class Element {
  constructor(id, type='text') { this.id=id;this.type=type;this.dataset={};this.hidden=false;this.disabled=false;this.checked=false;this.value='';this.textContent='';this.rows=[];this.options=[];this.attrs={};this.listeners={};this.parentElement={firstChild:{textContent:''}};this.classList={toggle(){}}; }
  set value(v) { this._value=String(v??''); } get value(){return this._value;}
  set innerHTML(s) {
   this._html=s;
   if(this.id==='typeRows') this.rows=[...s.matchAll(/<tr data-kind="([^"]+)">([\s\S]*?)<\/tr>/g)].map(m=>{
    const tr=new Element('row');tr.dataset.kind=m[1];tr.lastElementChild=new Element('last');
    tr.inputs=[...m[2].matchAll(/<input\b([^>]+)>/g)].map(n=>{const input=new Element('input','number');input.dataset.field=/data-field="([^"]+)"/.exec(n[1])[1];input.value=/value="([^"]*)"/.exec(n[1])[1];input.closest=()=>tr;return input;});
    tr.querySelectorAll=()=>tr.inputs;tr.querySelector=q=>tr.inputs.find(i=>q.includes('"'+i.dataset.field+'"'));return tr;
   });
   if(this.type==='select') {this.options=[...s.matchAll(/<option value="([^"]*)"/g)].map(m=>({value:m[1]}));if(this.options.length&&!this.options.some(o=>o.value===this.value))this.value=this.options[0].value;}
  }
  get innerHTML(){return this._html||'';}
  addEventListener(k,fn){this.listeners[k]=fn;}
  setAttribute(k,v){this.attrs[k]=v;} removeAttribute(k){delete this.attrs[k];}
  querySelector(){return new Element('option');}
 }
 const elements=new Map(ids.map(id=>[id,new Element(id)]));
 for(const m of html.matchAll(/<(input|select)\b([^>]*\bid="([^"]+)"[^>]*)>/g)){const el=elements.get(m[3]);el.type=m[1]==='select'?'select':(/type="([^"]+)"/.exec(m[2])?.[1]||'text');el.value=/value="([^"]*)"/.exec(m[2])?.[1]||'';}
 elements.get('historyKind').value='gateway';
 const steps=['config','gateway','cloud','preview'].map(step=>{const e=new Element(step+'Button');e.dataset.step=step;return e;});
 const nav=['home','config','history'].map(view=>{const e=new Element(view+'Nav');e.dataset.view=view;return e;});
 const views=['home','config','gateway','cloud','preview','history'].map(id=>elements.get(id));
 const doc={body:new Element('body'),getElementById(id){assert(elements.has(id),'unknown element '+id);return elements.get(id);},querySelectorAll(q){if(q==='.view')return views;if(q==='.rail nav button')return nav;if(q==='[data-step]')return steps;if(q==='[data-back]')return [];throw Error('unknown selector '+q);},querySelector(q){if(q.startsWith('[data-step='))return steps.find(b=>q.includes('"'+b.dataset.step+'"'));if(q.startsWith('tr[data-kind=')){const kind=/data-kind="([^"]+)"/.exec(q)[1];return elements.get('typeRows').rows.find(r=>r.dataset.kind===kind).querySelector(q);}throw Error('unknown selector '+q);}};
 const clone=x=>JSON.parse(JSON.stringify(x));
 let p={id:'fixture',name:'offline fixture',revision:1,profile:'storage',sourceStatus:null,bindings:[],mappingIssues:[],types:[{type:'PCS',total:2,points:10,fastPercent:20,fastMs:1000,slowMs:86400000}],pointsConfirmed:false,gateway:{projectPrefix:'test',maxProjectPoints:100000,maxForwardTags:6000,strategy:'balanced',forwardMode:'direct',mqttHost:'fixture.invalid',mqttPort:1883,mqttQos:1,mqttPub:'telemetry',mqttSub:'rpc',changePeriod:1,pubTimer:300,allowFill:false},cloud:{base_url:'http://fixture.invalid',device_name_prefix:'test-',asset_name_prefix:'test-',asset_template:'公司,1,Company',edge_mode:'none',edge_names:'',edge_count:0,enable_rule_chain_creation:false}};
 let jobs=[],calls=[],checkFails=false;
 const summary={totalDevices:2,totalPoints:20,projectCount:1,collectChannels:2,forwardChannels:2};
 const ctx=vm.createContext({document:doc,window:{addEventListener(){}},confirm:()=>true,setTimeout:()=>1,clearTimeout(){},fetch:async(url,options)=>{
  const body=options?.body?JSON.parse(options.body):null;calls.push({url,body});let result;
  if(url==='/api/bootstrap')result={token:'synthetic',candidate:{PCS:10},history:[],defaults:{}};
  else if(url==='/api/overview')result=[];
  else if(url==='/api/projects')result=[p];else if(url==='/api/deleted-projects')result=[];else if(url==='/api/jobs')result=jobs;
  else if(url.endsWith('/save')){p={...p,...body,revision:p.revision+1};result=p;}
  else if(url.endsWith('/configuration-check')){if(checkFails)return {ok:false,json:async()=>({message:'模板不足'})};result={summary,templates:[]};}
  else if(url.endsWith('/plan'))result={planId:'plan-fixture',summary,projects:[{name:'test1',counts:{PCS:2},devices:2,collectChannels:2,forwardChannels:2,points:20}],templates:[]};
  else if(url.endsWith('/continue-templates')){p.revision++;if(!p.bindings.length){p.templateMode=true;p.sourceStatus='TEMPLATE';p.bindings=[1,2].map(i=>({businessType:'PCS',collectAlias:'pcs'+i,platformName:'test-pcs'+i,platformId:''}));}result=p;}
  else if(url.endsWith('/provision')||url.endsWith('/bind-template-pool')){jobs=[{id:'cloud-fixture',projectId:p.id,kind:'cloud',state:'running',created:'2026-09-20',message:'正在准备'}];result=jobs[0];}
  else if(url.endsWith('/generate')){jobs.unshift({id:'gen-fixture',projectId:p.id,planId:body.planId,kind:'gateway',state:'running',created:'2026-09-21',message:'正在生成'});result=jobs[0];}
  else if(url==='/api/projects/fixture')result=p;else throw Error('unmocked request '+url);
  return {ok:true,json:async()=>clone(result)};
 }});
 vm.runInContext(source,ctx);
 await new Promise(resolve=>setImmediate(resolve));
 ctx.fixture=clone(p);vm.runInContext('render(fixture);switchView("config")',ctx);
 await elements.get('configNext').onclick();assert.match(elements.get('notice-config').textContent,/勾选/);
 elements.get('pointsConfirmed').checked=true;vm.runInContext('change()',ctx);
 await elements.get('configNext').onclick();assert.equal(vm.runInContext('currentView',ctx),'gateway',elements.get('notice-config').textContent);
 checkFails=true;await elements.get('gatewayNext').onclick();assert.equal(vm.runInContext('currentView',ctx),'gateway');assert.equal(elements.get('notice-gateway').textContent,'模板不足');
 checkFails=false;await elements.get('gatewayNext').onclick();assert.equal(vm.runInContext('currentView',ctx),'cloud');
 assert.equal(calls.filter(c=>c.url.endsWith('/provision')).length,0,'checking must not create resources');
 await elements.get('reviewResourcesBtn').onclick();assert.equal(elements.get('provisionBtn').hidden,false);assert.equal(calls.filter(c=>c.url.endsWith('/provision')).length,0);
 await elements.get('provisionBtn').onclick();assert.equal(vm.runInContext('currentView',ctx),'cloud');assert.match(elements.get('cloudJob').innerHTML,/正在准备/);
 jobs[0].state='failed';jobs[0].message='部分资源创建失败';await vm.runInContext('refreshJobs()',ctx);assert.match(elements.get('cloudJob').innerHTML,/部分资源创建失败/);assert.equal(elements.get('provisionBtn').disabled,true);
 jobs[0].state='completed';p.revision++;p.sourceStatus='SUCCESS';p.bindings=[1,2].map(i=>({businessType:'PCS',collectAlias:'pcs'+i,platformName:'fixture'+i,platformId:'d'+i}));await vm.runInContext('refreshJobs()',ctx);
 assert.equal(elements.get('platformSetup').hidden,true);assert.equal(elements.get('cloudNext').disabled,false);
 await elements.get('cloudNext').onclick();assert.equal(vm.runInContext('currentView',ctx),'preview');await elements.get('planBtn').onclick();assert.match(elements.get('planHead').innerHTML,/GROUPBMS/);assert.match(elements.get('planRows').innerHTML,/<td>2<\/td>/);await elements.get('generateBtn').onclick();assert.equal(vm.runInContext('currentView',ctx),'preview');assert.match(elements.get('gatewayJob').innerHTML,/正在生成/);
 jobs[0].state='completed';jobs[0].message='文件校验通过';await vm.runInContext('refreshJobs()',ctx);assert.match(elements.get('gatewayJob').innerHTML,/下载工程包/);assert(!elements.get('jobs').innerHTML.includes('部分资源创建失败'));
 vm.runInContext('switchView("config")',ctx);elements.get('typeRows').rows[0].inputs.find(i=>i.dataset.field==='total').value=1;elements.get('typeRows').rows[0].inputs.find(i=>i.dataset.field==='points').value=20;vm.runInContext('change()',ctx);
 await elements.get('configNext').onclick();await elements.get('gatewayNext').onclick();assert.match(elements.get('devicePoolSummary').textContent,/本轮使用 1 台，保留 1 台/);assert.equal(elements.get('platformSetup').hidden,true);assert.equal(calls.filter(c=>c.url.endsWith('/provision')).length,1,'reuse must not create devices again');
 p.mappingIssues=['组合关系待确认'];p.revision++;await vm.runInContext('refreshJobs()',ctx);
 assert.equal(elements.get('mapping').open,true);assert.equal(elements.get('cloudNext').disabled,true);
 assert.equal(elements.get('stepState-cloud').textContent,'待修正设备映射');
 p.bindings=[];p.mappingIssues=[];p.sourceStatus=null;p.revision++;jobs=[];await vm.runInContext('refreshJobs()',ctx);
 await elements.get('gatewayNext').onclick();await elements.get('previewResourcesBtn').onclick();
 assert.equal(elements.get('cloudNext').disabled,true,'wait for lookup to finish');
 jobs[0].state='failed';jobs[0].reuseExistingDevices=true;jobs[0].message='未找到设备';
 await vm.runInContext('refreshJobs()',ctx);
 assert.equal(elements.get('cloudNext').disabled,false,'failed lookup must allow next step');
 await elements.get('cloudNext').onclick();assert.equal(vm.runInContext('currentView',ctx),'preview');
 assert.equal(elements.get('planBtn').disabled,false,'template identities allow gateway generation');
 assert.match(elements.get('notice-preview').textContent,/ZIP/);
 assert.equal(elements.get('platformSetup').hidden,false,'template conversion entry remains visible');
 assert.equal(elements.get('previewResourcesBtn').disabled,false);
 assert.equal(elements.get('provisionBtn').disabled,true,'template pool must not create resources');
 assert.equal(elements.get('poolRefreshPanel').hidden,true,'template pool cannot refresh by empty platform IDs');
 await elements.get('previewResourcesBtn').onclick();
 assert.equal(calls.at(-2).url.endsWith('/bind-template-pool'),true);
 assert.equal(elements.get('previewResourcesBtn').disabled,true,'prevent concurrent conversion');
 jobs[0].state='failed';await vm.runInContext('refreshJobs()',ctx);
 assert.equal(elements.get('previewResourcesBtn').disabled,false,'failed conversion remains retryable');
 p.templateMode=false;p.sourceStatus='SUCCESS';p.revision++;p.bindings.forEach((b,i)=>b.platformId='real-'+i);
 await vm.runInContext('refreshJobs()',ctx);
 assert.equal(elements.get('platformSetup').hidden,true);
 assert.equal(elements.get('newRoundBtn').disabled,false);
 p.bindings=[];p.sourceStatus=null;p.templateMode=false;p.revision++;jobs=[];
 await vm.runInContext('refreshJobs()',ctx);vm.runInContext('switchView("gateway")',ctx);
 await elements.get('gatewayNext').onclick();assert.equal(elements.get('cloudNext').disabled,false,'uncreated devices can use local templates directly');
 const queries=calls.filter(c=>c.url.endsWith('/provision')).length;
 await elements.get('cloudNext').onclick();assert.equal(vm.runInContext('currentView',ctx),'preview');
 assert.equal(calls.filter(c=>c.url.endsWith('/provision')).length,queries,'local templates do not query or create platform devices');
 const large=[];
 for(const [kind,prefix] of [['BMS','bms'],['GROUPBMS','groupbms'],['CELL','cell']])for(let i=1;i<=900;i++)large.push({logicalId:prefix+i,businessType:kind,collectAlias:prefix+i,platformName:prefix+i,platformId:'id-'+prefix+i,stackIndex:i});
 p={...p,bindings:large,mappingIssues:[],sourceStatus:'SUCCESS'};
 elements.get('mapping').open=false;elements.get('stackSection').open=false;
 ctx.largeProject=p;vm.runInContext('render(largeProject)',ctx);
 assert.equal(elements.get('stacks').innerHTML,'','opening a large pool must not build every stack selector');
 elements.get('mapping').open=true;elements.get('stackSection').open=true;
 vm.runInContext('renderStacks()',ctx);
 assert.equal([...elements.get('stacks').innerHTML.matchAll(/<tr /g)].length,900);
 assert.equal([...elements.get('stacks').innerHTML.matchAll(/<option /g)].length,1800,'only selected options are built initially');
 const select=new Element('selectedStack','select');select.dataset.kind='CELL';select.value='cell1';select.closest=()=>select;
 ctx.stackTarget=select;vm.runInContext('loadStackOptions({target:stackTarget})',ctx);
 assert.equal(select.options.length,900,'candidate options are built only for the opened selector');
 return 'PASS: unique IDs, step prerequisites, local template failure, creation preview has no writes, creation failure stays local, binding reuse, in-place generation/download, repeated point/count changes without platform creation.';
}
module.exports={run};
if(require.main===module)run().then(console.log).catch(e=>{console.error(e);process.exitCode=1;});
