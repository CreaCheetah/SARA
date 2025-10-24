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

function syncRadiosFromMode(mode){
  const v = (mode === 'open') ? 'open' : (mode === 'closed' ? 'closed' : 'auto');
  const el = document.querySelector(`input[name="mode"][value="${v}"]`);
  if (el) el.checked = true;
}

function setDeliveryDisabled(disabled){
  $("delivery_enabled").disabled = disabled;
  $("sw_delivery").classList.toggle('disabled', disabled);
}

async function getStatus(){
  const r = await fetch(`${BASE}/runtime/status`, {cache:"no-store"});
  if(!r.ok) return;
  const j = await r.json();

  // status
  const isOpen = (j.mode === 'open');
  setPill($("st_open"), isOpen);
  setPill($("st_delivery"), !!j.delivery_enabled);
  setPill($("st_bot"), !!j.bot_enabled);
  $("win").textContent = `Tijdvenster ${j.window.open} • ${j.window.delivery} • ${j.window.close}`;

  // thema + radios
  theme(j.mode);
  syncRadiosFromMode(j.mode);

  // inputs
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
    $("badge_pasta").textContent = roundAllowed(+e.target.value);
  });
  $("delay_schotels_minutes").addEventListener('input', e=>{
    $("badge_schotels").textContent = roundAllowed(+e.target.value);
  });
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
    const r = await fetch(`${BASE}/admin/toggles`, {
      method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)
    });
    if(r.ok){
      btn.textContent = "Opgeslagen ✓"; btn.classList.remove('err'); btn.classList.add('ok');
      setTimeout(()=>{ btn.textContent = old; btn.classList.remove('ok'); }, 1500);
      getStatus();
    }else{
      btn.textContent = `Fout ${r.status}`; btn.classList.remove('ok'); btn.classList.add('err');
      setTimeout(()=>{ btn.textContent = old; btn.classList.remove('err'); }, 2000);
    }
  }catch(_){
    btn.textContent = "Netwerkfout"; btn.classList.remove('ok'); btn.classList.add('err');
    setTimeout(()=>{ btn.textContent = old; btn.classList.remove('err'); }, 2000);
  }
}

$("save").onclick = save;

let timer = setInterval(getStatus, 5000);
$("autopoll").onchange = e => { if(e.target.checked){ timer=setInterval(getStatus,5000) } else { clearInterval(timer) } };

hookSliders();
getStatus();
