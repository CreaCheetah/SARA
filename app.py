import os, json, csv, uuid
from datetime import datetime, time
from typing import Optional, Literal, Dict, Any
from urllib.parse import quote_plus
from pathlib import Path

import httpx
from fastapi import FastAPI, Response, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from zoneinfo import ZoneInfo

# ---------- App ----------
app = FastAPI(title="Mada Belassistent")

# ---------- Config ----------
TZ = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))
_host = os.getenv("PUBLIC_BASE_URL", os.getenv("RENDER_EXTERNAL_HOSTNAME", "mada-3ijw.onrender.com"))
BASE_URL = _host if str(_host).startswith("http") else f"https://{_host}"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TTS_MODEL = os.getenv("ENFTTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("ENFTTS_VOICE", "marin")
RECORD_CALLS = os.getenv("RECORD_CALLS", "false").lower() == "true"

REPO_ROOT = Path(__file__).resolve().parent
CONFIG_DELIVERY_PATH = Path(os.getenv("CONFIG_DELIVERY", REPO_ROOT / "config_delivery.json"))
PROMPTS_PATH = Path(os.getenv("PROMPTS_PATH", REPO_ROOT / "prompts_order_nl.json"))
CUSTOMER_CSV = os.getenv("CUSTOMER_CSV", "/mnt/data/klanten.csv")  # NIET in GitHub

# ---------- Openingstijden ----------
OPEN_START, OPEN_END = time(16, 0), time(22, 0)
DEL_START,  DEL_END  = time(17, 0), time(21, 30)

# ---------- Helpers: load config/prompts ----------
def _load_json(path: Path, fallback: Dict[str, Any]) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback

PROMPTS = _load_json(
    PROMPTS_PATH,
    {
        "greet_open_morning": "Goedemorgen, u spreekt met Mada, de digitale assistent van Ristorante Adam Spanbroek. Waarmee kan ik u helpen?",
        "greet_open_afternoon": "Goedemiddag, u spreekt met Mada, de digitale assistent van Ristorante Adam Spanbroek. Waarmee kan ik u helpen?",
        "greet_open_evening": "Goedenavond, u spreekt met Mada, de digitale assistent van Ristorante Adam Spanbroek. Waarmee kan ik u helpen?",
        "greet_closed": "We zijn op dit moment gesloten. U kunt ons weer bereiken vanaf vier uur in de middag.",
        "ask_flow_start": "Wilt u bezorgen of afhalen?",
        "ask_phone": "Welk telefoonnummer kan ik gebruiken om uw adres te controleren?",
        "confirm_phone": "Ik heb {tel}. Klopt dat?",
        "confirm_lookup_found": "Ik heb {straat} {huisnr} in {postcode}. Klopt dat?",
        "confirm_lookup_missing": "Ik heb nog geen adres. Wat is uw postcode?",
        "ask_house_number": "En het huisnummer alstublieft?",
        "ask_street_name": "Dank u. En de straatnaam?",
        "ask_items": "Wat mag ik voor u noteren?",
        "ask_more": "Wilt u nog iets toevoegen of is dit alles?",
        "summary": "Samengevat: {items}. {fulfilment} om {tijd}. Totaal {bedrag}.",
        "ask_payment_delivery": "Betaalt u contant of wilt u een iDEAL-link?",
        "ask_payment_pickup": "Betaalt u bij afhalen met contant of pin, of wilt u een iDEAL-link?",
        "confirm_payment": "Genoteerd: {betaling}.",
        "closing": "Dank u wel. Uw bestelling staat in. Fijne dag.",
        "fallback1": "Ik heb u niet goed verstaan. Kunt u het herhalen?",
        "fallback2_transfer": "Nog één moment. Ik verbind u door met een collega.",
        "say_prompt": "Zegt u maar."
    },
)

DELIVERY_CFG = _load_json(
    CONFIG_DELIVERY_PATH,
    {
        "zones": [],
        "sla": {"pickup_minutes": 15, "pickup_combo_minutes": 30, "delivery_minutes": 60},
    },
)

# ---------- Models ----------
class RuntimeOut(BaseModel):
    now: str
    mode: Literal["open","closed"]
    delivery_enabled: bool
    window: dict

# ---------- Status ----------
def evaluate_status(now: Optional[datetime] = None) -> RuntimeOut:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    t = now.time()
    open_now = OPEN_START <= t < OPEN_END
    delivery = open_now and (DEL_START <= t < DEL_END)
    return RuntimeOut(
        now=now.isoformat(),
        mode="open" if open_now else "closed",
        delivery_enabled=delivery,
        window={"open":"16:00","delivery":"17:00-21:30","close":"22:00"},
    )

@app.get("/runtime/status", response_model=RuntimeOut)
def runtime_status():
    return evaluate_status()

@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "time": datetime.now(TZ).isoformat(), "tz": str(TZ)})

# ---------- TTS (OpenAI) ----------
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

def greeting_text() -> str:
    now = datetime.now(TZ).time()
    if OPEN_START <= now < OPEN_END:
        if now < time(12, 0):
            return PROMPTS["greet_open_morning"]
        elif now < time(18, 0):
            return PROMPTS["greet_open_afternoon"]
        else:
            return PROMPTS["greet_open_evening"]
    else:
        return PROMPTS["greet_closed"]

# ---------- Twilio Voice (semi-realtime Gather) ----------
@app.api_route("/voice/incoming", methods=["GET","POST"])
def voice_incoming():
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>{say_url(greeting_text())}</Play>
  <Redirect method="POST">{BASE_URL}/voice/step</Redirect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

@app.api_route("/voice/step", methods=["GET","POST"])
def voice_step():
    hints = "bestellen, afhalen, bezorgen, pizza, schotel, pasta, postcode, huisnummer, telefoonnummer, klaar, dat is alles, stop"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="speech" language="nl-NL" hints="{hints}"
          action="{BASE_URL}/voice/handle" method="POST"
          timeout="10" speechTimeout="auto" bargeIn="true">
    <Play>{say_url(PROMPTS.get("say_prompt","Zegt u maar."))}</Play>
  </Gather>
  <Redirect method="POST">{BASE_URL}/voice/step</Redirect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

def _contains(text: str, *keys: str) -> bool:
    t = text.lower()
    return any(k in t for k in keys)

@app.post("/voice/handle")
async def voice_handle(request: Request):
    # Robuuste form-parse
    try:
        form = await request.form()
        speech = (form.get("SpeechResult") or "").strip()
    except Exception:
        speech = ""

    if not speech:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>{say_url(PROMPTS["fallback1"])}</Play>
  <Redirect method="POST">{BASE_URL}/voice/step</Redirect>
</Response>"""
        return Response(content=twiml, media_type="text/xml")

    # Basis routering
    if _contains(speech, "bezorg", "bezorgen"):
        msg = PROMPTS["ask_phone"]  # daarna kun je /crm/lookup gebruiken
    elif _contains(speech, "afhaal", "afhalen", "ophalen"):
        msg = PROMPTS["ask_items"]
    elif _contains(speech, "telefoon", "nummer"):
        msg = PROMPTS["confirm_phone"].format(tel=speech)
    elif _contains(speech, "postcode"):
        msg = PROMPTS["ask_house_number"]
    elif _contains(speech, "huisnummer"):
        msg = PROMPTS["ask_street_name"]
    elif _contains(speech, "klaar", "dat is alles", "niets meer"):
        msg = "Dank u. Ik vat zo samen."
    else:
        msg = PROMPTS["ask_more"]

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>{say_url(msg)}</Play>
  <Redirect method="POST">{BASE_URL}/voice/step</Redirect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

# ---------- CRM lookup (CSV op Render disk) ----------
@app.get("/crm/lookup")
def crm_lookup(tel: str = Query(..., min_length=6)):
    """CSV kolommen: fname,iname,phone,mobile,postcode,street1,house_number"""
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
                    "found": True,
                    "tel": tel,
                    "alt_tel": row.get("mobile") or None,
                    "voornaam": row.get("fname") or "",
                    "achternaam": row.get("iname") or "",
                    "postcode": row.get("postcode") or "",
                    "straat": row.get("street1") or "",
                    "huisnummer": row.get("house_number") or ""
                }
    return {"found": False}

# ---------- Order opslaan (Redis + filelog) ----------
@app.post("/order/submit")
async def order_submit(request: Request):
    """Verwacht: {items:[], total:..., klant:{naam,tel,adres}, fulfilment:{type,tijd}, betaalwijze, opmerkingen}"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    order_id = f"ord_{uuid.uuid4().hex[:12]}"
    payload["order_id"] = order_id
    payload["created_at"] = datetime.now(TZ).isoformat()

    # Redis (best effort)
    try:
        from redis import Redis
        rds = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
        rds.hset("orders:index", order_id, payload["created_at"])
        rds.set(f"order:{order_id}", json.dumps(payload, ensure_ascii=False), ex=7*24*3600)
    except Exception:
        pass

    # File log (durable op Render)
    try:
        os.makedirs("/mnt/data", exist_ok=True)
        with open("/mnt/data/orders.log", "a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass

    return {"ok": True, "order_id": order_id}
