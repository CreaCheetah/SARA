# Mada Belbot - Twilio Voice webhook + Dashboard API + OpenAI TTS

import os
import json
from datetime import datetime, time
from pathlib import Path
from typing import Optional, Literal

from fastapi import FastAPI, Response, Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError
from zoneinfo import ZoneInfo
from redis import Redis
from redis.exceptions import RedisError
import httpx

# ===== Init =====
app = FastAPI(title="Adams Belbot")
security = HTTPBasic()

# ===== Config & Auth =====
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "CHANGE_ME")
TZ = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "marin")  # voorbeelden: alloy, echo, nova, shimmer, coral, fable, onyx, verse, ballad, ash, sage, marin, cedar
RECORD_CALLS = os.getenv("RECORD_CALLS", "false").lower() == "true"

def auth(creds: HTTPBasicCredentials = Depends(security)) -> bool:
    if creds.username != ADMIN_USER or creds.password != ADMIN_PASS:
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return True

# ===== Redis =====
r = Redis.from_url(REDIS_URL, decode_responses=True)
KEY_OVERRIDES = "belbot:overrides"

# ===== Openingstijden =====
OPEN_START, OPEN_END = time(16, 0), time(22, 0)
DELIVERY_START, DELIVERY_END = time(17, 0), time(21, 30)

# ===== Modellen =====
class TogglesIn(BaseModel):
    bot_enabled: bool = True
    kitchen_closed: bool = False
    pasta_available: bool = True
    delay_pasta_minutes: int = Field(default=0, ge=0)
    delay_schotels_minutes: int = Field(default=0, ge=0)
    is_open_override: Literal["auto", "open", "closed"] = "auto"
    delivery_enabled: Optional[bool] = None
    pickup_enabled: Optional[bool] = None
    ttl_minutes: int = Field(default=180, ge=1, le=720)

class RuntimeOut(BaseModel):
    now: str
    mode: Literal["open", "closed"]
    delivery_enabled: bool
    pickup_enabled: bool
    close_reason: Optional[str] = None
    kitchen_closed: bool = False
    bot_enabled: bool = True
    pasta_available: bool
    delay_pasta_minutes: int
    delay_schotels_minutes: int
    window: dict

# ===== Helpers =====
def _auto(now: datetime):
    t = now.time()
    open_now = OPEN_START <= t < OPEN_END
    delivery_auto = DELIVERY_START <= t < DELIVERY_END
    pickup_auto = OPEN_START <= t < OPEN_END
    return open_now, delivery_auto, pickup_auto

def _load_overrides() -> Optional[TogglesIn]:
    try:
        raw = r.get(KEY_OVERRIDES)
        if not raw:
            return None
        return TogglesIn(**json.loads(raw))
    except (RedisError, json.JSONDecodeError, ValidationError):
        return None

def _save_overrides(body: TogglesIn):
    ttl = int(body.ttl_minutes) * 60
    try:
        r.set(KEY_OVERRIDES, body.model_dump_json(), ex=ttl)
    except RedisError:
        raise HTTPException(status_code=503, detail="Cache unavailable")

def evaluate_status(now: Optional[datetime] = None) -> RuntimeOut:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    over = _load_overrides()
    open_auto, delivery_auto, pickup_auto = _auto(now)

    if over and over.is_open_override == "closed":
        open_now = False
    elif over and over.is_open_override == "open":
        open_now = True
    else:
        open_now = open_auto

    if over and over.kitchen_closed:
        return RuntimeOut(
            now=now.isoformat(), mode="closed",
            delivery_enabled=False, pickup_enabled=False,
            close_reason=None, kitchen_closed=True,
            bot_enabled=(over.bot_enabled if over else True),
            pasta_available=(over.pasta_available if over else True),
            delay_pasta_minutes=(over.delay_pasta_minutes if over else 0),
            delay_schotels_minutes=(over.delay_schotels_minutes if over else 0),
            window={"open": "16:00", "delivery": "17:00-21:30", "close": "22:00"},
        )

    if not open_now:
        return RuntimeOut(
            now=now.isoformat(), mode="closed",
            delivery_enabled=False, pickup_enabled=False,
            close_reason="We zijn op dit moment gesloten.",
            kitchen_closed=False,
            bot_enabled=(over.bot_enabled if over else True),
            pasta_available=(over.pasta_available if over else True),
            delay_pasta_minutes=(over.delay_pasta_minutes if over else 0),
            delay_schotels_minutes=(over.delay_schotels_minutes if over else 0),
            window={"open": "16:00", "delivery": "17:00-21:30", "close": "22:00"},
        )

    delivery = delivery_auto
    pickup = pickup_auto
    if over:
        if over.delivery_enabled is not None:
            delivery = delivery and over.delivery_enabled
        if over.pickup_enabled is not None:
            pickup = pickup and over.pickup_enabled

    return RuntimeOut(
        now=now.isoformat(), mode="open",
        delivery_enabled=delivery, pickup_enabled=pickup,
        close_reason=None, kitchen_closed=False,
        bot_enabled=(over.bot_enabled if over else True),
        pasta_available=(over.pasta_available if over else True),
        delay_pasta_minutes=(over.delay_pasta_minutes if over else 0),
        delay_schotels_minutes=(over.delay_schotels_minutes if over else 0),
        window={"open": "16:00", "delivery": "17:00-21:30", "close": "22:00"},
    )

# ===== Taal/teksten =====
NAME = "Ristorante Adam Spanbroek"
def greeting_text() -> str:
    t = datetime.now(TZ).time()
    dagdeel = "Goedemiddag" if t < time(18, 0) else "Goedenavond"
    return f"{dagdeel}, u spreekt met Mada, de digitale assistent van {NAME}. Waarmee kan ik u helpen?"

# ===== OpenAI TTS endpoint =====
@app.get("/tts")
async def tts(text: str):
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="No OPENAI_API_KEY")
    payload = {
        "model": OPENAI_TTS_MODEL,
        "voice": OPENAI_TTS_VOICE,
        "input": text
    }
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post("https://api.openai.com/v1/audio/speech", headers=headers, json=payload)
        if resp.status_code != 200:
            raise HTTPException(status_code=400, detail=f"TTS error {resp.status_code}: {resp.text}")
        return StreamingResponse(iter([resp.content]), media_type="audio/mpeg")

def _gather_twiml(prompt_text: str) -> str:
    # Twilio speelt audio via onze /tts en luistert direct mee (speech).
    rec_attr = ' recording="true"' if RECORD_CALLS else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response{rec_attr}>
  <Gather input="speech" language="nl-NL" action="/voice/step" method="POST" timeout="5" speechTimeout="auto">
    <Play>/tts?text={httpx.QueryParams({'text': prompt_text}).get('text')}</Play>
  </Gather>
</Response>"""

# ===== Routes: runtime & voice =====
@app.get("/runtime/status", response_model=RuntimeOut)
def runtime_status():
    return evaluate_status()

@app.api_route("/voice/incoming", methods=["GET", "POST"])
def voice_incoming():
    # Startprompt
    twiml = _gather_twiml(greeting_text())
    return Response(content=twiml, media_type="text/xml")

# ===== Eenvoudige order-flow =====
NUMWORDS = {
    "een":1,"één":1,"twee":2,"drie":3,"vier":4,"vijf":5,"zes":6,"zeven":7,"acht":8,"negen":9,"tien":10,
    "1":1,"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,"10":10
}
MENU_KEYWORDS = {
    "margherita":"pizza margherita",
    "salami":"pizza salami",
    "hawai":"pizza hawai",
    "hawaii":"pizza hawai",
    "bolognese":"pasta bolognese",
    "quattro formaggi":"pasta quattro formaggi",
    "kapsalon":"kapsalon",
    "shoarma":"shoarma"
}
def parse_order(utter: str):
    u = f" {utter.lower()} "
    qty = 1
    for w, n in NUMWORDS.items():
        if f" {w} " in u:
            qty = n
            break
    items = []
    for kw, label in MENU_KEYWORDS.items():
        if kw in u:
            items.append({"item": label, "qty": qty})
    return items

@app.api_route("/voice/step", methods=["POST"])
async def voice_step(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid") or "unknown"
    said = (form.get("SpeechResult") or "").strip()
    key = f"belbot:call:{call_sid}"
    state = json.loads(r.get(key) or "{}") if r else {}
    stage = state.get("stage", "ordering")
    order = state.get("order", [])

    def reply(prompt_text: str) -> Response:
        return Response(content=_gather_twiml(prompt_text), media_type="text/xml")

    # 1) ordering
    if stage == "ordering":
        if said:
            # stopwoorden voor "niets/nee"
            if said.lower() in ("nee","nee dank u","nee dankjewel","dat is alles","dat was het","niks meer","niets meer"):
                if not order:
                    return reply("Ik heb nog geen bestelling verstaan. Zegt u bijvoorbeeld: twee pizza salami.")
                state["stage"] = "mode"
                r.setex(key, 900, json.dumps(state))
                return reply("Wordt de bestelling bezorgd of komt u afhalen?")
            got = parse_order(said)
            if got:
                order.extend(got)
                state["order"] = order
                r.setex(key, 900, json.dumps(state))
                lijst = ", ".join(f"{x['qty']} keer {x['item']}" for x in order)
                return reply(f"Ik heb genoteerd: {lijst}. Wilt u nog iets erbij?")
        r.setex(key, 900, json.dumps(state))
        return reply("Noem alstublieft het gerecht en het aantal. Bijvoorbeeld: één pizza margherita.")

    # 2) bezorging of afhalen
    if stage == "mode":
        u = said.lower()
        if "bezorg" in u:
            state["mode"] = "bezorgen"
        elif "afhaal" in u or "halen" in u:
            state["mode"] = "afhalen"
        else:
            r.setex(key, 900, json.dumps(state))
            return reply("Begrijp ik goed: bezorgen of afhalen?")
        state["stage"] = "phone"
        r.setex(key, 900, json.dumps(state))
        return reply("Welk telefoonnummer kan ik gebruiken voor de bevestiging?")

    # 3) telefoon en afronden
    if stage == "phone":
        digits = "".join(ch for ch in said if ch.isdigit())
        if len(digits) < 8:
            return reply("Ik hoorde geen geldig telefoonnummer. Zegt u alstublieft het telefoonnummer langzaam.")
        state["phone"] = digits
        state["stage"] = "done"
        r.setex(key, 900, json.dumps(state))
        lijst = ", ".join(f"{x['qty']} keer {x['item']}" for x in order)
        modus = state.get("mode","onbekend")
        return reply(f"Dank u. Samenvatting: {lijst}. {modus}. Telefoon {digits}. Dat was alles voor nu. Een fijne dag.")

    # fallback
    r.setex(key, 900, json.dumps(state))
    return reply("Kunt u dat herhalen?")

# ===== Twilio Media Stream WebSocket (placeholder) =====
@app.websocket("/twilio/stream")
async def twilio_stream(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            _ = await ws.receive_text()
    except WebSocketDisconnect:
        pass

# ===== Admin API (beschermd) =====
@app.post("/admin/toggles", dependencies=[Depends(auth)], response_model=RuntimeOut)
def set_toggles(body: TogglesIn):
    valid = {0, 10, 20, 30, 45, 60}
    if body.delay_pasta_minutes not in valid or body.delay_schotels_minutes not in valid:
        raise HTTPException(status_code=400, detail="Delay must be one of 0,10,20,30,45,60")
    _save_overrides(body)
    return evaluate_status()

# ===== Auth-protected Static Admin UI =====
BASE_DIR = Path(__file__).resolve().parent
ADMIN_UI_DIR = BASE_DIR / "admin_ui"

@app.get("/admin/ui/{path:path}", dependencies=[Depends(auth)])
def admin_ui_any(path: str):
    target = ADMIN_UI_DIR / (path or "index.html")
    if target.is_dir():
        target = target / "index.html"
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(target)

# ===== Logout =====
@app.get("/admin/logout")
def admin_logout():
    raise HTTPException(status_code=401, detail="Logged out", headers={"WWW-Authenticate": "Basic"})

# ===== Health =====
@app.get("/healthz")
def healthz():
    ok = True
    try:
        r.ping()
    except RedisError:
        ok = False
    return JSONResponse({"ok": ok, "redis": ok})
