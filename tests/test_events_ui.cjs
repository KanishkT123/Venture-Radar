// Rendering-logic checks with a minimal DOM double; these do not replace browser visual QA.
const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const elements = new Map();
function element(selector){
  if(!elements.has(selector))elements.set(selector,{
    textContent:'',innerHTML:'',value:'',disabled:false,open:false,hidden:false,style:{},
    options:[{value:'180'}],listeners:{},attributes:{},
    setAttribute(k,v){this.attributes[k]=v;},
    addEventListener(k,fn){this.listeners[k]=fn;},
    showModal(){this.open=true;},close(){this.open=false;}
  });
  return elements.get(selector);
}
const fixture={settings:{venture:'Example workspace',size_weight:.55,half_life_days:180,schedule_enabled:false},
  investments:[],investors:[{name:'Fund A',grantor:false,score:0,sources:[]}],edges:[],partner_profiles:[],jobs:[],
  configured:true,hosted:false,investor_events:{events:[{title:'Example Boston forum',city:'Boston',
  starts_on:'2026-11-12',ends_on:null,start_time:'17:30',end_time:'19:00',event_url:'https://example.com/event',
  firms:[{firm:'Fund A',role:'sponsor',person:null,source_url:'https://example.com/source',evidence:'Public sponsorship.'}]}],last_checked_at:100}};
const requests=[];
const sandbox={console,URL,Intl,Date,Set,Array,String,Math,JSON,Number,
  sessionStorage:{getItem(){return null;},setItem(){}},
  document:{querySelector:element,querySelectorAll(){return [];},activeElement:{tagName:'BODY'},hidden:false},
  setInterval(){},setTimeout(){},clearTimeout(){},
  fetch:async(path,options={})=>{
    requests.push({path,options});
    if(options.method==='POST'){
      fixture.jobs=[{id:'event-job',kind:'events',status:'queued',created_at:200}];
      return {ok:true,json:async()=>({job_id:'event-job'})};
    }
    return {ok:true,json:async()=>structuredClone(fixture)};
  }};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'),'utf8'),sandbox);
(async()=>{
  await new Promise(setImmediate);
  const run=s=>vm.runInContext(s,sandbox);
  const rows=()=>element('#investor-event-rows').innerHTML;
  assert.match(rows(),/Example Boston forum/);
  assert.match(rows(),/5:30 PM – 7:00 PM ET/);
  assert.match(rows(),/Fund A/);
  assert.equal((rows().match(/<td /g)||[]).length,4);
  assert.match(rows(),/rel="noopener noreferrer"/);
  assert.equal(requests.filter(r=>r.options.method==='POST').length,0);
  run("state.investor_events.events[0].start_time=null;state.investor_events.events[0].end_time=null;renderInvestorEvents()");
  assert.match(rows(),/Time not published/);
  run("state.investor_events.events[0].title='<img src=x onerror=alert(1)>';state.investor_events.events[0].firms[0].source_url='javascript:alert(1)';renderInvestorEvents()");
  assert.doesNotMatch(rows(),/<img/);
  assert.match(rows(),/&lt;img/);
  assert.match(rows(),/href="#"/);
  run("state.jobs=[{kind:'events',status:'running'}];renderInvestorEvents()");
  assert.equal(element('#refresh-events').disabled,true);
  assert.equal(element('#investor-events-panel').attributes['aria-busy'],'true');
  run("state.jobs=[{kind:'events',status:'failed',created_at:200,error:'Example failure'}];renderInvestorEvents()");
  assert.match(element('#investor-events-status').textContent,/Saved results are still shown/);
  assert.match(rows(),/Fund A/);
  run('state.jobs=[];state.investor_events.events=[];renderInvestorEvents()');
  assert.match(rows(),/No publicly documented matches/);
  run('state.configured=false;renderInvestorEvents()');
  assert.equal(element('#refresh-events').disabled,true);
  run('state.configured=true;renderInvestorEvents()');
  await element('#refresh-events').listeners.click({});
  assert.equal(requests.filter(r=>r.options.method==='POST').length,1);
  assert.equal(element('#refresh-events').disabled,true);
  assert.equal(element('#refresh-events').textContent,'Searching…');
  run("state.jobs=[{id:'finished',kind:'events',status:'completed',created_at:200,ended_at:300,result:JSON.stringify({investor_events:true,found:2})}];activityDialog()");
  assert.match(element('#modal-body').innerHTML,/Massachusetts investor events/);
  assert.match(element('#modal-body').innerHTML,/2 publicly listed event matches/);
  assert.doesNotMatch(element('#modal-body').innerHTML,/undefined/);
  console.log('Functional UI checks passed: four columns, local time, unknown time, cached reads, safe links/escaping, busy/failure/empty/missing-key states, refresh request, and event Activity text.');
})().catch(e=>{console.error(e);process.exitCode=1;});
