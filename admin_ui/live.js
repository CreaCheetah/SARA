const BASE = location.origin;
const $ = id => document.getElementById(id);

const setStrong = (el, val) => {
  el.textContent = String(val);
  el.className = (val === true || val === "open") ? "ok"
             : (val === false || val === "closed") ? "no"
             : "";
};

async function getStatus() {
  try {
    const r = await fetch(`${BASE}/runtime/status`, { cache: "no-store" });
    if (!r.ok) return;
    const j = await r.json();

    setStrong($("mode"), j.mode);
    setStrong($("delivery"), j.delivery_enabled);
    setStrong($("bot"), j.bot_enabled);
    $("win").textContent = `${j.window.open} / ${j.window.delivery} / ${j.window.close}`;

    $("bot_enabled").checked = !!j.bot_enabled;
    $("pasta_available").checked = !!j.pasta_available;
    $("delay_pasta_minutes").value = j.delay_pasta_minutes;
    $("delay_schotels_minutes").value = j.delay_schotels_minutes;
    $("delivery_enabled").checked = !!j.delivery_enabled;
  } catch (_) {
    // stil falen
  }
}

async function save() {
  const body = {
    bot_enabled: $("bot_enabled").checked,
    kitchen_closed: false, // verwijderd in UI, server veilig dicht = false
    pasta_available: $("pasta_available").checked,
    delay_pasta_minutes: +$("delay_pasta_minutes").value,
    delay_schotels_minutes: +$("delay_schotels_minutes").value,
    is_open_override: (document.querySelector('input[name="mode"]:checked') || {}).value || "auto",
    delivery_enabled: $("delivery_enabled").checked,
    pickup_enabled: null // niet gebruikt
    // ttl_minutes NIET meesturen -> server default
  };

  try {
    const r = await fetch(`${BASE}/admin/toggles`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    $("saveStatus").textContent = r.ok ? "Opgeslagen." : `Fout: ${r.status}`;
    if (r.ok) getStatus();
  } catch (_) {
    $("saveStatus").textContent = "Fout: netwerk";
  }
}

$("save").onclick = save;

let pollHandle = null;
function startPoll() {
  stopPoll();
  pollHandle = setInterval(getStatus, 5000);
}
function stopPoll() {
  if (pollHandle) {
    clearInterval(pollHandle);
    pollHandle = null;
  }
}

$("autopoll").onchange = e => {
  if (e.target.checked) startPoll();
  else stopPoll();
};

// init
getStatus();
if ($("autopoll").checked) startPoll();
