import os, json, csv, uuid, base64, re
from datetime import datetime, time, timedelta
from typing import Optional, Literal, Dict, Any, List
from urllib.parse import quote_plus
from pathlib import Path

import httpx
from fastapi import FastAPI, Response, HTTPException, Request, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from zoneinfo import ZoneInfo
from starlette.staticfiles import StaticFiles

# ------------ App ------------
app = FastAPI(title="SARA Belassistent")

# ------------ Config ------------
TZ = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))
_host = os.getenv("PUBLIC_BASE_URL", os.getenv("RENDER_EXTERNAL_HOSTNAME", "mada-3ijw.onrender.com"))
BASE_URL = _host if str(_host).startswith("http") else f"https://{_host}"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("TTS_VOICE", "marin")

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin")

REPO_ROOT = Path(__file__).resolve().parent
CONFIG_DELIVERY_PATH = Path(os.getenv("CONFIG_DELIVERY", REPO_ROOT / "config_delivery.json"))
PROMPTS_PATH = Path(os.getenv("PROMPTS_PATH", REPO_ROOT / "prompts_order_nl.json"))
MENU_PATH = Path(os.getenv("MENU_PATH", REPO_ROOT / "menu_ristorante_adam.json"))
CUSTOMER_CSV = os.getenv("CUSTOMER_CSV", "/mnt/data/klanten.csv")
ADMIN_UI_DIR = Path(os.getenv("ADMIN_UI_DIR", REPO_ROOT / "admin_ui"))

# Openingstijden (hard)
OPEN_START, OPEN_END = time(16, 0), time(22, 0)
DEL_START,  DEL_END  = time(17, 0), time(21, 30)

# ------------ Helpers: JSON / Config ------------
def _load_json(path: Path, fallback: Dict[str, Any]) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback

PROMPTS = _load_json(
    PROMPTS_PATH,
    {
        "greet_open_morning": "Goedemorgen, u spreekt met SARA, de digitale assistent van Ristorante Adam Spanbroek. Wat wilt u bestellen?",
        "greet_open_afternoon": "Goedemiddag, u spreekt met SARA, de digitale assistent van Ristorante Adam Spanbroek. Wat wilt u bestellen?",
        "greet_open_evening": "Goedenavond, u spreekt met SARA, de digitale assistent van Ristorante Adam Spanbroek. Wat wilt u bestellen?",
        "greet_closed": "We zijn op dit moment gesloten. U kunt ons weer bereiken vanaf vier uur in de middag.",
        "ask_items": "Wat wilt u bestellen?",
        "item_added": "Genoteerd: {qty}× {name}.",
        "confirm_more": "Wilt u nog iets toevoegen of is dit alles?",
        "summarize": "Samengevat: {items}. Totaal €{total}.",
        "ask_fulfilment": "Wilt u afhalen of laten bezorgen?",
        "ask_phone_for_delivery": "Kunt u uw telefoonnummer geven, dan controleer ik uw adres?",
        "confirm_lookup_found": "Ik heb {straat} {huisnr} in {postcode}. Klopt dat?",
        "confirm_lookup_missing": "Ik heb dit adres niet in mijn systeem. Wat is uw postcode en huisnummer?",
        "ask_payment_delivery": "Betaalt u contant of wilt u een iDEAL-link?",
        "ask_payment_pickup": "Betaalt u bij afhalen contant of pin, of wilt u een iDEAL-link?",
        "eta_delivery": "Uw bestelling wordt bezorgd om {tijd}.",
        "eta_pickup": "Uw bestelling staat klaar om {tijd}.",
        "closing": "Dank u wel voor uw bestelling. Fijne dag.",
        "fallback1": "Ik heb u niet goed verstaan. Kunt u het herhalen?",
        "say_prompt": "Zegt u maar."
    },
)

DELIVERY_CFG = _load_json(
    CONFIG_DELIVERY_PATH,
    {"zones": [], "sla": {"pickup_minutes": 15, "pickup_combo_minutes": 30, "delivery_minutes": 60}},
)

def _load_menu() -> List[Dict[str, Any]]:
    try:
        with open(MENU_PATH, encoding="utf-8") as f:
            data = json.load(f)
        # verwacht lijst van items met keys: code, name/naam, price/prijs
        items = []
        for it in data:
            name = it.get("name") or it.get("naam") or ""
            price = float(it.get("price") or it.get("prijs") or 0)
            code = it.get("code") or it.get("id") or name.lower().replace(" ", "_")[:24]
            if name and price > 0:
                items.append({"code": code, "name": name, "price": price, "norm": name.lower()})
        return items
    except Exception:
        return []

MENU_ITEMS = _load_menu()

# ------------ Basic Auth voor admin ------------
def _is_basic_auth_ok(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return False
    try:
        dec = base64.b64decode(auth.split(" ",1)[1]).decode("utf-8")
        user, pw = dec.split(":",1)
        return (user == ADMIN_USER and pw == ADMIN_PASS)
    except Exception:
        return False

@app.middleware("http")
async def admin_auth_mw(request: Request, call_next):
    p = request.url.path or ""
    if p.startswith("/admin/ui") or p.startswith("/admin/toggles"):
        if not _is_basic_auth_ok(request):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Admin"'},
                content="Unauthorized",
                media_type="text/plain",
            )
    return await call_next(request)

if ADMIN_UI_DIR.exists():
    app.mount("/admin/ui", StaticFiles(directory=str(ADMIN_UI_DIR), html=True), name="admin-ui")

# ------------ Redis + overrides ------------
def _redis():
    from redis import Redis
    return Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)

OVERRIDES_KEY = "mada:overrides"
DEFAULT_OVERRIDES = {
    "bot_enabled": True,
    "pasta_available": True,
    "delay_pasta_minutes": 0,
    "delay_schotels_minutes": 0,
    "is_open_override": "auto",  # auto|open|closed
    "delivery_enabled": False,
}
OVERRIDES_TTL_MIN = int(os.getenv("OVERRIDES_TTL_MIN", "180"))

def load_overrides() -> Dict[str, Any]:
    try:
        r = _redis()
        raw = r.get(OVERRIDES_KEY)
        if not raw:
            return DEFAULT_OVERRIDES.copy()
        data = json.loads(raw)
        out = DEFAULT_OVERRIDES.copy(); out.update({k:v for k,v in data.items() if k in out})
        return out
    except Exception:
        return DEFAULT_OVERRIDES.copy()

def save_overrides(data: Dict[str, Any]):
    body = DEFAULT_OVERRIDES.copy(); body.update({k: data.get(k, body[k]) for k in body.keys()})
    try:
        r = _redis()
        r.set(OVERRIDES_KEY, json.dumps(body, ensure_ascii=False), ex=OVERRIDES_TTL_MIN*60)
    except Exception:
        pass
    return body

# ------------ Runtime status ------------
class RuntimeOut(BaseModel):
    now: str
    mode: Literal["open","closed"]
    delivery_enabled: bool
    window: dict
    bot_enabled: bool
    pasta_available: bool
    delay_pasta_minutes: int
    delay_schotels_minutes: int

def _auto(now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    t = now.time()
    open_now = OPEN_START <= t < OPEN_END
    delivery_now = OPEN_START <= t < OPEN_END and (DEL_START <= t < DEL_END)
    return {"now": now, "mode": "open" if open_now else "closed", "delivery_window": delivery_now}

def evaluate_status(now: Optional[datetime] = None) -> RuntimeOut:
    ov = load_overrides()
    auto = _auto(now)
    mode = auto["mode"]
    if ov.get("is_open_override") == "open": mode = "open"
    elif ov.get("is_open_override") == "closed": mode = "closed"
    delivery_enabled = False if mode == "closed" else bool(ov.get("delivery_enabled") or auto["delivery_window"])
    now_dt = auto["now"]
    return RuntimeOut(
        now=now_dt.isoformat(),
        mode=mode,
        delivery_enabled=delivery_enabled,
        window={"open":"16:00","delivery":"17:00-21:30","close":"22:00"},
        bot_enabled=bool(ov.get("bot_enabled", True)),
        pasta_available=bool(ov.get("pasta_available", True)),
        delay_pasta_minutes=int(ov.get("delay_pasta_minutes", 0)),
        delay_schotels_minutes=int(ov.get("delay_schotels_minutes", 0)),
    )

@app.get("/runtime/status", response_model=RuntimeOut)
def runtime_status():
    return evaluate_status()

@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "time": datetime.now(TZ).isoformat(), "tz": str(TZ)})

# ------------ TTS ------------
@app.get("/tts")
async def tts(text: str):
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY missing")
    url = "https://api.openai.com/v1/audio/speech"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": TTS_MODEL, "voice": TTS_VOICE, "input": text, "format": "mp3"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"TTS error {r.status_code}: {r.text}")
        return Response(content=r.content, media_type="audio/mpeg")

def say_url(text: str) -> str:
    return f"{BASE_URL}/tts?text={quote_plus(text)}"

# ------------ Conversatie state machine ------------
# States: greet -> ask_items -> collecting -> confirm_more -> summarize -> fulfilment -> phone -> crm_confirm -> address -> payment -> eta -> closing
CALL_TTL_SEC = 2 * 3600

def _call_key(call_sid: str) -> str:
    return f"call:{call_sid}"

def _get_call(call_sid: str) -> Dict[str, Any]:
    try:
        r = _redis()
        raw = r.get(_call_key(call_sid))
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {"state": "greet", "items": [], "total": 0.0, "fulfilment": None, "customer": {}, "payment": None}

def _save_call(call_sid: str, data: Dict[str, Any]):
    try:
        r = _redis()
        r.set(_call_key(call_sid), json.dumps(data, ensure_ascii=False), ex=CALL_TTL_SEC)
    except Exception:
        pass

def _fmt_eur(x: float) -> str:
    return f"{x:0.2f}".replace(".", ",")

def _parse_qty_and_name(utt: str) -> List[Dict[str, Any]]:
    txt = re.sub(r"\s+", " ", utt.lower()).strip()
    found = []
    # simpele hoeveelheden: "2 margherita", "drie margherita" (alleen cijfers voor nu)
    m = re.findall(r"(\d+)\s+([a-z0-9äöüëéèïîç\s\-]+)", txt)
    used = set()
    if m:
        for qty_s, tail in m:
            qty = max(1, int(qty_s))
            # match menu item in tail
            best = None; score = 0
            for it in MENU_ITEMS:
                if it["norm"] in tail and it["norm"] not in used:
                    best = it; score = len(it["norm"]); break
                # fallback: substring match any significant token
                tokens = [t for t in it["norm"].split() if len(t) > 3]
                if any(t in tail for t in tokens) and score == 0:
                    best = it
            if best:
                used.add(best["norm"])
                found.append({"code": best["code"], "name": best["name"], "price": best["price"], "qty": qty})
    # enkelvoud zonder hoeveelheid
    if not found:
        for it in MENU_ITEMS:
            if it["norm"] in txt:
                found.append({"code": it["code"], "name": it["name"], "price": it["price"], "qty": 1})
    return found

def _items_to_text(items: List[Dict[str, Any]]) -> str:
    return ", ".join([f'{i["qty"]}× {i["name"]}' for i in items])

def _order_total(items: List[Dict[str, Any]]) -> float:
    return round(sum(i["qty"] * float(i["price"]) for i in items), 2)

def _delivery_fee(postcode: str) -> float:
    if not postcode: return 0.0
    pc = postcode.replace(" ", "").upper()
    for z in DELIVERY_CFG.get("zones", []):
        if any(pc.startswith(p.replace(" ", "").upper()) for p in z.get("postcodes", [])):
            try:
                return float(z.get("fee", 0))
            except Exception:
                return 0.0
    return 0.0

def _eta_minutes(fulfilment: str, delay_pasta: int, delay_schotels: int) -> int:
    base = DELIVERY_CFG.get("sla", {}).get("delivery_minutes" if fulfilment=="delivery" else "pickup_minutes", 30)
    extra = max(delay_pasta, delay_schotels)
    return int(base) + int(extra)

def _greeting_by_status() -> str:
    st = evaluate_status()
    if st.mode == "open":
        now = datetime.now(TZ).time()
        if now < time(12, 0): return PROMPTS["greet_open_morning"]
        elif now < time(18, 0): return PROMPTS["greet_open_afternoon"]
        else: return PROMPTS["greet_open_evening"]
    return PROMPTS["greet_closed"]

def _contains(text: str, *keys: str) -> bool:
    t = text.lower()
    return any(k in t for k in keys)

# ------------ Voice routes ------------
@app.api_route("/voice/incoming", methods=["GET","POST"])
def voice_incoming():
    # start state
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>{say_url(_greeting_by_status())}</Play>
  <Redirect method="POST">{BASE_URL}/voice/step</Redirect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

@app.api_route("/voice/step", methods=["GET","POST"])
def voice_step():
    hints = "bestellen, pizza, schotel, pasta, afhalen, bezorgen, contant, ideal, postcode, huisnummer, telefoonnummer, dat is alles, klaar"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="speech" language="nl-NL" hints="{hints}"
          action="{BASE_URL}/voice/handle" method="POST"
          timeout="8" speechTimeout="auto" bargeIn="true">
    <Play>{say_url(PROMPTS.get("say_prompt","Zegt u maar."))}</Play>
  </Gather>
  <Redirect method="POST">{BASE_URL}/voice/step</Redirect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

@app.post("/voice/handle")
async def voice_handle(request: Request):
    try:
        form = await request.form()
    except Exception:
        form = {}
    call_sid = (form.get("CallSid") or "no-sid").strip()
    speech = (form.get("SpeechResult") or "").strip()
    state = _get_call(call_sid)

    # gesloten → direct closed prompt en stop
    if _greeting_by_status() == PROMPTS["greet_closed"]:
        tw = f"""<Response><Play>{say_url(PROMPTS["greet_closed"])}</Play></Response>"""
        return Response(content=f'<?xml version="1.0" encoding="UTF-8"?>{tw}', media_type="text/xml")

    def say_and_next(msg: str, next_state: str) -> Response:
        state["state"] = next_state; _save_call(call_sid, state)
        tw = f"""<?xml version="1.0" encoding="UTF-8"?><Response><Play>{say_url(msg)}</Play><Redirect method="POST">{BASE_URL}/voice/step</Redirect></Response>"""
        return Response(content=tw, media_type="text/xml")

    # router
    s = state.get("state")

    # 0) Greet -> ask_items
    if s in ("greet", None):
        return say_and_next(PROMPTS["ask_items"], "ask_items")

    # 1) Vraag wat wil je bestellen -> parse items
    if s in ("ask_items", "collecting"):
        items = _parse_qty_and_name(speech)
        if items:
            # voeg toe
            state["items"] += items
            # bevestig laatste bundel
            msg = PROMPTS["item_added"].format(qty=items[-1]["qty"], name=items[-1]["name"])
            state["state"] = "confirm_more"; _save_call(call_sid, state)
            tw = f"""<?xml version="1.0" encoding="UTF-8"?><Response><Play>{say_url(msg)}</Play><Play>{say_url(PROMPTS["confirm_more"])}</Play><Redirect method="POST">{BASE_URL}/voice/step</Redirect></Response>"""
            return Response(content=tw, media_type="text/xml")
        # “dat is alles” zonder items → vraag items opnieuw
        if _contains(speech, "dat is alles", "klaar", "nee", "niets"):
            if state["items"]:
                state["state"] = "summarize"; _save_call(call_sid, state)
                items_text = _items_to_text(state["items"])
                total = _order_total(state["items"])
                state["total"] = total; _save_call(call_sid, state)
                msg = PROMPTS["summarize"].format(items=items_text, total=_fmt_eur(total))
                return say_and_next(msg, "fulfilment")
            else:
                return say_and_next(PROMPTS["ask_items"], "ask_items")
        # geen parse → herhaal vraag
        return say_and_next(PROMPTS["ask_items"], "ask_items")

    # 2) confirm_more -> terug naar ask_items of door
    if s == "confirm_more":
        if _contains(speech, "ja", "nog", "toevoegen", "meer"):
            return say_and_next(PROMPTS["ask_items"], "ask_items")
        if _contains(speech, "nee", "dat is alles", "klaar", "niets"):
            state["state"] = "summarize"; _save_call(call_sid, state)
            items_text = _items_to_text(state["items"]) if state["items"] else "geen items"
            total = _order_total(state["items"])
            state["total"] = total; _save_call(call_sid, state)
            msg = PROMPTS["summarize"].format(items=items_text, total=_fmt_eur(total))
            return say_and_next(msg, "fulfilment")
        # onduidelijk → nogmaals
        return say_and_next(PROMPTS["confirm_more"], "confirm_more")

    # 3) fulfilment: afhalen of bezorgen
    if s == "fulfilment":
        if _contains(speech, "afhaal", "afhalen", "ophalen", "pickup"):
            state["fulfilment"] = {"type": "pickup"}; _save_call(call_sid, state)
            return say_and_next(PROMPTS["ask_payment_pickup"], "payment")
        if _contains(speech, "bezorg", "bezorgen", "thuis"):
            state["fulfilment"] = {"type": "delivery"}; _save_call(call_sid, state)
            return say_and_next(PROMPTS["ask_phone_for_delivery"], "phone")
        # onduidelijk
        return say_and_next(PROMPTS["ask_fulfilment"], "fulfilment")

    # 4) phone: vraag tel, lookup CSV
    if s == "phone":
        tel = "".join(ch for ch in speech if ch.isdigit())
        state.setdefault("customer", {})["tel"] = tel
        # lookup
        found = None
        if os.path.exists(CUSTOMER_CSV) and tel:
            try:
                with open(CUSTOMER_CSV, newline="", encoding="utf-8") as f:
                    r = csv.DictReader(f)
                    for row in r:
                        phones = [
                            ''.join(ch for ch in (row.get("phone") or "") if ch.isdigit()),
                            ''.join(ch for ch in (row.get("mobile") or "") if ch.isdigit())
                        ]
                        if tel in phones:
                            found = {
                                "postcode": row.get("postcode") or "",
                                "straat": row.get("street1") or "",
                                "huisnr": row.get("house_number") or "",
                            }
                            break
            except Exception:
                found = None
        if found and (found["straat"] or found["postcode"]):
            state["customer"].update(found); _save_call(call_sid, state)
            msg = PROMPTS["confirm_lookup_found"].format(straat=found["straat"], huisnr=found["huisnr"], postcode=found["postcode"])
            return say_and_next(msg, "crm_confirm")
        else:
            return say_and_next(PROMPTS["confirm_lookup_missing"], "address")

    # 5) crm_confirm: ja klopt / nee
    if s == "crm_confirm":
        if _contains(speech, "ja", "klopt", "correct"):
            return say_and_next(PROMPTS["ask_payment_delivery"], "payment")
        if _contains(speech, "nee", "klopt niet"):
            return say_and_next(PROMPTS["confirm_lookup_missing"], "address")
        return say_and_next(PROMPTS["confirm_lookup_found"].format(
            straat=state["customer"].get("straat",""),
            huisnr=state["customer"].get("huisnr",""),
            postcode=state["customer"].get("postcode",""),
        ), "crm_confirm")

    # 6) address: verwacht postcode en/of huisnummer in vrije spraak
    if s == "address":
        pc = re.search(r"\b\d{4}\s?[a-zA-Z]{2}\b", speech)
        hn = re.search(r"\b(\d{1,4}[a-zA-Z]?)\b", speech)
        if pc: state["customer"]["postcode"] = pc.group(0).replace(" ","").upper()
        if hn: state["customer"]["huisnr"] = hn.group(1)
        if state["customer"].get("postcode") and state["customer"].get("huisnr"):
            return say_and_next(PROMPTS["ask_payment_delivery"], "payment")
        # nogmaals vragen
        return say_and_next("Kunt u uw postcode en huisnummer herhalen?", "address")

    # 7) payment: contant of ideal
    if s == "payment":
        if _contains(speech, "ideal", "i deal", "link"):
            state["payment"] = "ideal"
        elif _contains(speech, "contant", "cash"):
            state["payment"] = "cash"
        elif _contains(speech, "pin"):
            state["payment"] = "pin"
        else:
            # herhaal vraag passend bij fulfilment
            if state.get("fulfilment",{}).get("type") == "delivery":
                return say_and_next(PROMPTS["ask_payment_delivery"], "payment")
            else:
                return say_and_next(PROMPTS["ask_payment_pickup"], "payment")
        _save_call(call_sid, state)
        return say_and_next("Dank u.", "eta")

    # 8) eta: bereken tijd + eventuele bezorgkosten
    if s == "eta":
        st = evaluate_status()
        fulfil = state.get("fulfilment",{}).get("type") or "pickup"
        total = _order_total(state["items"])
        if fulfil == "delivery":
            fee = _delivery_fee(state.get("customer",{}).get("postcode",""))
            total = round(total + fee, 2)
        # vertragingen
        eta_min = _eta_minutes(fulfil, st.delay_pasta_minutes, st.delay_schotels_minutes)
        ready_at = (datetime.now(TZ) + timedelta(minutes=eta_min)).strftime("%H:%M")
        state["total"] = total; _save_call(call_sid, state)
        msg_eta = PROMPTS["eta_delivery" if fulfil=="delivery" else "eta_pickup"].format(tijd=ready_at)
        msg_sum = f"Totaal bedrag is €{_fmt_eur(total)}."
        tw = f"""<?xml version="1.0" encoding="UTF-8"?><Response>
          <Play>{say_url(msg_eta)}</Play>
          <Play>{say_url(msg_sum)}</Play>
          <Play>{say_url(PROMPTS["closing"])}</Play>
        </Response>"""
        # optioneel: hier could log order to /mnt/data/orders.log
        return Response(content=tw, media_type="text/xml")

    # default fallback
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><Response><Play>{say_url(PROMPTS["fallback1"])}</Play><Redirect method="POST">{BASE_URL}/voice/step</Redirect></Response>', media_type="text/xml")

# ------------ Status callback ------------
@app.post("/voice/status")
async def voice_status(request: Request):
    try:
        data = await request.form()
        payload = {k: data.get(k) for k in data.keys()}
    except Exception:
        payload = {}
    try:
        os.makedirs("/mnt/data", exist_ok=True)
        with open("/mnt/data/twilio_status.log", "a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return PlainTextResponse("ok")

# ------------ CRM API (optioneel debug) ------------
@app.get("/crm/lookup")
def crm_lookup(tel: str = Query(..., min_length=6)):
    path = CUSTOMER_CSV
    tel_norm = ''.join(ch for ch in tel if ch.isdigit())
    if not os.path.exists(path):
        return JSONResponse({"found": False}, status_code=404)
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            phones = [
                ''.join(ch for ch in (row.get("phone") or "") if ch.isdigit()),
                ''.join(ch for ch in (row.get("mobile") or "") if ch.isdigit())
            ]
            if tel_norm and tel_norm in phones:
                return {
                    "found": True, "tel": tel,
                    "postcode": row.get("postcode") or "",
                    "straat": row.get("street1") or "",
                    "huisnummer": row.get("house_number") or ""
                }
    return {"found": False}

# ------------ Orders log (blijft bestaan) ------------
@app.post("/order/submit")
async def order_submit(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    order_id = f"ord_{uuid.uuid4().hex[:12]}"
    payload["order_id"] = order_id
    payload["created_at"] = datetime.now(TZ).isoformat()
    try:
        from redis import Redis
        rds = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
        rds.hset("orders:index", order_id, payload["created_at"])
        rds.set(f"order:{order_id}", json.dumps(payload, ensure_ascii=False), ex=7*24*3600)
    except Exception:
        pass
    try:
        os.makedirs("/mnt/data", exist_ok=True)
        with open("/mnt/data/orders.log", "a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return {"ok": True, "order_id": order_id}

# ------------ Admin toggles ------------
@app.post("/admin/toggles")
async def admin_toggles(request: Request):
    """
    JSON:
    { bot_enabled, pasta_available, delay_pasta_minutes, delay_schotels_minutes,
      is_open_override: "auto"|"open"|"closed", delivery_enabled }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if body.get("is_open_override") not in {"auto","open","closed"}:
        body["is_open_override"] = "auto"
    def _norm(v):
        try: n = int(v)
        except Exception: n = 0
        allowed = [0,10,20,30,45,60]
        return min(allowed, key=lambda a: abs(a-n))
    body["delay_pasta_minutes"] = _norm(body.get("delay_pasta_minutes", 0))
    body["delay_schotels_minutes"] = _norm(body.get("delay_schotels_minutes", 0))
    saved = save_overrides(body)
    return {"ok": True, "saved": saved, "ttl_minutes": OVERRIDES_TTL_MIN}
