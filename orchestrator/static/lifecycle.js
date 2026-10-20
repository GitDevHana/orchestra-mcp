// Explicit model IDs are forwarded unchanged. No hidden model substitution.
let modelCatalog={models:{}};
function modelFields(parent,prefix,agent={provider:'codex',model:null}){
  const box=document.createElement('div');box.className='row';
  const label=document.createElement('label');label.textContent='공급자';
  const provider=document.createElement('select');provider.id=prefix+'-provider';
  for(const name of ['codex','claude','cursor','gemini'])provider.add(new Option(name,name));
  provider.value=agent.provider;label.append(provider);
  const ml=document.createElement('label');ml.textContent='모델 선택 / ID 직접 입력';
  const input=document.createElement('input');input.id=prefix+'-model';input.placeholder='빈칸: CLI 기본 모델';input.value=agent.model||'';
  const list=document.createElement('datalist');list.id=prefix+'-models';input.setAttribute('list',list.id);ml.append(input,list);
  function options(){list.replaceChildren();for(const id of modelCatalog.models[provider.value]||[])list.append(new Option(id,id));}
  provider.addEventListener('change',()=>{input.value='';options()});options();box.append(label,ml);parent.append(box);
  return {provider,input,options};
}
function plannerModels(){for(let i=0;i<Number($('#count').value);i++){
  const old=$('#provider-'+i);if($('#model-'+i))continue;
  const fields=modelFields(old.parentElement.parentElement,'planner-'+i,{provider:old.value});
  fields.provider.removeAttribute('id');fields.provider.id='provider-'+i;
  fields.input.id='model-'+i;old.parentElement.remove();
  fields.provider.addEventListener('change',budgetFields);
}budgetFields();}
const slotsWithModels=slots;slots=function(){slotsWithModels();plannerModels()};$('#count').onchange=slots;
const cleanupLabel=document.createElement('label');cleanupLabel.textContent='시간 초과 후 인수인계 정리 한도(초 · 최대 600)';
cleanupLabel.innerHTML+='<input id="cleanup-seconds" type="number" min="0" max="600" value="600"><small>작업 종료 후 읽기 전용으로 정리합니다. 토큰·구독 한도에 도달하면 기록만 저장합니다.</small>';
$('#planner').insertBefore(cleanupLabel,$('#plan'));
const modelNote=document.createElement('p');modelNote.className='muted';modelNote.textContent='모델 후보는 이용 권한을 보장하지 않습니다. Codex는 로컬 목록, 나머지는 후보 또는 직접 입력입니다. Cursor 모델 ID는 해당 CLI 목록을 확인하세요.';$('#slots').after(modelNote);
const apiWithModels=api;api=async function(path,body){if(path==='/api/plan'){
  body.spec.cleanup_seconds=Number($('#cleanup-seconds').value);
  body.spec.agents.forEach((a,i)=>a.model=$('#model-'+i).value.trim()||null);
}return apiWithModels(path,body)};
const resumePanel=document.createElement('section');resumePanel.className='card hidden';resumePanel.id='resume-panel';
resumePanel.innerHTML='<h2>저장한 작업 이어하기</h2><p>현재 파일과 인수인계를 새 작업 폴더로 복사합니다. 완료한 작업을 확인하고 남은 작업을 이어갑니다. 새 계획은 시작 버튼으로 승인합니다.</p><label>추가 지시<textarea id="resume-feedback">남은 작업을 이어서 수행하세요.</textarea></label><label id="resume-candidate-label">이어갈 코드<select id="resume-candidate"><option value="a">A</option><option value="b">B</option></select></label><div id="resume-agents"></div><label>새 작업 시간(초)<input id="resume-seconds" type="number" min="1" max="86400" placeholder="새로 입력"></label><button id="resume-plan" class="primary">이어하기 계획 만들기</button>';
$('#current').insertBefore(resumePanel,$('#agent-panes'));
const resumeModeLabel=document.createElement('label');resumeModeLabel.textContent='이어서 진행할 작업 방식';
resumeModeLabel.innerHTML+='<select id="resume-mode"><option value="implement_review">옵션 1 · 하나의 구현 + 검토</option><option value="dual_implementers">옵션 2 · 독립 구현 A/B + 상호 피드백</option></select>';
const resumeCountLabel=document.createElement('label');resumeCountLabel.textContent='새 에이전트 수';
resumeCountLabel.innerHTML+='<select id="resume-count"><option value="1">1개</option><option value="2">2개</option></select>';
$('#resume-agents').before(resumeModeLabel,resumeCountLabel);
const resumeRoundsLabel=document.createElement('label');resumeRoundsLabel.textContent='새 최대 구현 라운드';
resumeRoundsLabel.innerHTML+='<input id="resume-rounds" type="number" min="1" max="30" value="3">';
$('#resume-plan').before(resumeRoundsLabel);
function resumeAgentFields(){
  const dual=$('#resume-mode').value==='dual_implementers';
  if(dual)$('#resume-count').value='2';
  $('#resume-count').disabled=dual;
  const count=Number($('#resume-count').value);
  const previous=[...$('#resume-agents').children].map((_,i)=>({provider:$('#resume-'+i+'-provider').value,model:$('#resume-'+i+'-model').value,max_tokens:$('#resume-token-'+i).value}));
  $('#resume-agents').replaceChildren();
  for(let i=0;i<count;i++){
    const agent=previous[i]||info.config.agents[Math.min(i,info.config.agents.length-1)];
    const box=document.createElement('div');box.append(document.createTextNode('Agent '+names[i].toUpperCase()));modelFields(box,'resume-'+i,agent);
    const label=document.createElement('label');label.textContent='새 최대 토큰 (Codex 구독 한도만 사용하려면 빈칸)';
    const input=document.createElement('input');input.id='resume-token-'+i;input.type='number';input.min=100;input.value=previous[i]?.max_tokens||'';
    label.append(input);box.append(label);$('#resume-agents').append(box);
  }
}
$('#resume-mode').onchange=resumeAgentFields;$('#resume-count').onchange=resumeAgentFields;
let resumeFormRun=null,modelStageRun=null;
const renderWithLifecycle=render;render=function(){renderWithLifecycle();
  const active=['running','cleaning_up','awaiting_quota_confirmation','awaiting_approval'].includes(info.status);
  resumePanel.classList.toggle('hidden',active);$('#stop').disabled=!active;
  if(info.status==='cleaning_up'){
    const left=Math.max(0,Math.ceil((info.cleanup?.deadline||0)-Date.now()/1000));
    $('#state').textContent='정리 중 · 남은 시간 '+left+'초';
    $('#next-stage-form').classList.add('hidden');
  }
  if(info.cleanup?.outcome)$('#reason').textContent+=' · 정리 결과: '+info.cleanup.outcome;
  if(resumeFormRun!==run){resumeFormRun=run;$('#resume-agents').replaceChildren();$('#resume-seconds').value='';
    $('#resume-mode').value=info.config.mode;$('#resume-count').value=String(info.config.agents.length);$('#resume-rounds').value=String(info.config.max_rounds);
    $('#resume-candidate-label').classList.toggle('hidden',info.config.mode!=='dual_implementers');
    resumeAgentFields();
  }
  if(info.comparison&&modelStageRun!==run){modelStageRun=run;document.querySelectorAll('.stage-model-choice').forEach(e=>e.remove());info.config.agents.forEach((a,i)=>{const box=document.createElement('div');box.className='stage-model-choice';modelFields(box,'stage-'+i,a);$('#stage-plan').before(box)})}
};
$('#resume-plan').onclick=()=>act(async()=>{
  const agents=Array.from({length:Number($('#resume-count').value)},(_,i)=>({provider:$('#resume-'+i+'-provider').value,model:$('#resume-'+i+'-model').value.trim()||null,max_tokens:$('#resume-token-'+i).value?Number($('#resume-token-'+i).value):null}));
  const planned=await api('/api/resume',{run,decision:{agents,mode:$('#resume-mode').value,max_rounds:Number($('#resume-rounds').value),max_seconds:Number($('#resume-seconds').value),feedback:$('#resume-feedback').value,candidate:info.config.mode==='dual_implementers'?$('#resume-candidate').value:null}});await choose(planned.id);
});
const apiWithStageModels=api;api=async function(path,body){if(path==='/api/next-stage')body.decision.agents=body.decision.agents.map((a,i)=>{
  const provider=$('#stage-'+i+'-provider').value;return {...a,provider,model:$('#stage-'+i+'-model').value.trim()||null,max_tokens:$('#stage-budget').value==='quota'&&provider==='codex'?null:Number($('#stage-token-'+names[i]).value)};
});return apiWithStageModels(path,body)};
$('#stage-plan').onclick=()=>act(async()=>{
  const decision={feedback:$('#stage-feedback').value,base_candidate:$('#stage-base').value,take_from_a:$('#stage-take-a').value,take_from_b:$('#stage-take-b').value,
    selected_files:JSON.parse($('#stage-files').value),mode:$('#stage-mode').value,agents:info.config.agents.map(()=>({max_tokens:100})),
    max_seconds:Number($('#stage-seconds').value),max_rounds:Number($('#stage-rounds').value),codex_reserve_percent:10,
    codex_five_hour_stop_at_used_percent:$('#stage-five').value?Number($('#stage-five').value):null,
    codex_weekly_stop_at_used_percent:$('#stage-week').value?Number($('#stage-week').value):null};
  if(!decision.feedback.trim())throw Error('다음 구현 방향을 입력하세요.');
  const planned=await api('/api/next-stage',{run,decision});await choose(planned.id);
});
act(async()=>{modelCatalog=await api('/api/models');plannerModels();});
act(async()=>{
  const policy=await api('/api/doctor');
  const note=document.createElement('small');
  note.id='workspace-policy';
  note.textContent=policy.allow_any_workspace?'직접 지정한 기존 폴더를 사용할 수 있습니다.':'사용 가능한 폴더: '+policy.workspace_roots.join(' · ');
  $('#workspace').after(note);
});

// Completion policy is visible before starting, including on continuations.
function completionFields(prefix,before,commands=false){
  const box=document.createElement('fieldset');
  const legend=document.createElement('legend');legend.textContent='완료 기준과 피드백 반영';box.append(legend);
  const criteria=document.createElement('label');criteria.textContent='완료 기준 · 한 줄에 하나';
  const input=document.createElement('textarea');input.id=prefix+'-criteria';
  input.placeholder='예: 계정 전환 뒤 이전 요청의 응답이 표시되지 않는다';criteria.append(input);box.append(criteria);
  const action=document.createElement('label');action.textContent='옵션 2 상호 피드백 이후';
  const select=document.createElement('select');select.id=prefix+'-feedback-action';
  select.add(new Option('남은 라운드에서 차단 결함 수정 후 재검토','revise'));
  select.add(new Option('피드백 후 사용자 선택 대기','wait'));action.append(select);box.append(action);
  const note=document.createElement('p');note.className='muted';
  note.textContent='시간은 최대 한도입니다. 옵션 2의 한 라운드는 구현·검증·상호 피드백을 포함합니다. 검증 명령이 없거나 확인 근거가 부족하면 검증 미완료로 표시합니다. 최종 A/B 선택과 병합은 자동으로 하지 않습니다.';
  box.append(note);
  if(commands){
    const label=document.createElement('label');label.textContent='이번 단계 검증 명령 · argv 배열';
    const checks=document.createElement('textarea');checks.id=prefix+'-checks';label.append(checks);box.append(label);
    const backendLabel=document.createElement('label');backendLabel.textContent='검증 명령 실행 위치';
    const backend=document.createElement('select');backend.id=prefix+'-backend';
    backend.add(new Option('Docker 컨테이너','container'));backend.add(new Option('이 PC에서 실행','trusted_host'));
    backendLabel.append(backend);box.append(backendLabel);
    const imageLabel=document.createElement('label');imageLabel.textContent='컨테이너 이미지 (Docker 검증 시)';
    const image=document.createElement('input');image.id=prefix+'-image';imageLabel.append(image);box.append(imageLabel);
    backend.onchange=()=>imageLabel.classList.toggle('hidden',backend.value!=='container');
  }
  before.before(box);
}
completionFields('plan',$('#plan'));
completionFields('resume',$('#resume-plan'),true);
completionFields('stage',$('#stage-plan'),true);
function completionValues(prefix,commands=false){
  const values={acceptance_criteria:$('#'+prefix+'-criteria').value.split('\n').map(s=>s.trim()).filter(Boolean),
    dual_feedback_action:$('#'+prefix+'-feedback-action').value};
  if(commands){
    values.validation_commands=JSON.parse($('#'+prefix+'-checks').value);
    values.command_backend=$('#'+prefix+'-backend').value;
    values.command_image=values.command_backend==='container'?($('#'+prefix+'-image').value.trim()||null):null;
    if(values.validation_commands.length&&values.command_backend==='container'&&!values.command_image)
      throw Error('검증 명령에 사용할 컨테이너 이미지를 입력하세요.');
  }
  return values;
}
const apiWithCompletion=api;api=async function(path,body){
  if(path==='/api/plan')Object.assign(body.spec,completionValues('plan'));
  if(path==='/api/resume')Object.assign(body.decision,completionValues('resume',true));
  if(path==='/api/next-stage')Object.assign(body.decision,completionValues('stage',true));
  return apiWithCompletion(path,body);
};
const completionPanel=document.createElement('div');completionPanel.id='completion-result';
$('#reason').after(completionPanel);
const completionLabels={completed:'검증 통과',unverified:'검증 미완료',needs_fix:'수정 필요',unverifiable:'검증 불가'};
let completionFormRun=null;
const renderWithCompletion=render;render=function(){
  renderWithCompletion();
  if(!info)return;
  if(info.status==='unverified')$('#state').textContent='검증 미완료';
  completionPanel.replaceChildren();
  if(info.completion){const p=document.createElement('p');p.textContent=info.completion.reason;completionPanel.append(p)}
  for(const [key,candidate] of Object.entries(info.comparison?.candidates||{})){
    const p=document.createElement('p'),status=candidate.completion?.status;
    p.textContent=key.toUpperCase()+' · '+(completionLabels[status]||'완료 판정 대기')+
      (candidate.completion?.reason?' · '+candidate.completion.reason:'');completionPanel.append(p);
  }
  const source=document.createElement('p');source.className='muted';
  source.textContent='기준 코드: '+(info.source?.path||info.config.workspace)+
    (info.source?.candidate?' · 선택 '+info.source.candidate.toUpperCase():'')+' · 작업 폴더: '+info.config.workspace;
  completionPanel.append(source);
  if(!info.config.validation_commands.length){
    const p=document.createElement('p');p.textContent='등록된 검증 명령 없음 · 에이전트 자체 보고만으로 완료 처리하지 않습니다.';completionPanel.append(p);
  }
  if(completionFormRun!==run){
    completionFormRun=run;
    for(const prefix of ['resume','stage']){
      $('#'+prefix+'-criteria').value=(info.config.acceptance_criteria||[]).join('\n');
      $('#'+prefix+'-feedback-action').value=info.config.dual_feedback_action||'revise';
      $('#'+prefix+'-checks').value=JSON.stringify(info.config.validation_commands,null,2);
      $('#'+prefix+'-backend').value=info.config.command_backend||'container';
      $('#'+prefix+'-image').value=info.config.command_image||'';
      $('#'+prefix+'-backend').dispatchEvent(new Event('change'));
    }
  }
};
// Roles follow the selected mode. Special instructions belong in the task.
const slotsWithCompletion=slots;slots=function(){
  slotsWithCompletion();
  for(let i=0;i<Number($('#count').value);i++){
    const role=$('#role-'+i);role.readOnly=true;
    role.value=$('#mode').value==='dual_implementers'?'독립 구현과 검증, 전달된 피드백 반영':
      i===1?'읽기 전용 검토 · 차단 결함은 구현자에게 반환':'구현과 검증 · 차단 결함 수정';
  }
};$('#count').onchange=slots;
slots();
