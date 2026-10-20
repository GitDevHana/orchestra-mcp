// Real Chromium CDP smoke test without an extra browser automation dependency.
import { spawn } from 'node:child_process';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';

const root = join(import.meta.dirname, '..');
let data;
for(let i=0;i<100;i++){
  try{data=JSON.parse(await readFile(join(root,'demo-data','records','server.json'),'utf8'));break}catch{}
  await new Promise(r=>setTimeout(r,100));
}
if(!data)throw Error('Dashboard server metadata did not appear');
const profile = join(root, 'demo-data', 'browser-' + Date.now());
await mkdir(profile, {recursive:true});
const chrome = spawn('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0', '--user-data-dir='+profile, 'about:blank'],
  {windowsHide:true,stdio:'ignore'});
let socket,serial=0;const pending=new Map(),errors=[];
function send(method,params={},sessionId){return new Promise((resolve,reject)=>{
  const id=++serial,timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timeout: '+method))},15000);
  pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));
})}
try {
  let port;
  for(let i=0;i<200;i++){try{port=(await readFile(join(profile,'DevToolsActivePort'),'utf8')).trim().split(/\r?\n/);break}catch{}await new Promise(r=>setTimeout(r,100))}
  if(!port)throw Error('Chrome did not expose its local debugging endpoint');
  socket=new WebSocket('ws://127.0.0.1:'+port[0]+port[1]);
  await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true})});
  socket.addEventListener('message',message=>{
    const value=JSON.parse(message.data);if(value.method==='Runtime.exceptionThrown')errors.push(value.params.exceptionDetails);
    if(value.id){const item=pending.get(value.id);if(item){clearTimeout(item.timer);pending.delete(value.id);value.error?item.reject(Error(JSON.stringify(value.error))):item.resolve(value.result)}}
  });
  const target=await send('Target.createTarget',{url:'about:blank'});
  const {sessionId}=await send('Target.attachToTarget',{targetId:target.targetId,flatten:true});
  await send('Runtime.enable',{},sessionId);await send('Page.enable',{},sessionId);
  await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1080,deviceScaleFactor:1,mobile:false},sessionId);
  const evalJS=async expression=>{const result=await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true,userGesture:true},sessionId);if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value};
  await send('Page.navigate',{url:data.dashboard},sessionId);
  for(let i=0;i<100;i++){if(await evalJS("typeof api==='function' && !!document.querySelector('#demo')"))break;await new Promise(r=>setTimeout(r,100))}
  await evalJS("document.querySelector('#demo').click()");
  for(let i=0;i<100;i++){if(await evalJS("typeof info!=='undefined' && info?.status==='awaiting_approval'"))break;await new Promise(r=>setTimeout(r,100))}
  if(!await evalJS("info?.status==='awaiting_approval'"))throw Error('Demo plan not displayed');
  await evalJS("document.querySelector('#start').click()");
  for(let i=0;i<100;i++){if(await evalJS("info?.status==='completed'"))break;await new Promise(r=>setTimeout(r,150))}
  const facts=await evalJS("({status:info?.status,panes:document.querySelectorAll('#agent-panes section').length,approvals:document.querySelectorAll('.approval').length,overflow:document.documentElement.scrollWidth>innerWidth,run:info.id})");
  if(facts.status!=='completed'||facts.panes!==2||facts.approvals<1||facts.overflow)throw Error(JSON.stringify(facts));
  await evalJS("document.querySelector('#approval-list input').checked=true; document.querySelector('#approve').click()");
  for(let i=0;i<100;i++){if(await evalJS("info.approvals.some(a=>a.state==='approved')"))break;await new Promise(r=>setTimeout(r,100))}
  if(!await evalJS("info.approvals.some(a=>a.state==='approved')"))throw Error('Approval decision did not persist');
  await evalJS("document.querySelector('#agent-panes button').click()");
  let targets;
  for(let i=0;i<50;i++){
    targets=await send('Target.getTargets');
    if(targets.targetInfos.some(t=>t.url.includes('agent=a')))break;
    await new Promise(r=>setTimeout(r,100));
  }
  if(!targets.targetInfos.some(t=>t.url.includes('agent=a')))throw Error('Agent popout did not open: '+JSON.stringify(targets));
  await evalJS("document.querySelector('#export').click()");
  await new Promise(r=>setTimeout(r,500));
  const image=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false},sessionId);
  await writeFile(join(root,'demo-data','dashboard.png'),Buffer.from(image.data,'base64'));
  await evalJS("document.querySelector('#new').click(); document.querySelector('#mode').value='dual_implementers'; document.querySelector('#mode').dispatchEvent(new Event('change')); document.querySelector('#demo').click()");
  for(let i=0;i<100;i++){if(await evalJS("info?.status==='awaiting_approval'&&info.config.mode==='dual_implementers'"))break;await new Promise(r=>setTimeout(r,100))}
  await evalJS("document.querySelector('#start').click()");
  for(let i=0;i<100;i++){if(await evalJS("info?.status==='awaiting_user_review'"))break;await new Promise(r=>setTimeout(r,100))}
  const dual=await evalJS("({status:info?.status,run:info?.id,feedback_complete:info?.comparison?.feedback_complete,fresh:!document.querySelector('#stage-token-a').value&&!document.querySelector('#stage-token-b').value&&!document.querySelector('#stage-seconds').value,overflow:document.documentElement.scrollWidth>innerWidth})");
  if(dual.status!=='awaiting_user_review'||!dual.feedback_complete||!dual.fresh||dual.overflow)throw Error(JSON.stringify(dual));
  // Drafts survive refresh while the user considers a decision.
  await evalJS("document.querySelector('#stage-feedback').value='A 구조를 기준으로 B의 오류 처리 반영'; document.querySelector('#stage-take-b').value='오류 처리'; document.querySelector('#stage-token-a').value='12000'; document.querySelector('#stage-token-b').value='15000'; document.querySelector('#stage-seconds').value='90'");
  await evalJS("poll()");
  if(!await evalJS("document.querySelector('#stage-feedback').value.includes('A 구조')&&document.querySelector('#stage-token-a').value==='12000'"))throw Error('Stage decision draft was lost');
  const dualImage=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false},sessionId);
  await writeFile(join(root,'demo-data','dual-dashboard.png'),Buffer.from(dualImage.data,'base64'));
  await evalJS("document.querySelector('#stage-plan').click()");
  for(let i=0;i<100;i++){if(await evalJS("info?.status==='awaiting_approval'&&!!info.parent_id"))break;await new Promise(r=>setTimeout(r,100))}
  const nextStage=await evalJS("({status:info?.status,parent:info?.parent_id,max_seconds:info?.config.max_seconds,tokens:info?.config.agents.map(a=>a.max_tokens),empty_usage:Object.keys(info?.usage||{}).length===0})");
  if(nextStage.status!=='awaiting_approval'||nextStage.parent!==dual.run||nextStage.max_seconds!==90||JSON.stringify(nextStage.tokens)!=='[12000,15000]'||!nextStage.empty_usage)throw Error(JSON.stringify(nextStage));
  if(errors.length)throw Error(JSON.stringify(errors));
  const result={...facts,approval_decision:true,popout:true,dual,nextStage,uncaught_errors:errors.length};
  await writeFile(join(root,'demo-data','browser-smoke.json'),JSON.stringify(result,null,2));
  console.log(JSON.stringify(result));
} finally {
  if(socket?.readyState===1){try{await send('Browser.close')}catch{}socket.close()}
  if(chrome.exitCode===null)chrome.kill();
}
