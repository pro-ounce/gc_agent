(function(){
  "use strict";
  // Base-path aware: works served at /admin OR behind a proxy prefix (/<prefix>/admin).
  var _ai = location.pathname.indexOf("/admin");
  var API = _ai >= 0 ? location.pathname.slice(0, _ai + 6) : "/admin";   // ".../admin"
  var ROOT = API.replace(/\/admin$/, "");                                // proxy prefix ("" if none) — used by the Metrics tab
  var $ = function(s,r){return (r||document).querySelector(s);};
  var elTabs = $("#tabs"), elSections = $("#sections"), elStatus = $("#status");
  var btnSave = $("#btn-save"), btnReset = $("#btn-reset");
  var esc = function(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});};
  // Safe DOM id from an arbitrary config key/group (used to wire aria-labelledby / tabs).
  var sid = function(s){return String(s==null?"":s).replace(/[^\w-]/g,"_");};
  var params = [];       // from server
  var pending = {};       // key -> new value (dirty)

  function setStatus(msg, cls){ elStatus.textContent = msg||""; elStatus.className = cls||""; }
  function markDirty(){ var n=Object.keys(pending).length; btnSave.disabled = n===0;
    if(n) setStatus(n+" unsaved change"+(n>1?"s":""), "dirty"); else setStatus("", ""); }

  function ctlFor(p){
    var v = (p.key in pending) ? pending[p.key] : p.value;
    // Associate every control with its parameter name (+ description) so assistive tech
    // announces a meaningful accessible name, and expose the current state textually.
    var lab = ' aria-labelledby="lbl-'+sid(p.key)+' desc-'+sid(p.key)+'"';
    if(p.type==="bool"){
      var on = !!v;
      return '<label class="switch"><input type="checkbox" data-key="'+esc(p.key)+'"'+(on?" checked":"")+lab+'>'
        +'<span class="track" aria-hidden="true"><span class="knob"></span></span><span class="state">'+(on?"On":"Off")+'</span></label>';
    }
    if(p.type==="select"){
      var opts=(p.options||[]).map(function(o){return '<option value="'+esc(o)+'"'+(String(o)===String(v)?" selected":"")+'>'+esc(o)+'</option>';}).join("");
      return '<div class="ctl"><select data-key="'+esc(p.key)+'"'+lab+'>'+opts+'</select></div>';
    }
    if(p.type==="int"||p.type==="float"){
      var step = p.type==="float" ? (p.step||0.1) : 1;
      var mn = ("min" in p)?' min="'+p.min+'"':"", mx=("max" in p)?' max="'+p.max+'"':"";
      return '<div class="ctl"><input type="number" data-key="'+esc(p.key)+'" value="'+esc(v)+'" step="'+step+'"'+mn+mx+lab+'></div>';
    }
    return '<div class="ctl"><input type="text" data-key="'+esc(p.key)+'" value="'+esc(v)+'"'+lab+'></div>';
  }

  function cardFor(p){
    var ovr = p.overridden ? '<span class="ovr">overridden</span>' : '';
    return '<div class="card"><div class="top"><span class="lbl" id="lbl-'+sid(p.key)+'">'+esc(p.label)+ovr+'</span>'
      +'<span class="key mono">'+esc(p.key)+'</span></div>'
      +'<div class="desc" id="desc-'+sid(p.key)+'">'+esc(p.description)+'</div>'
      + ctlFor(p)
      +'<div class="def">default: <span class="mono">'+esc(p.default)+'</span></div></div>';
  }

  function tabBtn(label, key, active){
    return '<button class="admin-tab'+(active?" active":"")+'" data-tab="'+esc(key)+'" role="tab"'
      +' id="tab-'+sid(key)+'" aria-controls="panel-'+sid(key)+'" aria-selected="'+(active?"true":"false")+'"'
      +' tabindex="'+(active?"0":"-1")+'">'+esc(label)+'</button>';
  }

  function render(){
    var groups = [];
    params.forEach(function(p){ if(groups.indexOf(p.group)<0) groups.push(p.group); });
    elTabs.innerHTML = groups.map(function(g,i){ return tabBtn(g,g,i===0); }).join("")
      + tabBtn("Metrics","__metrics__",false)
      + tabBtn("Activity","__activity__",false)
      + tabBtn("Backups","__backups__",false)
      + tabBtn("Logs","__logs__",false);
    elSections.innerHTML = groups.map(function(g,i){
      var cards = params.filter(function(p){return p.group===g;}).map(cardFor).join("");
      return '<section class="admin-section'+(i===0?" active":"")+'" data-tab="'+esc(g)+'" role="tabpanel"'
        +' id="panel-'+sid(g)+'" aria-labelledby="tab-'+sid(g)+'" tabindex="0"><div class="cards">'+cards+'</div></section>';
    }).join("") + metricsSectionHTML() + activitySectionHTML() + backupsSectionHTML() + logsSectionHTML();
    // tab switching — WAI-ARIA tabs: roving tabindex, arrow/Home/End keys, aria-selected.
    var tabEls = Array.prototype.slice.call(elTabs.children);
    function selectTab(btn){
      tabEls.forEach(function(b){
        var on = b===btn;
        b.classList.toggle("active", on);
        b.setAttribute("aria-selected", on?"true":"false");
        b.tabIndex = on?0:-1;
      });
      Array.prototype.forEach.call(elSections.children, function(s){
        s.classList.toggle("active", s.getAttribute("data-tab")===btn.getAttribute("data-tab"));
      });
    }
    tabEls.forEach(function(btn, idx){
      btn.addEventListener("click", function(){ selectTab(btn); });
      btn.addEventListener("keydown", function(e){
        var i=null;
        if(e.key==="ArrowRight"||e.key==="ArrowDown") i=(idx+1)%tabEls.length;
        else if(e.key==="ArrowLeft"||e.key==="ArrowUp") i=(idx-1+tabEls.length)%tabEls.length;
        else if(e.key==="Home") i=0;
        else if(e.key==="End") i=tabEls.length-1;
        if(i!==null){ e.preventDefault(); tabEls[i].focus(); selectTab(tabEls[i]); }
      });
    });
    // input wiring
    Array.prototype.forEach.call(elSections.querySelectorAll("[data-key]"), function(inp){
      var handler = function(){
        var key = inp.getAttribute("data-key");
        var spec = params.filter(function(p){return p.key===key;})[0];
        var val;
        if(inp.type==="checkbox"){ val = inp.checked; var st=inp.parentNode.querySelector(".state"); if(st) st.textContent = val?"On":"Off"; }
        else if(spec.type==="int") val = parseInt(inp.value,10);
        else if(spec.type==="float") val = parseFloat(inp.value);
        else val = inp.value;
        // dirty if differs from current server value
        if(String(val)===String(spec.value)) delete pending[key]; else pending[key]=val;
        markDirty();
      };
      inp.addEventListener(inp.type==="checkbox"?"change":"input", handler);
      if(inp.tagName==="SELECT") inp.addEventListener("change", handler);
    });
    initBackups();
    initMetrics();
    initActivity();
    initLogs();
  }

  // ── Backups tab (full console: repo · stats · schedule · manual · snapshots) ──
  function badge(txt, color, bg){ return '<span style="font-size:11px;font-weight:600;color:'+color+';background:'+bg+';border-radius:999px;padding:2px 9px">'+esc(txt)+'</span>'; }
  function fmtDur(ms){ if(ms==null) return "—"; return ms>=1000?(ms/1000).toFixed(1)+"s":ms+"ms"; }
  function stateBadge(s){
    if(s==="SUCCESS") return badge("SUCCESS","var(--good)","var(--good-wash)");
    if(s==="IN_PROGRESS") return badge("IN_PROGRESS","var(--amber)","var(--amber-wash)");
    if(s==="FAILED"||s==="PARTIAL") return badge(s,"#b91c1c","#fdeaea");
    return badge(s||"?","var(--ink-2)","#f3f4f6");
  }

  function backupsSectionHTML(){
    return '<section class="admin-section" data-tab="__backups__" role="tabpanel" id="panel-__backups__" aria-labelledby="tab-__backups__" tabindex="0">'
      // repo + stats
      +'<div class="cards" style="margin-bottom:14px">'
      +'<div class="card"><div class="top"><span class="lbl">Repository</span><span id="bk-repo-verify"></span></div>'
      +'<div class="def" style="margin-top:8px">name: <span class="mono" id="bk-repo-name">—</span></div>'
      +'<div class="def">indices: <span class="mono" id="bk-repo-indices">—</span></div></div>'
      +'<div class="card"><div class="top"><span class="lbl">Stats</span></div>'
      +'<div class="def" style="margin-top:8px">snapshots: <b id="bk-stat-total">—</b> &nbsp; last success: <span class="mono" id="bk-stat-last">—</span></div>'
      +'<div class="def" id="bk-stat-states"></div></div></div>'
      // schedule
      +'<div class="card" style="margin-bottom:14px"><div class="top"><span class="lbl">Schedule <span id="bk-sch-enabled"></span></span>'
      +'<button id="bk-sch-toggle" class="btn" style="padding:5px 11px">—</button></div>'
      +'<div class="desc">Daily Snapshot-Management policy. Uses standard 5-field cron (min hour dom mon dow).</div>'
      +'<div class="form-row" style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end">'
      +'<label style="flex:1;min-width:180px">cron<br><input type="text" id="bk-sch-cron" placeholder="0 2 * * *" style="width:100%;font:inherit;font-size:14px;padding:9px 11px;border:1px solid var(--line);border-radius:9px" class="mono"></label>'
      +'<label style="width:150px">retention (days)<br><input type="number" id="bk-sch-ret" min="1" max="365" style="width:100%;font:inherit;font-size:14px;padding:9px 11px;border:1px solid var(--line);border-radius:9px"></label>'
      +'<button id="bk-sch-save" class="btn primary">Save schedule</button><span id="bk-sch-status" role="status" aria-live="polite" style="font-size:13px"></span></div>'
      +'<div class="def" id="bk-sch-runs" style="margin-top:10px"></div></div>'
      // manual
      +'<div class="card" style="margin-bottom:14px"><div class="top"><span class="lbl">Take a snapshot now</span></div>'
      +'<div class="desc">Trigger a snapshot immediately — do this before a major push. Scheduled snapshots keep running regardless.</div>'
      +'<div style="display:flex;gap:9px;align-items:center;flex-wrap:wrap">'
      +'<input type="text" id="bk-label" aria-label="Snapshot label (optional)" placeholder="label (optional, e.g. pre-v1.5-deploy)" style="flex:1;min-width:220px;font:inherit;font-size:14px;padding:9px 11px;border:1px solid var(--line);border-radius:9px">'
      +'<button id="bk-take" class="btn primary">Take snapshot now</button><span id="bk-status" role="status" aria-live="polite" style="font-size:13px"></span></div></div>'
      // snapshots table
      +'<div class="card"><div class="top"><span class="lbl">Recent snapshots</span>'
      +'<button id="bk-refresh" class="btn" style="padding:5px 11px">Refresh</button></div>'
      +'<div id="bk-list" class="desc" style="margin-top:10px">loading…</div></div></section>';
  }

  function renderOverview(d){
    // repo + stats
    var rv = document.getElementById("bk-repo-verify");
    if(rv) rv.innerHTML = d.repository && d.repository.verified ? badge("verified","var(--good)","var(--good-wash)") : badge("unverified","#b91c1c","#fdeaea");
    document.getElementById("bk-repo-name").textContent = (d.repository&&d.repository.name)||"—";
    document.getElementById("bk-repo-indices").textContent = d.indices||"—";
    var st = d.stats||{};
    document.getElementById("bk-stat-total").textContent = st.total!=null?st.total:"—";
    document.getElementById("bk-stat-last").textContent = st.last_success||"—";
    document.getElementById("bk-stat-states").innerHTML = Object.keys(st.by_state||{}).map(function(k){return k+": "+st.by_state[k];}).join(" · ");
    // schedule
    var sch = d.schedule||{};
    var en = document.getElementById("bk-sch-enabled");
    en.innerHTML = !sch.exists ? badge("not set up","var(--ink-2)","#f3f4f6") : (sch.enabled?badge("enabled","var(--good)","var(--good-wash)"):badge("disabled","var(--amber)","var(--amber-wash)"));
    document.getElementById("bk-sch-cron").value = sch.cron||"0 2 * * *";
    document.getElementById("bk-sch-ret").value = sch.retention_days!=null?sch.retention_days:14;
    var tg = document.getElementById("bk-sch-toggle");
    tg.textContent = sch.exists ? (sch.enabled?"Disable":"Enable") : "—";
    tg.disabled = !sch.exists; tg.dataset.next = sch.enabled?"false":"true";
    document.getElementById("bk-sch-save").textContent = sch.exists?"Save schedule":"Set up schedule";
    var runs = "";
    if(sch.last_execution){ runs += "last run: "+stateBadge(sch.last_execution.status)+" <span class='mono'>"+esc(sch.last_execution.time||"")+"</span>"; }
    if(sch.next_execution){ runs += (runs?" &nbsp; ":"")+"next: <span class='mono'>"+esc(sch.next_execution)+"</span>"; }
    document.getElementById("bk-sch-runs").innerHTML = runs;
    // snapshots table
    var el = document.getElementById("bk-list");
    if(d.error){ el.innerHTML = '<span style="color:#b91c1c">'+esc(d.error)+'</span>'; return; }
    var snaps = d.snapshots||[];
    if(!snaps.length){ el.innerHTML = "No snapshots yet."; return; }
    var rows = snaps.map(function(s){
      return '<tr style="border-top:1px solid var(--line)">'
        +'<td style="padding:8px 6px" class="mono">'+esc(s.snapshot)+'</td>'
        +'<td style="padding:8px 6px">'+stateBadge(s.state)+'</td>'
        +'<td style="padding:8px 6px">'+esc(s.trigger||"")+(s.label?' · '+esc(s.label):"")+'</td>'
        +'<td style="padding:8px 6px;white-space:nowrap">'+esc(s.start_time||"—")+'</td>'
        +'<td style="padding:8px 6px" class="mono">'+fmtDur(s.duration_ms)+'</td></tr>';
    }).join("");
    el.innerHTML = '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:13px">'
      +'<thead><tr style="text-align:left;color:var(--ink-2);font-size:11.5px;text-transform:uppercase;letter-spacing:.04em">'
      +'<th scope="col" style="padding:0 6px 6px">Snapshot</th><th scope="col" style="padding:0 6px 6px">State</th><th scope="col" style="padding:0 6px 6px">Trigger</th>'
      +'<th scope="col" style="padding:0 6px 6px">Started</th><th scope="col" style="padding:0 6px 6px">Duration</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
  }

  function loadBackups(){
    var el = document.getElementById("bk-list"); if(el) el.textContent = "loading…";
    fetch(API+"/backup",{cache:"no-store"}).then(function(r){return r.json();}).then(renderOverview)
      .catch(function(e){ if(el) el.innerHTML='<span style="color:#b91c1c">Failed: '+esc(e.message)+'</span>'; });
  }

  function post(url, body, statusEl, okMsg){
    if(statusEl){ statusEl.textContent="working…"; statusEl.style.color="var(--ink-2)"; }
    return fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})})
      .then(function(r){ return r.json().then(function(d){ return {ok:r.ok,d:d}; }); })
      .then(function(res){
        if(!res.ok){ if(statusEl){statusEl.textContent=res.d.detail||"failed";statusEl.style.color="#b91c1c";} }
        else if(statusEl){ statusEl.textContent=okMsg||"done ✓"; statusEl.style.color="var(--good)"; setTimeout(function(){statusEl.textContent="";},3000); }
        return res;
      });
  }

  function initBackups(){
    var take = document.getElementById("bk-take"); if(!take) return;
    take.addEventListener("click", function(){
      var label=(document.getElementById("bk-label").value||"").trim();
      take.disabled=true;
      post(API+"/backup",{label:label},document.getElementById("bk-status"),"snapshot started ✓")
        .then(function(res){ if(res.ok) setTimeout(loadBackups,1500); }).finally(function(){ take.disabled=false; });
    });
    document.getElementById("bk-sch-save").addEventListener("click", function(){
      var cron=(document.getElementById("bk-sch-cron").value||"").trim();
      var ret=parseInt(document.getElementById("bk-sch-ret").value,10);
      post(API+"/backup/schedule",{cron:cron,retention_days:ret,enabled:true},document.getElementById("bk-sch-status"),"schedule saved ✓")
        .then(function(res){ if(res.ok) renderOverview(res.d.snapshots!=null?res.d:{schedule:res.d}); loadBackups(); });
    });
    document.getElementById("bk-sch-toggle").addEventListener("click", function(){
      var next=this.dataset.next==="true";
      post(API+"/backup/schedule/toggle",{enabled:next},document.getElementById("bk-sch-status"),next?"enabled ✓":"disabled ✓")
        .then(function(){ loadBackups(); });
    });
    document.getElementById("bk-refresh").addEventListener("click", loadBackups);
    loadBackups();
  }

  function ms(v){ if(v==null) return "—"; return v>=1000?(v/1000).toFixed(2)+"s":Math.round(v)+"ms"; }
  function num(v){ return v==null?"—":Number(v).toLocaleString("en-US"); }
  function tile(label, val, foot){
    return '<div class="card"><div class="def" style="margin:0">'+esc(label)+'</div>'
      +'<div class="big" style="margin-top:4px">'+esc(val)+'</div>'
      +(foot?'<div class="def">'+esc(foot)+'</div>':'')+'</div>';
  }

  // ── Metrics tab (live system dashboard: inference device, CPU/GPU/mem, charts, ticker) ──
  function gaugeCard(label,id,keyRight){
    return '<div class="card"><div class="top"><span class="lbl">'+label+'</span><span class="big" id="'+id+'-val">—</span></div>'
      +'<div class="gauge"><div class="bar"><div class="fill" id="'+id+'-fill"></div></div></div>'
      +'<div class="def" id="'+id+'-sub" style="margin-top:8px">—</div></div>';
  }
  function chartCard(label,id,unit){
    return '<div class="card"><div class="top"><span class="lbl">'+label+'</span><span class="big" style="font-size:18px" id="'+id+'-cur">—</span></div>'
      +'<canvas class="chart" id="'+id+'"></canvas></div>';
  }
  function metricsSectionHTML(){
    return '<section class="admin-section" data-tab="__metrics__" role="tabpanel" id="panel-__metrics__" aria-labelledby="tab-__metrics__" tabindex="0">'
      +'<div class="ticker-wrap" aria-hidden="true"><div id="mx-ticker" class="ticker-track">loading…</div></div>'
      +'<div class="admin-header" style="margin-bottom:10px"><span class="lbl" id="mx-device">Checking inference device…</span>'
      +'<span class="btns"><label class="switch" style="gap:6px"><input type="checkbox" id="mx-auto" checked aria-label="Auto-refresh metrics">'
      +'<span class="track" aria-hidden="true"><span class="knob"></span></span><span class="state" style="font-size:12px">live</span></label>'
      +'<button id="mx-refresh" class="btn" style="padding:5px 11px">Refresh</button></span></div>'
      +'<div class="card" style="margin-bottom:14px"><div class="top"><span class="lbl">Performance health</span><span id="mx-verdict" class="badge-dev">assessing…</span></div>'
      +'<div id="mx-kpis" class="cards" style="margin-top:12px"></div></div>'
      +'<div class="cards" style="margin-bottom:14px">'+gaugeCard("CPU","mx-cpu")+gaugeCard("Memory","mx-mem")
      +'<div class="card"><div class="top"><span class="lbl">GPU <span class="key mono" id="mx-gpu-name"></span></span><span class="big" id="mx-gpu-val">—</span></div>'
      +'<div class="gauge"><div class="bar"><div class="fill" id="mx-gpu-fill"></div></div></div>'
      +'<div class="def" id="mx-gpu-sub" style="margin-top:8px">—</div></div></div>'
      +'<div class="cards" style="margin-bottom:14px">'+chartCard("CPU load %","mx-c-cpu")+chartCard("GPU utilisation %","mx-c-gpu")+chartCard("Tokens / sec","mx-c-tok")+'</div>'
      +'<div class="card" style="margin-bottom:14px"><div class="top"><span class="lbl">Runtime</span></div>'
      +'<div id="mx-runtime" class="def" role="status" aria-live="polite" style="margin-top:8px">loading…</div></div>'
      +'<div id="mx-cards" class="cards"></div></section>';
  }

  // Rolling time-series + last token counter for the derived tokens/sec.
  var mxSeries={cpu:[],gpu:[],tok:[]}, mxLast={tok:null,ts:null}, mxTimer=null;
  function pushSeries(a,v){ a.push(v); if(a.length>60) a.shift(); }

  function drawChart(id, data, opts){
    var cv=document.getElementById(id); if(!cv) return; opts=opts||{};
    var dpr=Math.min(2,window.devicePixelRatio||1), w=cv.clientWidth||300, h=cv.clientHeight||84;
    cv.width=w*dpr; cv.height=h*dpr; var g=cv.getContext("2d"); g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,w,h);
    var pad=4, n=data.length, col=opts.color||"#7c3aed";
    var max=opts.max!=null?opts.max:Math.max.apply(null,data.concat([1])); if(max<=0)max=1;
    g.strokeStyle="rgba(17,24,39,.06)"; g.lineWidth=1;
    g.beginPath(); g.moveTo(pad,pad+(h-2*pad)*0.5); g.lineTo(w-pad,pad+(h-2*pad)*0.5); g.stroke();
    if(n<2) return;
    var X=function(i){return pad+(w-2*pad)*(i/(n-1));}, Y=function(v){return pad+(h-2*pad)*(1-Math.max(0,Math.min(max,v))/max);};
    g.beginPath(); g.moveTo(X(0),h-pad); for(var i=0;i<n;i++) g.lineTo(X(i),Y(data[i])); g.lineTo(X(n-1),h-pad); g.closePath();
    var grad=g.createLinearGradient(0,pad,0,h); grad.addColorStop(0,col+"33"); grad.addColorStop(1,col+"05"); g.fillStyle=grad; g.fill();
    g.beginPath(); for(i=0;i<n;i++){ i?g.lineTo(X(i),Y(data[i])):g.moveTo(X(i),Y(data[i])); } g.strokeStyle=col; g.lineWidth=1.8; g.stroke();
    g.beginPath(); g.arc(X(n-1),Y(data[n-1]),2.6,0,7); g.fillStyle=col; g.fill();
  }
  function setGauge(id, pct, big, sub){
    var f=document.getElementById(id+"-fill"); if(!f) return;
    var p=Math.max(0,Math.min(100,pct||0));
    f.style.width=p+"%"; f.className="fill"+(p>=90?" crit":p>=70?" warn":"");
    var v=document.getElementById(id+"-val"); if(v) v.textContent=big;
    var s=document.getElementById(id+"-sub"); if(s) s.textContent=sub;
  }
  function tkItem(k,v){ return '<span class="tk">'+esc(k)+' <b>'+esc(v)+'</b></span>'; }
  function buildTicker(sys, info){
    var c=sys.cpu||{}, m=sys.mem||{}, gp=(sys.gpu||[])[0]||{}, inf=sys.inference||{};
    var r=info.runtime||{}, met=info.metrics||{}, llm=met.llm||{}, turn=met.turn||{};
    var dev=(inf.models||[]).length? (inf.models[0].device+" "+(inf.models[0].gpu_percent)+"%") : "—";
    var items=[["Inference",dev],["CPU",(c.percent!=null?c.percent+"%":"—")],["Load",(c.load&&c.load[0]!=null?c.load[0].toFixed(2):"—")],
      ["Mem",(m.percent!=null?m.percent+"%":"—")],["GPU",(gp.util!=null?gp.util+"%":"—")],["GPU °C",(gp.temp!=null?Math.round(gp.temp)+"°":"—")],
      ["GPU W",(gp.power!=null?gp.power.toFixed(1):"—")],["Model",(r.llm||{}).model||"?"],["Turns",num(turn.count)],
      ["LLM avg",ms(llm.avg_ms)],["Tokens",num((((llm.tokens||{}).input)||0)+(((llm.tokens||{}).output)||0))],["Sessions",num(r.sessions)]];
    var chunk=items.map(function(kv){return tkItem(kv[0],kv[1]);}).join(""); return chunk+chunk;
  }
  function renderDevice(inf){
    var el=document.getElementById("mx-device"); if(!el) return;
    var mods=(inf&&inf.models)||[];
    if(!inf||!inf.available||!mods.length){ el.innerHTML='<span class="badge-dev cpu">No model loaded</span>'; return; }
    el.innerHTML = mods.map(function(mo){
      var cls=mo.device==="GPU"?"gpu":(mo.device==="CPU"?"cpu":"split");
      var icon=mo.device==="GPU"?"⚡":(mo.device==="CPU"?"⚠":"◑");
      return '<span class="badge-dev '+cls+'" style="margin-right:8px">'+icon+' '+esc(mo.name)+' — '+esc(mo.device)+' '+mo.gpu_percent+'%</span>';
    }).join("");
  }
  function renderMetrics(info, sys){
    info=info||{}; sys=sys||{};
    var r=info.runtime||{}, m=info.metrics||{}, llm=m.llm||{}, turn=m.turn||{}, tools=m.tools||{}, mcp=m.mcp||{}, http=m.http||{};
    // device badge + ticker
    renderDevice(sys.inference);
    var tk=document.getElementById("mx-ticker"); if(tk) tk.innerHTML=buildTicker(sys, info);
    // gauges
    var c=sys.cpu||{}, mem=sys.mem||{}, gp=(sys.gpu||[])[0]||{};
    setGauge("mx-cpu", c.percent, (c.percent!=null?c.percent+"%":"—"), (c.cores||"?")+" cores · load "+((c.load&&c.load.length)?c.load.map(function(x){return x.toFixed(2);}).join(" "):"—"));
    setGauge("mx-mem", mem.percent, (mem.percent!=null?mem.percent+"%":"—"), num(mem.used_mb)+" / "+num(mem.total_mb)+" MB"+(mem.swap_total_mb?(" · swap "+num(mem.swap_used_mb)+"/"+num(mem.swap_total_mb)):""));
    var gname=document.getElementById("mx-gpu-name"); if(gname) gname.textContent=gp.name||"—";
    // GB10 unified memory → nvidia-smi mem is N/A; show system RAM as the GPU's (unified) memory.
    var gmem = gp.mem_total_mb ? (num(gp.mem_used_mb)+" / "+num(gp.mem_total_mb)+" MB") : (num(mem.used_mb)+" / "+num(mem.total_mb)+" MB (unified)");
    setGauge("mx-gpu", gp.util, (gp.util!=null?gp.util+"%":"—"), (gp.temp!=null?Math.round(gp.temp)+"°C":"—")+" · "+(gp.power!=null?gp.power.toFixed(1)+"W":"—")+" · "+gmem);
    // charts
    pushSeries(mxSeries.cpu, c.percent||0); pushSeries(mxSeries.gpu, gp.util||0);
    var tok=(((llm.tokens||{}).input)||0)+(((llm.tokens||{}).output)||0), now=sys.ts||Date.now();
    if(mxLast.tok!=null && now>mxLast.ts){ var tps=(tok-mxLast.tok)/((now-mxLast.ts)/1000); pushSeries(mxSeries.tok, tps>0?tps:0); }
    mxLast.tok=tok; mxLast.ts=now;
    drawChart("mx-c-cpu", mxSeries.cpu, {max:100,color:"#7c3aed"}); drawChart("mx-c-gpu", mxSeries.gpu, {max:100,color:"#16a34a"}); drawChart("mx-c-tok", mxSeries.tok, {color:"#2563eb"});
    var cc=document.getElementById("mx-c-cpu-cur"); if(cc) cc.textContent=(c.percent!=null?c.percent+"%":"—");
    var gc=document.getElementById("mx-c-gpu-cur"); if(gc) gc.textContent=(gp.util!=null?gp.util+"%":"—");
    var tc=document.getElementById("mx-c-tok-cur"); if(tc){ var last=mxSeries.tok[mxSeries.tok.length-1]; tc.textContent=(last!=null?last.toFixed(1):"—"); }
    // runtime line + tiles
    var up=r.uptime_seconds||0, uph=up>=3600?(up/3600).toFixed(1)+"h":Math.round(up/60)+"m";
    var rt=document.getElementById("mx-runtime"); if(rt) rt.innerHTML=
      "model <b class=mono>"+esc((r.llm||{}).model||"?")+"</b> · tools <b>"+num(r.tools_loaded)+"</b> · sessions <b>"+num(r.sessions)+"</b> · uptime <b>"+esc(uph)+"</b> · store <b>"+esc(r.store_backend||"?")+"</b>";
    var mc=document.getElementById("mx-cards"); if(mc) mc.innerHTML=
      tile("Turns", num(turn.count), "avg "+ms(turn.avg_ms)+(turn.avg_tools!=null?" · "+Number(turn.avg_tools).toFixed(1)+" tools/turn":""))
     +tile("LLM calls", num(llm.calls), "avg "+ms(llm.avg_ms)+" / call")
     +tile("LLM tokens", num(tok), num((llm.tokens||{}).input)+" in · "+num((llm.tokens||{}).output)+" out")
     +tile("Tool execs", num(tools.executions), "avg "+ms(tools.avg_ms))
     +tile("MCP requests", num(mcp.requests), "avg "+ms(mcp.avg_ms))
     +tile("HTTP requests", num(http.requests), "avg "+ms(http.avg_ms));
  }
  // Performance assessment — grade recent turns against thresholds tuned for a 14B model
  // on the GB10 (num_ctx 8192). 'lower is better' unless better==='hi'.
  function grade(v, good, warn, better){
    if(v==null) return "warn";
    if(better==="hi") return v>=good?"good":(v>=warn?"warn":"bad");
    return v<=good?"good":(v<=warn?"warn":"bad");
  }
  function assessPerf(turns, sys){
    turns=turns||[];
    var done=turns.filter(function(t){return t.total_ms!=null;}), n=done.length;
    function sum(f){ return done.reduce(function(a,t){return a+(f(t)||0);},0); }
    var outTok=sum(function(t){return t.tokens_out;}), llms=sum(function(t){return t.llm_ms;}),
        tot=sum(function(t){return t.total_ms;}), inTok=sum(function(t){return t.tokens_in;}),
        toolms=sum(function(t){return t.tools_ms;});
    var errN=turns.filter(function(t){return t.errors&&t.errors.length;}).length;
    var dev=(sys.inference&&sys.inference.models&&sys.inference.models[0])||null;
    var k=[];
    if(dev && dev.device!=="GPU") k.push(["Inference", dev.device+" "+dev.gpu_percent+"%", dev.device==="CPU"?"bad":"warn", "model not fully on GPU"]);
    // Total-token throughput (prompt+gen ÷ LLM time) is the clearest health signal: a healthy
    // GPU runs in the hundreds of tok/s; a CPU fallback / degraded box collapses to tens.
    var tps=llms>0?(inTok+outTok)/(llms/1000):null;
    k.push(["Token throughput", tps!=null?Math.round(tps)+" tok/s":"—", grade(tps,80,30,"hi"), "prompt+gen ÷ LLM time"]);
    var avgTot=n?tot/n:null;   k.push(["Avg turn latency", avgTot!=null?ms(avgTot):"—", grade(avgTot,6000,15000), n+" turns"]);
    var avgLlm=n?llms/n:null;  k.push(["LLM latency", avgLlm!=null?ms(avgLlm):"—", grade(avgLlm,5000,12000), "per turn"]);
    var avgIn=n?inTok/n:null, load=avgIn!=null?100*avgIn/8192:null;
    k.push(["Prompt load", load!=null?Math.round(load)+"%":"—", grade(load,50,85), "avg "+num(Math.round(avgIn||0))+" / 8192 ctx"]);
    var toolPct=tot>0?100*toolms/tot:null; k.push(["Tool overhead", toolPct!=null?Math.round(toolPct)+"%":"—", grade(toolPct,40,70), "tools ÷ total"]);
    var errPct=turns.length?100*errN/turns.length:0; k.push(["Error rate", Math.round(errPct)+"%", grade(errPct,0.5,10), errN+" / "+turns.length+" turns"]);
    var st=k.map(function(x){return x[2];});
    var verdict = st.indexOf("bad")>=0 ? ["Needs attention","bad"] : (st.indexOf("warn")>=0 ? ["Degraded","warn"] : ["Optimal","ok"]);
    return {kpis:k, verdict:verdict, n:n};
  }
  function renderPerf(turns, sys){
    var a=assessPerf(turns, sys||{});
    var vb=document.getElementById("mx-verdict");
    if(vb){ vb.className="badge-dev "+a.verdict[1]; vb.textContent=(a.verdict[1]==="ok"?"✓ ":a.verdict[1]==="warn"?"◑ ":"⚠ ")+a.verdict[0]+(a.n?" · "+a.n+" turns":" · no data yet"); }
    var el=document.getElementById("mx-kpis"); if(!el) return;
    el.innerHTML=a.kpis.map(function(x){
      return '<div class="card"><div class="def" style="margin:0">'+esc(x[0])+' <span class="dot dot-'+x[2]+'"></span></div>'
        +'<div class="big" style="font-size:20px;margin-top:2px">'+esc(x[1])+'</div><div class="def">'+esc(x[3])+'</div></div>';
    }).join("");
  }
  function loadMetrics(){
    return Promise.all([
      fetch(ROOT+"/actuator/info",{cache:"no-store"}).then(function(r){return r.json();}).catch(function(){return {};}),
      fetch(API+"/system",{cache:"no-store"}).then(function(r){return r.json();}).catch(function(){return {};}),
      fetch(API+"/turns?limit=40",{cache:"no-store"}).then(function(r){return r.json();}).catch(function(){return {turns:[]};})
    ]).then(function(res){ renderMetrics(res[0], res[1]); renderPerf((res[2]||{}).turns||[], res[1]); })
      .catch(function(e){ var rt=document.getElementById("mx-runtime"); if(rt) rt.innerHTML='<span style="color:#b91c1c">Failed: '+esc(e.message)+'</span>'; });
  }
  function metricsActive(){ var s=document.querySelector('[data-tab="__metrics__"]'); return s&&s.classList.contains("active"); }
  function initMetrics(){
    var b=document.getElementById("mx-refresh"); if(!b) return;
    b.addEventListener("click",loadMetrics);
    if(mxTimer){clearInterval(mxTimer);}
    mxTimer=setInterval(function(){ var a=document.getElementById("mx-auto"); if(a&&a.checked&&metricsActive()) loadMetrics(); }, 2500);
    loadMetrics();
  }

  // ── Activity tab (live chat turns: prompt, answer, metrics, errors) ──
  function activitySectionHTML(){
    return '<section class="admin-section" data-tab="__activity__" role="tabpanel" id="panel-__activity__" aria-labelledby="tab-__activity__" tabindex="0">'
      +'<div class="card"><div class="top"><span class="lbl">Live activity <span id="ac-count" class="key"></span></span>'
      +'<span class="btns"><label class="switch" style="gap:6px"><input type="checkbox" id="ac-auto" checked aria-label="Auto-refresh activity">'
      +'<span class="track" aria-hidden="true"><span class="knob"></span></span><span class="state" style="font-size:12px">live</span></label>'
      +'<label class="switch" style="gap:6px"><input type="checkbox" id="ac-erronly" aria-label="Errors only"><span class="track" aria-hidden="true"><span class="knob"></span></span><span class="state" style="font-size:12px">errors</span></label>'
      +'<button id="ac-refresh" class="btn" style="padding:5px 11px">Refresh</button></span></div>'
      +'<div id="ac-list" style="margin-top:8px;max-height:66vh;overflow:auto">loading…</div></div></section>';
  }
  function turnRow(t){
    var err=(t.errors&&t.errors.length)?t.errors:[];
    var toks=(t.tokens_in!=null||t.tokens_out!=null)?(num(t.tokens_in)+"→"+num(t.tokens_out)+" tok"):"";
    var tools=(t.tools&&t.tools.length)?t.tools.map(function(x){return '<span class="pill">'+esc(x)+'</span>';}).join(" "):'<span class="def">no tools</span>';
    var meta=[t.ts?'<span class="mono">'+esc(t.ts)+'</span>':'', t.user_id?"user "+esc(t.user_id):'',
      t.total_ms!=null?'<span class="mono">'+ms(t.total_ms)+'</span> total':'',
      t.llm_ms!=null?"llm "+ms(t.llm_ms):'', t.tools_ms!=null?"tools "+ms(t.tools_ms):'',
      toks, t.outcome?'outcome <span class="mono">'+esc(t.outcome)+'</span>':''].filter(Boolean).join('<span style="opacity:.4">·</span>');
    return '<div class="turn'+(err.length?' err':'')+'">'
      +'<div class="q">'+(t.question?esc(t.question):'<span class="def">(no prompt captured)</span>')+'</div>'
      +(t.answer?'<div class="a">↳ '+esc(t.answer)+(t.blocks?' <span class="pill">'+t.blocks+' card'+(t.blocks>1?'s':'')+'</span>':'')+'</div>':'')
      +err.map(function(e){return '<div class="errline">⚠ '+esc(e)+'</div>';}).join('')
      +'<div class="meta">'+tools+meta+'</div></div>';
  }
  function loadTurns(){
    var el=document.getElementById("ac-list"); if(!el) return;
    var errOnly=(document.getElementById("ac-erronly")||{}).checked;
    fetch(API+"/turns?limit=60",{cache:"no-store"}).then(function(r){return r.json();}).then(function(d){
      var turns=(d.turns||[]).filter(function(t){ return errOnly ? (t.errors&&t.errors.length) : true; });
      var cnt=document.getElementById("ac-count"); if(cnt) cnt.textContent="("+turns.length+")";
      el.innerHTML = turns.length ? turns.map(turnRow).join("") : '<div class="def" style="padding:12px 2px">No chat turns captured yet.</div>';
    }).catch(function(e){ el.innerHTML='<span style="color:#b91c1c">Failed: '+esc(e.message)+'</span>'; });
  }
  var _acTimer=null;
  function activityActive(){ var s=document.querySelector('[data-tab="__activity__"]'); return s&&s.classList.contains("active"); }
  function initActivity(){
    var b=document.getElementById("ac-refresh"); if(!b) return;
    b.addEventListener("click",loadTurns);
    document.getElementById("ac-erronly").addEventListener("change",loadTurns);
    if(_acTimer){clearInterval(_acTimer);}
    _acTimer=setInterval(function(){ var a=document.getElementById("ac-auto"); if(a&&a.checked&&activityActive()) loadTurns(); }, 3000);
    loadTurns();
  }

  // ── Logs tab (recent in-memory logs) ──
  function logsSectionHTML(){
    return '<section class="admin-section" data-tab="__logs__" role="tabpanel" id="panel-__logs__" aria-labelledby="tab-__logs__" tabindex="0">'
      +'<div class="card"><div class="top"><span class="lbl">Recent logs</span>'
      +'<span class="btns"><select id="lg-filter" aria-label="Filter logs by event type" style="font:inherit;font-size:13px;padding:6px 9px;border:1px solid var(--line);border-radius:8px">'
      +'<option value="">all</option><option value="turn_summary">turn_summary</option><option value="chat_prompt">chat_prompt</option>'
      +'<option value="turn_step">turn_step</option><option value="__err">errors</option></select>'
      +'<label class="switch" style="gap:6px"><input type="checkbox" id="lg-auto" aria-label="Auto-refresh logs"><span class="track" aria-hidden="true"><span class="knob"></span></span><span class="state" style="font-size:12px">auto</span></label>'
      +'<button id="lg-refresh" class="btn" style="padding:5px 11px">Refresh</button></span></div>'
      +'<div id="lg-list" class="def" style="margin-top:10px;max-height:60vh;overflow:auto">loading…</div></div></section>';
  }
  function fmtFields(f){
    if(f.event==="turn_summary") return "total="+ms(f.total_ms)+" retrieval="+ms(f.retrieval_ms)+" llm="+ms(f.llm_ms)+" tools="+ms(f.tools_ms)+" iters="+(f.iterations!=null?f.iterations:"?")+" tok="+num(f.prompt_tokens)+"/"+num(f.completion_tokens)+(f.tools_used?" "+JSON.stringify(f.tools_used):"");
    var skip={event:1,step:1,request_id:1,session_id:1,user_id:1,agent:1,func:1};
    return Object.keys(f).filter(function(k){return !skip[k];}).map(function(k){return k+"="+(typeof f[k]==="object"?JSON.stringify(f[k]):f[k]);}).join(" ");
  }
  function loadLogs(){
    var el=document.getElementById("lg-list"); if(!el) return;
    var f=document.getElementById("lg-filter").value;
    var q="?limit=200"; if(f==="__err") q+="&level=ERROR"; else if(f) q+="&event="+encodeURIComponent(f);
    fetch(API+"/logs"+q,{cache:"no-store"}).then(function(r){return r.json();}).then(function(d){
      var rows=(d.logs||[]).map(function(r){
        var lc=r.level==="ERROR"?"#b91c1c":(r.level==="WARNING"?"var(--amber)":"var(--ink-2)");
        var extra=fmtFields(r.fields||{});
        return '<div style="border-top:1px solid var(--line);padding:6px 2px;font-size:12.5px">'
          +'<span class="mono" style="color:var(--ink-2)">'+esc(r.ts)+'</span> '
          +'<span class="mono" style="color:'+lc+'">'+esc(r.level)+'</span> '
          +'<span class="mono" style="color:var(--accent)">'+esc(r.logger)+'</span> '
          +esc(r.msg)
          +(extra?'<div class="mono" style="color:var(--ink-2);margin-left:12px">'+esc(extra)+'</div>':'')+'</div>';
      }).join("");
      el.innerHTML = rows || "No logs captured yet.";
    }).catch(function(e){ el.innerHTML='<span style="color:#b91c1c">Failed: '+esc(e.message)+'</span>'; });
  }
  var _lgTimer=null;
  function initLogs(){
    var b=document.getElementById("lg-refresh"); if(!b) return;
    b.addEventListener("click",loadLogs);
    document.getElementById("lg-filter").addEventListener("change",loadLogs);
    document.getElementById("lg-auto").addEventListener("change",function(){
      if(_lgTimer){clearInterval(_lgTimer);_lgTimer=null;}
      if(this.checked){ _lgTimer=setInterval(loadLogs,4000); loadLogs(); }
    });
    loadLogs();
  }

  function load(){
    setStatus("loading…","");
    fetch(API+"/config",{headers:{Accept:"application/json"},cache:"no-store"})
      .then(function(r){ if(!r.ok) throw new Error("HTTP "+r.status); return r.json(); })
      .then(function(d){ params = d.params||[]; pending={}; render(); markDirty(); setStatus("",""); })
      .catch(function(e){ setStatus("Failed to load: "+e.message,"err"); });
  }

  btnSave.addEventListener("click", function(){
    if(!Object.keys(pending).length) return;
    btnSave.disabled=true; setStatus("saving…","");
    fetch(API+"/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({updates:pending})})
      .then(function(r){ if(!r.ok) throw new Error("HTTP "+r.status); return r.json(); })
      .then(function(d){ params=d.params||[]; pending={}; render(); setStatus("Saved ✓","ok"); setTimeout(function(){setStatus("","");},2500); })
      .catch(function(e){ setStatus("Save failed: "+e.message,"err"); btnSave.disabled=false; });
  });

  btnReset.addEventListener("click", function(){
    if(!confirm("Reset ALL parameters to their .env defaults?")) return;
    setStatus("resetting…","");
    fetch(API+"/config/reset",{method:"POST"})
      .then(function(r){ if(!r.ok) throw new Error("HTTP "+r.status); return r.json(); })
      .then(function(d){ params=d.params||[]; pending={}; render(); setStatus("Reset to defaults ✓","ok"); setTimeout(function(){setStatus("","");},2500); })
      .catch(function(e){ setStatus("Reset failed: "+e.message,"err"); });
  });

  load();
})();
