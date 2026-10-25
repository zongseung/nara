import {collection, review, workbook, escape} from './views.js';

const $ = selector => document.querySelector(selector);
const sections = ['conditions','collect','review','export'];
let job=null, items=[], events=[], queue=[], current='conditions', itemIndex=0, candidateIndex=0, timer=null, busy=false, loadVersion=0;
const notice=message=>{ const target=$('#research-dialog').open?$('#research-error'):$('#notice');target.textContent=message;target.hidden=!message; };

async function api(path, body, options={}) {
  const response=await fetch(path, body===undefined?options:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),...options});
  const data=response.headers.get('content-type')?.includes('application/json')?await response.json():{detail:'서버 응답을 받지 못했습니다. 연결 상태를 확인하고 다시 시도하세요.'};
  if(!response.ok){if(data.detail?.job_id){await loadJob(data.detail.job_id);show('collect');}throw new Error(typeof data.detail==='string'?data.detail:data.detail?.message||'입력 조건과 서버 연결을 확인하세요.');}
  return data;
}

async function recent(){
  const jobs=await api('/jobs');
  $('#recent').innerHTML='<option value="">새 작업</option>'+jobs.map(j=>`<option value="${j.id}" ${j.id===job?.id?'selected':''}>${escape(j.request.company||'파일 업로드')} ${escape(j.request.keyword)} · ${escape(new Date(j.created_at).toLocaleString('ko-KR'))}</option>`).join('');
}

function render(){
  if(job&&current==='collect')$('#collect').innerHTML=collection(job,items,events,queue);
  if(job&&current==='review')$('#review').innerHTML=review(job,items,itemIndex,candidateIndex);
  if(job&&current==='export')$('#export').innerHTML=workbook(job,items);
  const ready=job&&['review','exported'].includes(job.status);
  document.querySelectorAll('[data-step]').forEach(button=>{
    const step=button.dataset.step;
    button.disabled=step==='conditions'?false:step==='collect'?!job:!ready;
    button.classList.toggle('done',sections.indexOf(step)<sections.indexOf(current));
    if(step===current)button.setAttribute('aria-current','step');else button.removeAttribute('aria-current');
  });
}

function show(step){
  current=sections.includes(step)?step:'conditions';
  for(const name of sections)$('#'+name).hidden=name!==current;
  if(job)history.replaceState(null,'',`#job=${job.id}&view=${current}`);
  render();
  window.scrollTo({top:0,behavior:'instant'});
  document.title=`${{conditions:'새 비교 작업',collect:'수집 진행',review:'경쟁사 검토',export:'가격비교표'}[current]} | nara`;
}

async function loadJob(id){
  clearTimeout(timer);
  const version=++loadVersion;
  const [next, nextItems, nextEvents]=await Promise.all([api(`/jobs/${id}`),api(`/jobs/${id}/items`),api(`/jobs/${id}/events`)]);
  const nextQueue=next.status==='queued'?await api('/jobs'):[];
  if(version!==loadVersion)return;
  job=next;items=nextItems;events=nextEvents;queue=nextQueue;
  itemIndex=Math.min(itemIndex,Math.max(0,items.length-1));
  candidateIndex=Math.min(candidateIndex,Math.max(0,(items[itemIndex]?.candidates.length||0)-1));
  render();
  if(['queued','running'].includes(job.status))timer=setTimeout(()=>loadJob(id).catch(error=>{notice(error.message);timer=setTimeout(()=>loadJob(id).catch(e=>notice(e.message)),5000);}),1500);
}

async function act(action){
  if(busy)return;
  busy=true;notice('');
  try{await action();}catch(error){notice(error.message);}finally{busy=false;$('#start').disabled=false;$('#start').textContent='수집 시작';}
}

function command(){
  const form=new FormData($('#job-form'));
  const exact=form.get('price')==='exact';
  $('#job-form').elements.band.disabled=!exact;
  $('#command').textContent=`uv run python compare_file.py \\\n  --company ${JSON.stringify(form.get('company'))} --keyword ${JSON.stringify(form.get('keyword'))} \\\n  --limit ${form.get('limit')} --top ${form.get('top')} --workers 1 \\\n  ${form.has('same_direction')?'--same-direction ':''}${exact?'--exact-price --fallback-band '+Number(form.get('band'))/100+' ':''}--out comparison.xlsx`;
}

$('#job-form').addEventListener('input',command);
$('#upload').addEventListener('change',()=>{const file=$('#upload').files[0];$('#upload-name').hidden=!file;$('#upload-name').textContent=file?`${file.name} · 파일의 업체와 희망가를 사용합니다.`:'';$('#job-form').elements.company.required=!file;});
$('#job-form').addEventListener('submit',event=>{event.preventDefault();act(async()=>{
  if(!$('#job-form').reportValidity())return;
  $('#start').disabled=true;
  $('#start').textContent='접수 중…';
  const data=new FormData($('#job-form')),file=$('#upload').files[0];
  const request={company:String(data.get('company')).trim(),keyword:String(data.get('keyword')).trim(),limit:Number(data.get('limit')),top:Number(data.get('top')),exact_price:data.get('price')==='exact',fallback_band:data.get('price')==='exact'?Number(data.get('band'))/100:0,same_direction:data.has('same_direction')};
  let created;
  if(file){
    if(file.size>8*1024*1024)throw new Error('입력 파일은 최대 8MB입니다.');
    const query=new URLSearchParams({limit:request.limit,top:request.top,exact_price:request.exact_price,fallback_band:request.fallback_band,same_direction:request.same_direction});
    created=await api(`/jobs/upload?${query}`,undefined,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});
  }else created=await api('/jobs',request);
  clearTimeout(timer);loadVersion++;
  job=created;items=[];events=[];queue=[];itemIndex=0;candidateIndex=0;
  show('collect');await loadJob(created.id);await recent();
});});

document.addEventListener('click',event=>{
  const step=event.target.closest('[data-step]');if(step&&!step.disabled){show(step.dataset.step);return;}
  const item=event.target.closest('[data-item]');if(item){itemIndex=Number(item.dataset.item);candidateIndex=0;render();return;}
  const candidate=event.target.closest('[data-candidate]');if(candidate&&!candidate.disabled){candidateIndex=Number(candidate.dataset.candidate);render();return;}
  const button=event.target.closest('[data-action]');if(!button||button.disabled)return;
  act(async()=>{
    const action=button.dataset.action;
    if(action==='open-job'){itemIndex=0;candidateIndex=0;await loadJob(button.dataset.job);show('collect');await recent();return;}
    if(action==='new'){show('conditions');return;}
    if(['review','collect','export'].includes(action)){show(action);return;}
    if(action==='supports'){$('#supports-form').reset();$('#supports-dialog').showModal();return;}
    if(action==='research'){
      const item=items[itemIndex],candidate=item.candidates[candidateIndex],form=$('#research-form');
      form.reset();$('#research-error').hidden=true;
      Object.assign(form.dataset,{job:job.id,index:item.index,revision:job.revision,target:candidate['식별번호']});
      $('#research-target').textContent=`${candidate['업체']} · ${candidate['모델']||candidate['식별번호']}`;
      $('#research-dialog').showModal();return;
    }
    button.disabled=true;
    if(action==='exclude'){
      const item=items[itemIndex],excluded=item.excluded.includes(candidateIndex)?item.excluded.filter(i=>i!==candidateIndex):[...item.excluded,candidateIndex];
      await api(`/jobs/${job.id}/items/${item.index}/selection`,{revision:job.revision,excluded});
    }else if(action==='workbook')await api(`/jobs/${job.id}/workbook`,{revision:job.revision});
    else if(action==='retry'||action==='cancel')await api(`/jobs/${job.id}/${action}`,{});
    await loadJob(job.id);
  }).finally(()=>{button.disabled=false;});
});

$('#close-supports').addEventListener('click',()=>$('#supports-dialog').close());
$('#close-research').addEventListener('click',()=>$('#research-dialog').close());
$('#research-form').addEventListener('submit',event=>{event.preventDefault();act(async()=>{
  const form=event.target,context=form.dataset,submit=form.querySelector('[type="submit"]');
  submit.disabled=true;
  try{
    const accepted=await api(`/jobs/${context.job}/items/${context.index}/research`,{
      revision:Number(context.revision),replace_item_id:context.target,
      conditions:{material:String(new FormData(form).get('material')).trim()}});
    $('#research-dialog').close();job=accepted;show('collect');await loadJob(accepted.id);
  }finally{submit.disabled=false;}
});});
$('#supports-form').addEventListener('submit',event=>{event.preventDefault();act(async()=>{
  const data=new FormData(event.target);
  await api(`/jobs/${job.id}/items/${items[itemIndex].index}/supports`,{revision:job.revision,count:Number(data.get('count')),note:String(data.get('note')).trim()});
  $('#supports-dialog').close();await loadJob(job.id);show('collect');
});});
$('#recent').addEventListener('change',event=>act(async()=>{if(event.target.value){itemIndex=0;candidateIndex=0;await loadJob(event.target.value);show(['review','exported'].includes(job.status)?'review':'collect');}else{clearTimeout(timer);loadVersion++;job=null;items=[];events=[];history.replaceState(null,'','#conditions');show('conditions');}}));
window.addEventListener('hashchange',()=>{if(location.hash==='#conditions')show('conditions');});
command();
act(async()=>{const params=new URLSearchParams(location.hash.slice(1));if(params.get('job')){await loadJob(params.get('job'));const desired=params.get('view');show(['review','exported'].includes(job.status)?desired||'review':'collect');}await recent();});
