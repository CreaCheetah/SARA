const BASE = location.origin;
let authHeader = null;
const $ = id => document.getElementById(id);
const setStrong = (el,val)=>{el.textContent=val;el.className=(val===true||val==="open")?"ok":((val===false||val==="closed")?"no":"");};

$("login").onclick=()=>{authHeader="Basic "+btoa(`${$("user").value}:${$("pass").value}`);$("authStatus").textContent="Ingelogd (alleen in deze browser)";};

async function getStatus(){
  const r=await fetch(`${BASE}/runtime/status`,{cache:"no-store"});
  if(!r.ok)return;
  const j=await r.json();
  setStrong($("mode"),j.mode);
  setStrong($("delivery"),j.delivery_enabled);
  setStrong($("pickup"),j.pickup_enabled);
  setStrong($("bot"),j.bot_enabled);
  $("kitchen").textContent=j.kitchen_closed?"gesloten":"open";
  $("win").textContent=`${j.window.open} / ${j.window.delivery} / ${j.window.close}`;
  $("now").textContent=new Date(j.now).toLocaleString();
  $("bot_enabled").checked=!!j.bot_enabled;
  $("kitchen_closed").checked=!!j.kitchen_closed;
  $("pasta_available").checked=!!j.pasta_available;
  $("delay_pasta_minutes").value=j.delay_pasta_minutes;
  $("delay_schotels_minutes").value=j.delay_schotels_minutes;
  $("delivery_enabled_null").checked=false;
  $("pickup_enabled_null").checked=false;
  $("delivery_enabled").checked=!!j.delivery_enabled;
  $("pickup_enabled").checked=!!j.pickup_enabled;
  document.querySelector(`input[name="mode"][value="auto"]`).checked=true;
}
$("refresh").onclick=getStatus;

async function save(){
  if(!authHeader){$("saveStatus").textContent="Eerst inloggen.";return;}
  const delManual=$("delivery_enabled_null").checked;
  const pickManual=$("pickup_enabled_null").checked;
  const body={
    bot_enabled:$("bot_enabled").checked,
    kitchen_closed:$("kitchen_closed").checked,
    pasta_available:$("pasta_available").checked,
    delay_pasta_minutes:+$("delay_pasta_minutes").value,
    delay_schotels_minutes:+$("delay_schotels_minutes").value,
    is_open_override:(document.querySelector('input[name="mode"]:checked')||{}).value||"auto",
    delivery_enabled:delManual?$("delivery_enabled").checked:null,
    pickup_enabled:pickManual?$("pickup_enabled").checked:null,
    ttl_minutes:null
  };
  const r=await fetch(`${BASE}/admin/toggles`,{method:"POST",headers:{"Content-Type":"application/json","Authorization":authHeader},body:JSON.stringify(body)});
  $("saveStatus").textContent=r.ok?"Opgeslagen.":`Fout: ${r.status}`;
  if(r.ok)getStatus();
}
$("save").onclick=save;

let timer=setInterval(getStatus,5000);
$("autopoll").onchange=e=>{if(e.target.checked){timer=setInterval(getStatus,5000)}else{clearInterval(timer)}};
getStatus();
