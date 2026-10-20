// Actual Chrome checks against the fresh offline smoke server; no model calls.
import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {spawn} from 'node:child_process';
import {join} from 'node:path';
import assert from 'node:assert/strict';

const [metadataFile, transcript, output] = process.argv.slice(2);
if (!output) throw Error('Usage: node check_resume_browser.mjs server.json transcript-relative-path output-dir');
const metadata = JSON.parse(await readFile(metadataFile, 'utf8'));
const profile = join(output, 'chrome-profile');
await mkdir(profile, {recursive: true});
const chrome = spawn('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe', [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--remote-debugging-port=0', '--user-data-dir='+profile, 'about:blank'
], {windowsHide: true, stdio: ['ignore', 'ignore', 'pipe']});
let chromeError;
chrome.on('error', e => {chromeError = e;});
let diagnostics='';
chrome.stderr.on('data', chunk=>{diagnostics=(diagnostics+chunk).slice(-4000);});
let socket;
const pending = new Map();
const exceptions = [];
let serial = 0;
async function until(fn, timeout=15000) {
  const end = Date.now()+timeout;
  let last;
  while (Date.now()<end) {
    if (chromeError) throw chromeError;
    try {const value=await fn(); if(value)return value;} catch(e) {last=e;}
    await new Promise(r=>setTimeout(r,100));
  }
  throw Error('Browser check timed out: '+(last?.message||''));
}
try {
  const port = await until(async()=>Number((await readFile(join(profile,'DevToolsActivePort'),'utf8')).split('\n')[0]));
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  socket = new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
  socket.onmessage = event => {
    const value=JSON.parse(event.data);
    if(value.method==='Runtime.exceptionThrown')exceptions.push(value.params.exceptionDetails.text);
    if(value.id) {
      const request=pending.get(value.id);pending.delete(value.id);
      if(value.error)request.reject(Error(value.error.message));else request.resolve(value.result);
    }
  };
  socket.onclose = event => {
    for(const request of pending.values())request.reject(Error('Chrome connection closed '+event.code+' '+diagnostics));
    pending.clear();
  };
  function call(method,params={}) {
    const id=++serial;
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timed out: '+method+' '+diagnostics));},15000);
      pending.set(id,{resolve:value=>{clearTimeout(timer);resolve(value);},reject:error=>{clearTimeout(timer);reject(error);}});
      socket.send(JSON.stringify({id,method,params}));
    });
  }
  async function evaluate(expression) {
    const result=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
    if(result.exceptionDetails)throw Error(result.exceptionDetails.exception?.description||result.exceptionDetails.text);
    return result.result.value;
  }
  await call('Page.enable');await call('Runtime.enable');
  await call('Page.navigate',{url:metadata.dashboard});
  await until(()=>evaluate("document.readyState==='complete' && !!document.querySelector('#command-backend') && typeof info !== 'undefined'"));
  const defaults=await evaluate("({backend:$('#command-backend').value,imageVisible:!$('#command-image-label').classList.contains('hidden')})");
  assert.equal(defaults.backend,'container');assert.equal(defaults.imageVisible,true);
  await until(()=>evaluate("!!document.querySelector('#model-0') && modelCatalog.models.codex.length>0"));
  await evaluate("$('#model-0').value='gpt-6.1-sol';$('#cleanup-seconds').value='600';");
  await evaluate(`$('#count').value='1';$('#count').dispatchEvent(new Event('change'));
    $('#task').value='Offline browser plan only';$('#workspace').value=${JSON.stringify(metadata.workspace_roots[0])};
    $('#codex-budget').value='tokens';$('#codex-budget').dispatchEvent(new Event('change'));
    $('#model-0').value='gpt-6.1-sol';$('#plan-criteria').value='Keep chosen source';
    $('#checks').value='[["python","-V"]]';$('#plan').click();`);
  await until(()=>evaluate("$('#notice').textContent.includes('컨테이너 이미지')"));
  await evaluate("$('#command-backend').value='trusted_host';$('#command-backend').dispatchEvent(new Event('change'));$('#plan').click();");
  await until(()=>evaluate("info?.status==='awaiting_approval' && info.config.command_backend==='trusted_host'"));
  const hostPlan=await evaluate("({status:info.status,backend:info.config.command_backend,image:info.config.command_image,commands:info.config.validation_commands,warning:!!info.scope.host_command_warning})");
  assert.deepEqual(hostPlan.commands,[['python','-V']]);assert.equal(hostPlan.warning,true);assert.equal(hostPlan.image,null);
  assert.equal(await evaluate("info.config.agents[0].model"),'gpt-6.1-sol');
  assert.equal(await evaluate("info.config.cleanup_seconds"),600);
  assert.deepEqual(await evaluate("info.config.acceptance_criteria"),['Keep chosen source']);
  assert.equal(await evaluate("$('#role-0').readOnly"),true);
  await evaluate("$('#new').click();$('#demo').click();");
  await until(()=>evaluate("info?.status==='awaiting_approval' && events.some(e=>e.kind==='demo')"));
  await evaluate("$('#start').click()");
  await until(()=>evaluate("info?.status==='unverified'"));
  assert.equal(await evaluate("$('#state').textContent"),'검증 미완료');
  assert.equal(await evaluate("$('#completion-result').textContent.includes('검증 명령 없음')"),true);
  await until(()=>evaluate("!!info.preserved_implementation"));
  await evaluate("$('#resume-mode').value='dual_implementers';$('#resume-mode').dispatchEvent(new Event('change'));$('#resume-seconds').value='30';for(let i=0;i<2;i++)$('#resume-token-'+i).value='12000';$('#resume-0-model').value='opus';$('#resume-criteria').value='Keep resumed source';$('#resume-checks').value='[[\"python\",\"-V\"]]';$('#resume-backend').value='trusted_host';$('#resume-feedback-action').value='wait';$('#resume-plan').click();");
  await until(()=>evaluate("info?.status==='awaiting_approval' && info.config.agents[0].model==='opus'"));
  assert.equal(await evaluate("info.config.mode"),'dual_implementers');
  assert.equal(await evaluate("info.config.agents.length"),2);
  assert.equal(await evaluate("info.config.agents[1].role.includes('검토자')"),false);
  assert.deepEqual(await evaluate("info.config.acceptance_criteria"),['Keep resumed source']);
  assert.deepEqual(await evaluate("info.config.validation_commands"),[['python','-V']]);
  assert.equal(await evaluate("info.config.dual_feedback_action"),'wait');
  assert.equal(await evaluate("info.config.command_backend"),'trusted_host');
  assert.equal(await evaluate("info.source.path.endsWith('shared')"),true);
  await evaluate("$('#new').click();$('#command-backend').value='container';$('#command-backend').dispatchEvent(new Event('change'));$('#command-image').value='python:3.14-slim';$('#plan').click();");
  await until(()=>evaluate("info?.status==='awaiting_approval' && info.config.command_backend==='container'"));
  const containerPlan=await evaluate("({status:info.status,backend:info.config.command_backend,image:info.config.command_image})");
  assert.equal(containerPlan.image,'python:3.14-slim');
  const screenshot=await call('Page.captureScreenshot',{format:'png'});
  await writeFile(join(output,'dashboard.png'),Buffer.from(screenshot.data,'base64'));
  const url=new URL('/artifact',metadata.dashboard);
  url.searchParams.set('token',new URL(metadata.dashboard).searchParams.get('token'));
  url.searchParams.set('path',transcript);
  await call('Page.navigate',{url:url.href});
  await until(()=>evaluate("document.readyState==='complete' && document.querySelectorAll('#events article').length>0"));
  const options=await evaluate("[...$('#agent').options].map(o=>o.value)");
  assert.deepEqual(options,['','a','b']);
  await evaluate("$('#agent').value='a';$('#agent').dispatchEvent(new Event('input'));");
  const filter=await evaluate("({count:document.querySelectorAll('#events article').length,onlyA:[...document.querySelectorAll('#events article strong')].every(e=>e.textContent.includes(' a · '))})");
  assert.ok(filter.count>0&&filter.onlyA);
  await evaluate("$('#query').value='no-match-offline-browser-check';$('#query').dispatchEvent(new Event('input'));");
  assert.equal(await evaluate("document.querySelectorAll('#events article').length"),0);
  const blocked=await evaluate(`fetch(${JSON.stringify(new URL('/api/runs',metadata.dashboard).href)}).then(()=>false,()=>true)`);
  assert.equal(blocked,true);
  assert.deepEqual(exceptions,[]);
  const evidence={browser:'headless Chrome',defaults,missingImageRejected:true,hostPlan,containerPlan,
    transcript:{options,filter,searchWorks:true,apiFetchBlocked:true},uncaughtExceptions:exceptions};
  await writeFile(join(output,'browser-evidence.json'),JSON.stringify(evidence,null,2));
  console.log(JSON.stringify({browser:'passed',paidModelCalls:0,modelSelection:'passed',resumePlan:'passed',completionPolicy:'passed',transcriptFilter:'passed',csp:'passed'}));
} finally {
  socket?.close();
  chrome.kill();
  await new Promise(resolve=>{if(chrome.exitCode!==null)resolve();else chrome.once('exit',resolve);});
}
