const el = (h) => { const t=document.createElement('template'); t.innerHTML=h.trim(); return t.content.firstChild; };
  const esc = (s) => String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

  // ---- field kinds ----
  const KINDS = [
    {k:'text', r:'Free text the user types (the default).', demo:'<span class="inp">Type the value\u2026</span>'},
    {k:'app_picker', r:'Selectable \u2014 pick an application from the catalogue.', demo:'<span class="chip sel">Formulation Planner</span><span class="chip sel">Smart Hub <span class="go">\u203a</span></span>'},
    {k:'role_picker', r:'Selectable \u2014 pick an existing role (used in the privilege flow, not role creation).', demo:'<span class="chip sel">Super Admin</span><span class="chip sel">Hub Viewer <span class="go">\u203a</span></span>'},
    {k:'menu_picker', r:'Selectable \u2014 pick a menu, or \u201ccreate a new menu first\u201d.', demo:'<span class="chip sel">Budget menu <span class="go">\u203a</span></span><span class="chip sel">Create a new menu first</span>'},
    {k:'yesno', r:'Boolean \u2014 two decision buttons (not a picker).', demo:'<span class="yn"><span class="y">\u2713 Yes</span><span class="n">\u2715 No</span></span>'},
  ];
  const kw=document.getElementById('kinds');
  KINDS.forEach(x=>kw.appendChild(el(`<div class="card"><div class="k">${x.k}</div><div class="r">${x.r}</div><div class="demo">${x.demo}</div></div>`)));

  // ---- modifiers ----
  const MODS=[
    ['kind','str','Control to render: <code>text</code>, <code>app_picker</code>, <code>role_picker</code>, <code>menu_picker</code>, <code>yesno</code>. Defaults to <code>text</code>.'],
    ['optional','bool','The step can be skipped; the field is left empty.'],
    ['maxlen','int','Reject free text longer than this \u2014 mirrors the DB column width.'],
    ['is_code','bool','Normalize the entered value to <span class="c">UPPER_SNAKE</span> before storing.'],
    ['suggest_from','str','Propose <span class="c">UPPER(other field)</span> as a default the user can accept or override.'],
    ['auto_from','str','Fully auto-generate a code from another field \u2014 <b>never prompted</b>, computed and de-duped as the flow advances.'],
    ['unique','str','Uniqueness rule checked live: <code>app_code</code>, <code>role_name</code>, <code>role_code</code>, <code>menu_name</code>, <code>menu_code</code>. Drives the amber reference state.'],
  ];
  const mb=document.getElementById('mods');
  MODS.forEach(([a,b,c])=>mb.appendChild(el(`<tr><td><code>${a}</code></td><td>${b}</td><td>${c}</td></tr>`)));

  // ---- lifecycle ----
  const LIFE=[
    {s:'detect',h:'Intent',p:'A create verb + entity noun (_EC_VERB / _EC_ENTITY) classifies the request \u2014 or asks which.'},
    {s:'start',h:'Start',p:'start_entity_create loads the spec, floors the panel to Cozy, shows the rail.'},
    {s:'step \u00d7N',h:'Fields',p:'_efield_step renders each EField in order \u2014 picker, yes/no, or typed \u2014 validating length + uniqueness.'},
    {s:'confirm',h:'Confirm',p:'_entity_finalize shows every captured value and waits for an explicit Create.'},
    {s:'run',h:'Execute',p:'The tool is called once with fields + defaults. The only write in the whole flow.'},
    {s:'done',h:'Complete',p:'Collapses to a green \u2713 card with \u201cwhat next\u201d chips.',cls:'done'},
    {s:'or cancel',h:'Cancelled',p:'Cancel folds the steps into a rose \u2715 card. Nothing saved.',cls:'cancel'},
  ];
  const lf=document.getElementById('life');
  LIFE.forEach(x=>lf.appendChild(el(`<div class="step ${x.cls||''}"><div class="s">${x.s}</div><h4>${x.h}</h4><p>${x.p}</p></div>`)));

  // ---- walkthrough ----
  const WF_STEPS=[
    {label:'Application', q:'Which <b>application</b> is this role for?', ctl:'<span class="chip sel">Formulation Planner</span><span class="chip sel">Revenue Planner <span class="go">\u203a</span></span>', val:'Formulation Planner'},
    {label:'Role name', q:"What's the role's <b>display name</b>? <i>Pick a distinct name.</i>", ctl:'<span class="chip hint amber" style="font-family:var(--font-mono)">\ud83d\udee1 Roles 16 \u25be</span> <span style="font-size:11.5px;color:var(--amber)">existing names shown for reference</span>', val:'Budget Reviewer'},
    {label:'Role code', q:'(auto-generated from the name \u2014 never asked)', ctl:'<span class="inp mono">BUDGET_REVIEWER \u00b7 auto</span>', val:'BUDGET_REVIEWER \u00b7 auto', mono:true, auto:true},
    {label:'Description', q:'A one-line <b>description</b> of the role?', ctl:'<span class="inp">Type the description\u2026</span>', val:'Approves budget requests'},
    {label:'Admin role', q:'Is this an <b>admin</b> role?', ctl:'<span class="yn"><span class="y">\u2713 Yes</span><span class="n">\u2715 No</span></span>', val:'No'},
    {label:'Menu', q:'Which <b>menu</b> should be mapped to this role?', ctl:'<span class="chip sel">Budget menu <span class="go">\u203a</span></span><span class="chip sel">Create a new menu first</span>', val:'Budget menu'},
    {label:'Review & confirm', q:'Everything captured \u2014 create the role?', ctl:'<span class="yn"><span class="y">\u2713 Create</span><span class="n" style="color:var(--rose)">Cancel</span></span>', val:''},
  ];
  let wi=0, timer=null;
  const wrail=document.getElementById('w-rail'), wq=document.getElementById('w-q'),
        wctl=document.getElementById('w-control'), wcnt=document.getElementById('w-cnt'), wnow=document.getElementById('w-now');
  function drawWalk(){
    wrail.innerHTML='';
    WF_STEPS.forEach((s,i)=>{
      const state = i<wi ? 'done' : (i===wi ? 'cur' : '');
      const showVal = i<wi && s.val;
      wrail.appendChild(el(`<div class="row ${state}">
        <span class="b">${i<wi?'\u2713':''}</span><span>${s.label}</span>
        ${showVal?`<span class="v ${s.mono?'mono':''}">${esc(s.val)}</span>`:''}</div>`));
    });
    const s=WF_STEPS[wi];
    wq.innerHTML=s.q; wctl.innerHTML=s.ctl; wcnt.textContent=`${wi+1} / ${WF_STEPS.length}`;
    wnow.textContent = s.auto ? 'AUTO' : 'NOW';
    document.getElementById('w-prev').disabled = wi===0;
    document.getElementById('w-next').disabled = wi===WF_STEPS.length-1;
  }
  function step(d){ wi=Math.max(0,Math.min(WF_STEPS.length-1,wi+d)); drawWalk(); }
  function play(){
    const btn=document.getElementById('w-play');
    if(timer){ clearInterval(timer); timer=null; btn.textContent='\u25b6 Play'; return; }
    if(wi===WF_STEPS.length-1) wi=0;
    btn.textContent='\u275a\u275a Pause';
    timer=setInterval(()=>{ if(wi>=WF_STEPS.length-1){clearInterval(timer);timer=null;btn.textContent='\u25b6 Play';return;} step(1); },2200);
    drawWalk();
  }
  document.getElementById('w-prev').addEventListener('click',()=>step(-1));
  document.getElementById('w-next').addEventListener('click',()=>step(1));
  document.getElementById('w-play').addEventListener('click',play);
  drawWalk();

  // ---- icons (real widget SVGs) ----
  const SVG_ROLE='<svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.5 6c-2.61.7-5.67 1-8.5 1s-5.89-.3-8.5-1L3 8c1.86.5 4 .83 6 1v13h2v-6h2v6h2V9c2-.17 4.14-.5 6-1l-.5-2zM12 6c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2z"/></svg>';
  const SVG_MENU='<svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 6h18v2H3zM3 11h18v2H3zM3 16h12v2H3z"/></svg>';
  const SVG_APP='<svg viewBox="0 0 24 24" fill="currentColor"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/></svg>';
  const glyph=(g)=>`<svg viewBox="0 0 24 24" fill="currentColor"><text x="12" y="17" text-anchor="middle" font-size="15" font-family="monospace">${g}</text></svg>`;
  const ENT=[['role','g-v',SVG_ROLE,'app_picker / role'],['menu','g-v',SVG_MENU,'menu_picker'],['application','g-v',SVG_APP,'default / gear']];
  const STATE=[
    ['completed','g-g',glyph('\u2713'),'green \u2713 \u2014 done'],
    ['cancelled','g-r',glyph('\u2715'),'rose \u2715 \u2014 cancelled'],
    ['current','g-v','<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="6"/></svg>','violet \u25cf \u2014 active step'],
    ['admin','g-a',glyph('\ud83d\udee1'),'admin role marker'],
    ['reference','g-a',glyph('\u25be'),'amber \u2014 taken / reference'],
  ];
  const ie=document.getElementById('ico-entity'), is=document.getElementById('ico-state');
  ENT.forEach(([n,g,svg,d])=>ie.appendChild(el(`<div class="ico"><div class="glyph ${g}">${svg}</div><div class="nm">${n}</div><div class="d">${d}</div></div>`)));
  STATE.forEach(([n,g,svg,d])=>is.appendChild(el(`<div class="ico"><div class="glyph ${g}">${svg}</div><div class="nm">${n}</div><div class="d">${d}</div></div>`)));

  // ---- design tokens ----
  const COLORS=[
    ['--violet','Brand \u00b7 selectable','pickers, links, active step','#7c3aed'],
    ['--amber','Reference / taken','existing unique values','#b45309'],
    ['--green','Completed','done card, \u2713, admin','#16a34a'],
    ['--rose','Cancelled','cancel card, \u2715, Cancel','#dc2626'],
    ['--ink','Text','prose & values','#1c1530'],
    ['--muted','Muted','labels, captions','#5f5878'],
  ];
  const tc=document.getElementById('tok-colors');
  COLORS.forEach(([v,lab,use,hex])=>tc.appendChild(el(
    `<div class="swrow"><span class="chipc" style="background:var(${v})"></span><div style="min-width:0"><div class="lab">${lab}</div><div class="use">${use}</div></div><div class="hex">${hex}<br><span style="opacity:.7">${v}</span></div></div>`)));
  const FONTS=[
    ['UI chrome','Poppins','headers \u00b7 labels \u00b7 chips \u00b7 buttons \u2014 400 / 500 / 600 / 700','font-family:Poppins,sans-serif'],
    ['Prose','Inter','agent answers \u00b7 descriptions \u00b7 body \u2014 400 / 500 / 600','font-family:Inter,sans-serif'],
    ['Code / IDs','JetBrains Mono','codes \u00b7 IDs \u00b7 numbers \u2014 400 / 500 / 600','font-family:var(--font-mono)'],
  ];
  const tf=document.getElementById('tok-fonts');
  FONTS.forEach(([role,name,wts,st])=>tf.appendChild(el(
    `<div class="f"><div class="role">${role}</div><div class="name" style="${st}">${name}</div><div class="wts">${wts}</div></div>`)));
  const TYPE=[
    ['Display','18 / 700','Good morning, GC'],['Title','15.5 / 600','Create application role'],
    ['Body','15 / 400','Prose reads in Inter.'],['Label','12 / 600','WHAT I CAN HELP WITH'],
    ['Caption','11 / 500','Awaiting your answer'],['Mono','13 / 500','SMART_HUB'],
  ];
  const tt=document.getElementById('tok-type');
  TYPE.forEach(([r,s,sample])=>{
    const mono=r==='Mono'?'font-family:var(--font-mono)':'';
    tt.appendChild(el(`<div class="typ"><div class="spec">${r} \u00b7 ${s}</div><div style="${mono}">${esc(sample)}</div></div>`));
  });

  // ---- worked example ----
  const EXAMPLE=[
    ['applicationId','app_picker','<span class="badge v">selectable</span>','application chips'],
    ['roleName','text \u00b7 unique','<span class="badge a">reference</span>','typed; existing names amber'],
    ['role','auto_from','<span class="badge m">auto \u00b7 mono</span>','BUDGET_REVIEWER \u00b7 auto'],
    ['roleDescription','text','\u2014','typed (Inter)'],
    ['isAdmin','yesno','<span class="badge" style="color:var(--green-ink);background:var(--green-soft)">boolean</span>','\u2713 Yes / \u2715 No'],
    ['menuId','menu_picker','<span class="badge v">selectable</span>','menu chips'],
  ];
  const exb=document.getElementById('ex-rows');
  EXAMPLE.forEach(r=>exb.appendChild(el(`<tr><td><code>${r[0]}</code></td><td>${r[1]}</td><td>${r[2]}</td><td class="mono" style="font-size:12px;color:var(--violet-ink)">${r[3]}</td></tr>`)));

  // ---- checklist ----
  const CHECK=[
    "Read the backend tool's schema \u2014 note every NOT-NULL and unique column, and the exact field keys.",
    'Add an entry to <code>_ENTITY_CREATE</code>: <code>label</code>, <code>tool</code>, ordered <code>fields</code>, and <code>defaults</code>.',
    'Pick a <code>kind</code> per field; add <code>maxlen</code> / <code>unique</code> / <code>is_code</code> to match the column; use <code>auto_from</code> for derived codes.',
    'Register the entity noun in <code>_EC_ENTITY</code> so <code>_detect_entity</code> routes the intent.',
    'Confirm <code>_entity_finalize</code> maps collected values + defaults to the tool\'s arguments.',
    'Dry-run in the widget: step through, check the rail, <b>cancel once</b> (rose card), then <b>complete once</b> (green card) against a test record.',
  ];
  const cl=document.getElementById('checklist-list');
  CHECK.forEach(c=>cl.appendChild(el(`<li>${c}</li>`)));

  // ---- copy ----
  function wireCopy(btn,getText){
    btn.addEventListener('click',async()=>{
      const text=getText();
      try{ await navigator.clipboard.writeText(text); }
      catch(e){ const s=document.createElement('textarea'); s.value=text; document.body.appendChild(s); s.select();
        try{document.execCommand('copy');}catch(_){} s.remove(); }
      const o=btn.textContent; btn.textContent='Copied'; btn.classList.add('ok');
      setTimeout(()=>{btn.textContent=o;btn.classList.remove('ok');},1300);
    });
  }
  document.querySelectorAll('.copy[data-copy]').forEach(b=>wireCopy(b,()=>document.getElementById(b.dataset.copy).innerText));

  // ---- builder ----
  const KIND_OPTS=['text','app_picker','role_picker','menu_picker','yesno'];
  let fields=[
    {key:'teamName',prompt:"What's the **team name**?",kind:'text',optional:false,is_code:false,maxlen:'120',unique:'',suggest_from:'',auto_from:''},
    {key:'teamCode',prompt:'A short **team code**?',kind:'text',optional:false,is_code:true,maxlen:'30',unique:'',suggest_from:'teamName',auto_from:''},
    {key:'isActive',prompt:'Is the team **active**?',kind:'yesno',optional:false,is_code:false,maxlen:'',unique:'',suggest_from:'',auto_from:''},
  ];
  const TG=[['optional','optional'],['is_code','is_code']];
  const host=document.getElementById('b-fields');
  function renderFields(){
    host.innerHTML='';
    fields.forEach((f,i)=>{
      const row=el('<div class="field-row"></div>');
      row.appendChild(el(`<div class="grid2"><div><label class="lbl">key</label><input type="text" class="mono" data-f="key" value="${esc(f.key)}"></div><div><label class="lbl">kind</label><select data-f="kind">${KIND_OPTS.map(k=>`<option ${k===f.kind?'selected':''}>${k}</option>`).join('')}</select></div></div>`));
      row.appendChild(el(`<div style="margin-top:7px"><label class="lbl">prompt</label><input type="text" data-f="prompt" value="${esc(f.prompt)}"></div>`));
      row.appendChild(el(`<div class="grid2" style="margin-top:7px"><div><label class="lbl">maxlen</label><input type="text" class="mono" data-f="maxlen" value="${esc(f.maxlen)}" placeholder="0 = none"></div><div><label class="lbl">unique</label><input type="text" class="mono" data-f="unique" value="${esc(f.unique)}" placeholder="e.g. role_code"></div></div>`));
      row.appendChild(el(`<div class="grid2" style="margin-top:7px"><div><label class="lbl">suggest_from</label><input type="text" class="mono" data-f="suggest_from" value="${esc(f.suggest_from)}"></div><div><label class="lbl">auto_from</label><input type="text" class="mono" data-f="auto_from" value="${esc(f.auto_from)}"></div></div>`));
      const act=el('<div class="row-actions"></div>'); const tg=el('<div class="toggles"></div>');
      TG.forEach(([prop,lab])=>tg.appendChild(el(`<span class="tg" role="button" tabindex="0" aria-pressed="${f[prop]}" data-tg="${prop}">${lab}</span>`)));
      act.appendChild(tg); act.appendChild(el('<button class="btn danger sm" data-del>remove</button>')); row.appendChild(act);
      row.querySelectorAll('[data-f]').forEach(inp=>{const p=inp.dataset.f; const h=()=>{f[p]=inp.value;sync();}; inp.addEventListener('input',h); inp.addEventListener('change',h);});
      row.querySelectorAll('[data-tg]').forEach(b=>{const t=()=>{const p=b.dataset.tg; f[p]=!f[p]; b.setAttribute('aria-pressed',f[p]); sync();}; b.addEventListener('click',t); b.addEventListener('keydown',e=>{if(e.key===' '||e.key==='Enter'){e.preventDefault();t();}});});
      row.querySelector('[data-del]').addEventListener('click',()=>{fields.splice(i,1);renderFields();sync();});
      host.appendChild(row);
    });
  }
  const pyStr=(s)=>'"'+String(s).replace(/\\/g,'\\\\').replace(/"/g,'\\"')+'"';
  function genField(f){
    const p=[pyStr(f.key),pyStr(f.prompt)];
    if(f.kind&&f.kind!=='text') p.push(`kind=${pyStr(f.kind)}`);
    if(f.optional) p.push('optional=True');
    if(f.auto_from) p.push(`auto_from=${pyStr(f.auto_from)}`);
    if(f.suggest_from) p.push(`suggest_from=${pyStr(f.suggest_from)}`);
    if(f.is_code) p.push('is_code=True');
    if(f.maxlen&&String(f.maxlen).trim()&&String(f.maxlen).trim()!=='0') p.push(`maxlen=${parseInt(f.maxlen,10)||0}`);
    if(f.unique) p.push(`unique=${pyStr(f.unique)}`);
    return `        EField(${p.join(', ')}),`;
  }
  function genDefaults(raw){
    const pairs=String(raw||'').split(',').map(s=>s.trim()).filter(Boolean).map(p=>{const i=p.indexOf('='); if(i<0)return null; return `${pyStr(p.slice(0,i).trim())}: ${pyStr(p.slice(i+1).trim())}`;}).filter(Boolean);
    return pairs.length?`{${pairs.join(', ')}}`:'{}';
  }
  function gen(){
    const key=document.getElementById('b-key').value.trim()||'entity';
    const label=document.getElementById('b-label').value.trim()||key;
    const tool=document.getElementById('b-tool').value.trim()||'addEntity_post';
    return `${pyStr(key)}: {\n    "label": ${pyStr(label)}, "tool": ${pyStr(tool)},\n    "fields": (\n${fields.map(genField).join('\n')}\n    ),\n    "defaults": ${genDefaults(document.getElementById('b-defaults').value)},\n},`;
  }
  function sync(){
    const key=document.getElementById('b-key').value.trim()||'entity';
    document.getElementById('b-cap').textContent=`_ENTITY_CREATE[${pyStr(key)}]`;
    document.getElementById('b-out').textContent=gen();
  }
  document.getElementById('b-add').addEventListener('click',()=>{fields.push({key:'newField',prompt:'Prompt text?',kind:'text',optional:false,is_code:false,maxlen:'',unique:'',suggest_from:'',auto_from:''});renderFields();sync();});
  ['b-key','b-label','b-tool','b-defaults'].forEach(id=>document.getElementById(id).addEventListener('input',sync));
  wireCopy(document.getElementById('b-copy'),gen);
  renderFields(); sync();

  // ---- scrollspy TOC ----
  const links=[...document.querySelectorAll('nav.toc a')];
  const byId={}; links.forEach(a=>byId[a.getAttribute('href').slice(1)]=a);
  const setActive=(id)=>{ links.forEach(a=>a.classList.remove('active')); const a=byId[id]; if(a){a.classList.add('active');
    a.scrollIntoView({block:'nearest',inline:'nearest'}); } };
  const sections=[...document.querySelectorAll('main section[id]')];
  let active=null;
  const spy=new IntersectionObserver((entries)=>{
    // pick the entry nearest the top that is intersecting
    const vis=entries.filter(e=>e.isIntersecting).sort((a,b)=>a.boundingClientRect.top-b.boundingClientRect.top);
    if(vis.length){ const id=vis[0].target.id; if(id!==active){active=id;setActive(id);} }
  },{rootMargin:'-55px 0px -70% 0px',threshold:0});
  sections.forEach(s=>spy.observe(s));
  links.forEach(a=>a.addEventListener('click',()=>{const id=a.getAttribute('href').slice(1); active=id; setActive(id);}));
  if(sections[0]) setActive(sections[0].id);
