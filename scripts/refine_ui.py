from pathlib import Path

page = Path(__file__).resolve().parent.parent / "orchestrator" / "static" / "dashboard.html"
text = page.read_text(encoding="utf-8")
anchor = "act(async()=>{const d=await api('/api/doctor');"
enhancement = r'''
// Keep draft text and selection while events refresh the panels.
const drafts={},approvalCommands={};const renderOriginal=render;
render=function(){
  const active=document.activeElement,key=active?.dataset?.draft,start=active?.selectionStart,end=active?.selectionEnd;
  renderOriginal();
  for(const panel of document.querySelectorAll('#agent-panes section')){
    const a=panel.querySelector('h2').textContent.includes('Agent A')?'a':'b',input=panel.querySelector('textarea');
    input.dataset.draft='message-'+a;input.value=drafts[input.dataset.draft]||'';
    input.oninput=()=>{drafts[input.dataset.draft]=input.value};
    const send=panel.querySelector('button:last-child'),old=send.onclick;
    send.onclick=async()=>{await old();if(!input.value)drafts[input.dataset.draft]=''};
  }
  for(const item of info.approvals){
    const check=[...document.querySelectorAll('#approval-list input')].find(n=>n.value===item.id);
    if(!check)continue;
    const label=document.createElement('label');label.textContent='검토 후 승인할 명시 실행 명령 (JSON argv, 선택 사항)';
    const input=document.createElement('textarea');input.dataset.draft='command-'+item.id;
    input.placeholder='["실행파일", "인수"]';input.disabled=item.state!=='pending';
    input.value=approvalCommands[item.id]??(item.argv?JSON.stringify(item.argv):'');
    input.oninput=()=>{approvalCommands[item.id]=input.value};label.append(input);check.closest('.approval').append(label);
    check.checked=drafts['selected-'+item.id]||false;check.onchange=()=>{drafts['selected-'+item.id]=check.checked};
  }
  if(key){const next=document.querySelector('[data-draft="'+CSS.escape(key)+'"]');if(next){next.focus();next.setSelectionRange(start,end)}}
  const diagram=events.filter(e=>e.kind==='archify_export').at(-1)?.payload;
  if(diagram&&diagram.status==='passed'&&!$('#files').querySelector('[data-diagrams]')){
    const links=document.createElement('p');links.dataset.diagrams='1';
    for(const [i,receipt] of (diagram.receipts||[]).entries()){
      const relative=receipt.path.replaceAll('\\','/').split('/.archify/')[1];if(!relative)continue;
      const a=document.createElement('a');a.textContent='Archify '+(i+1);a.href='/artifact?token='+encodeURIComponent(token)+'&path='+encodeURIComponent('.archify/'+relative);a.target='_blank';a.rel='noopener';a.style.marginRight='12px';links.append(a);
    }$('#files').append(links);
  }
};
for(const [id,approve]of [['approve',true],['reject',false]])$('#'+id).onclick=()=>act(async()=>{
  const ids=[...document.querySelectorAll('#approval-list input:checked')].map(e=>e.value),command_overrides={};
  if(approve)for(const id of ids){const input=document.querySelector('[data-draft="command-'+id+'"]');if(input?.value.trim())command_overrides[id]=JSON.parse(input.value)}
  await api('/api/decide',{run,ids,approve,command_overrides});for(const id of ids)delete drafts['selected-'+id];await poll();
});
'''
if "const drafts={},approvalCommands={}" not in text:
    if anchor not in text:
        raise SystemExit("Dashboard anchor changed")
    page.write_text(text.replace(anchor, enhancement + "\n" + anchor), encoding="utf-8")
