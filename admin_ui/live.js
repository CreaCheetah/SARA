const BASE = location.origin;
const $ = id => document.getElementById(id);

const ALLOWED = [0,10,20,30,45,60];
const roundAllowed = v => {
  let n = Math.max(0, Math.min(60, Math.round(v/5)*5));
  let best = ALLOWED[0], d = 1e9;
  for (const a of ALLOWED){ const diff = Math.abs(a-n); if(diff<d){d=diff;best=a;} }
  return best;
};

const setPill = (el, ok) => { el.textContent = ok ? "aan" : "uit"; el.className = ok ? "ok" : "no"; };
const theme = mode => document.body.classList.toggle('mode-closed', mode === 'closed');
const setChip = (el, label, ok) => { el.textContent = `${label}: ${ok ? "aan" : "uit"}`; el.className = `chip ${ok ? "ok" : "no"}`; };
const setOpenChip = (mode) => { const ok = (mode === "open"); $("chip_open").textContent = `Restaurant: ${ok?"open":"dicht"}`; $("chip_open").className = `chip ${ok?"ok":"no"}`; };

function syncRadiosFromMode(mode){
  const v = (mode === 'open') ? 'open' : (mode === 'closed' ? 'closed' : 'auto');
  const el = document.querySelector(`input[name="mode"][value="${v}"]`);
  if (el) el.checked = true;
}

function setDeliveryDisabled(disabled){
  $("delivery_enabled").disabled = disabled;
  $("sw_delivery").classList.toggle('disabled', disabled);
}

function makePresets(nodeId, inputId, badgeId){
  const host = $(nodeId);
  host.innerHTML = "";
  ALLOWED.forEach(v=>{
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip-btn";
    b.textContent = v;
    b.onclick = () => {
      $(inputId).value = v;
      $(badgeId).textContent = v;
      [...host.children].forEach(x=>x.classList.remove("active"));
      b.classList.add("active");
    };
    host.appendChild(b);
  });
}

function updateLatency(ms){
  const dot = $("latency");
  dot.classList.remove("ok","slow","bad");
  if (ms < 300) dot.classList.add("ok");
  else if (ms < 800) dot.classList.add("slow");
  else dot.classList.add("bad");
}

async function api(path, opts){
  const t0 = performance.now();
  const r = await fetch(`${BASE}${path}`, opts);
  updateLatency(performance.now() - t0);
  return r;
}

async function getStatus(){
  const r = await api(`/runtime/status`, {cache:"no-store"});
  if(!r.ok) return;
  const j = await r.json();

  const isOpen = (j.mode === 'open');
  setPill($("st_open"), isOpen);
  setPill($("st_bot"), !!j.bot_enabled);
  setChip($("chip_mada"), "SARA", !!j.bot_enabled);
  setOpenChip(j.mode);
  $("win").textContent = `Tijdvenster ${j.window.open} • ${j.window.delivery} • ${j.window.close}`;
  $("svctime").textContent = `Systeemtijd: ${new Date(j.now).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'})}`;

  theme(j.mode);
  syncRadiosFromMode(j.mode);

  $("bot_enabled").checked = !!j.bot_enabled;
  $("pasta_available").checked = !!j.pasta_available;

  const dp = roundAllowed(+j.delay_pasta_minutes || 0);
  const ds = roundAllowed(+j.delay_schotels_minutes || 0);
  $("delay_pasta_minutes").value = dp; $("badge_pasta").textContent = dp;
  $("delay_schotels_minutes").value = ds; $("badge_schotels").textContent = ds;

  setDeliveryDisabled(j.mode === 'closed');
  $("delivery_enabled").checked = !!j.delivery_enabled;
}

function hookSliders(){
  $("delay_pasta_minutes").addEventListener('input', e=>{
    const v = roundAllowed(+e.target.value);
    $("badge_pasta").textContent = v;
  });
  $("delay_schotels_minutes").addEventListener('input', e=>{
    const v = roundAllowed(+e.target.value);
    $("badge_schotels").textContent = v;
  });
  makePresets("pre_pasta","delay_pasta_minutes","badge_pasta");
  makePresets("pre_schotels","delay_schotels_minutes","badge_schotels");
}

async function save(){
  const body = {
    bot_enabled: $("bot_enabled").checked,
    kitchen_closed: false,
    pasta_available: $("pasta_available").checked,
    delay_pasta_minutes: roundAllowed(+$("delay_pasta_minutes").value),
    delay_schotels_minutes: roundAllowed(+$("delay_schotels_minutes").value),
    is_open_override: (document.querySelector('input[name="mode"]:checked')||{}).value || "auto",
    delivery_enabled: $("delivery_enabled").checked
  };

  const btn = $("save");
  const old = btn.textContent;
  try{
    const r = await api(`/admin/toggles`, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    if(r.ok){
      btn.textContent = "Opgeslagen ✓";
      $("savedNote").textContent = `laatst opgeslagen ${new Date().toLocaleTimeString()}`;
      setTimeout(()=>{ btn.textContent = old; }, 1500);
      getStatus();
    }else{
      btn.textContent = `Fout ${r.status}`; setTimeout(()=>{ btn.textContent = old; }, 2000);
    }
  }catch(_){
    btn.textContent = "Netwerkfout"; setTimeout(()=>{ btn.textContent = old; }, 2000);
  }
}

function resetDefaults(){
  document.querySelector('input[name="mode"][value="auto"]').checked = true;
  $("delivery_enabled").checked = false;
  $("delay_pasta_minutes").value = 0; $("badge_pasta").textContent = 0;
  $("delay_schotels_minutes").value = 0; $("badge_schotels").textContent = 0;
  $("bot_enabled").checked = true;
  $("pasta_available").checked = true;
  save();
}

$("save").onclick = save;
$("reset").onclick = resetDefaults;

let timer = setInterval(getStatus, 5000);
$("autopoll").onchange = e => { if(e.target.checked){ timer=setInterval(getStatus,5000) } else { clearInterval(timer) } };

document.addEventListener("visibilitychange", ()=>{
  if(document.hidden){ clearInterval(timer); }
  else if($("autopoll").checked){ timer = setInterval(getStatus, 5000); getStatus(); }
});

document.addEventListener("keydown", (e)=>{ if(e.key==="Enter") save(); });

hookSliders();
getStatus();
