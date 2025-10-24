# Mada Belbot – Twilio ↔ OpenAI Realtime (NL) + Dashboard API

import os, json, base64, audioop, asyncio
from datetime import datetime, time
from pathlib import Path
from typing import Optional, Literal

from fastapi import FastAPI, Response, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError
from zoneinfo import ZoneInfo
from redis import Redis
from redis.exceptions import RedisError
import websockets

app = FastAPI(title="Adams Belbot")
security = HTTPBasic()

# ---------- Config ----------
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "CHANGE_ME")
TZ = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
OPENAI_REALTIME_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}"

def auth(creds: HTTPBasicCredentials = Depends(security)) -> bool:
    if creds.username != ADMIN_USER or creds.password != ADMIN_PASS:
        raise HTTPException(status_code=401, detail="Unauthorized",
                            headers={"WWW-Authenticate": "Basic"})
    return True

# ---------- Redis ----------
r = Redis.from_url(REDIS_URL, decode_responses=True)
KEY_OVERRIDES = "belbot:overrides"

# ---------- Openingstijden ----------
OPEN_START, OPEN_END = time(16, 0), time(22, 0)
DELIVERY_START, DELIVERY_END = time(17, 0), time(21, 30)

# ---------- Models ----------
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

# ---------- Helpers ----------
def _auto(now: datetime):
    t = now.time()
    open_now = OPEN_START <= t < OPEN_END
    delivery_auto = DELIVERY_START <= t < DELIVERY_END
    pickup_auto = OPEN_START <= t < OPEN_END
    return open_now, delivery_auto, pickup_auto

def _load_overrides() -> Optional[TogglesIn]:
    try:
        raw = r.get(KEY_OVERRIDES)
        if not raw: return None
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

# ---------- Teksten ----------
NAME = "Ristorante Adam Spanbroek"
OPENING_GREETING = f"Goedemiddag, u spreekt met Mada, de digitale assistent van {NAME}. Waarmee kan ik u helpen vandaag?"
OPENING_GREETING_EVE = f"Goedenavond, u spreekt met Mada, de digitale assistent van {NAME}. Waarmee kan ik u helpen vandaag?"

def select_greeting(now: Optional[datetime] = None) -> str:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    t = now.time()
    st = evaluate_status(now)
    if st.mode == "closed":
        if t < time(18, 0):
            return f"Goedemiddag, u spreekt met Mada, de digitale assistent van {NAME}. We zijn op dit moment gesloten. Vanaf vier uur zijn wij weer bereikbaar."
        else:
            return f"Goedenavond, u spreekt met Mada, de digitale assistent van {NAME}. We zijn op dit moment gesloten. Vanaf vier uur zijn wij weer bereikbaar."
    return OPENING_GREETING if t < time(18, 0) else OPENING_GREETING_EVE

# ---------- Routes ----------
@app.get("/runtime/status", response_model=RuntimeOut)
def runtime_status():
    return evaluate_status()

@app.api_route("/voice/incoming", methods=["GET", "POST"])
def voice_incoming():
    text = select_greeting()
    hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "mada-3ijw.onrender.com")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="nl-NL">{text}</Say>
  <Connect>
    <Stream url="wss://{hostname}/twilio/stream"/>
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

# ---------- Twilio Media Stream ↔ OpenAI Realtime ----------
# Twilio audio: base64 PCMU (μ-law) 8kHz mono
# OpenAI Realtime audio out: PCM16; wij resamplen terug naar μ-law 8kHz.
def mulaw_b64_to_pcm16k(b64: str) -> bytes:
    # μ-law 8k → PCM16 8k
    pcm8k = audioop.ulaw2lin(base64.b64decode(b64), 2)
    # 8k → 16k resample
    converted, _ = audioop.ratecv(pcm8k, 2, 1, 8000, 16000, None)
    return converted

def pcm16k_to_mulaw_b64(pcm16: bytes) -> str:
    # 16k → 8k
    pcm8k, _ = audioop.ratecv(pcm16, 2, 1, 16000, 8000, None)
    ulaw = audioop.lin2ulaw(pcm8k, 2)
    return base64.b64encode(ulaw).decode()

@app.websocket("/twilio/stream")
async def twilio_stream(ws: WebSocket):
    await ws.accept()
    # OpenAI Realtime WS
    oa_headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    async with websockets.connect(OPENAI_REALTIME_URL, extra_headers=oa_headers) as oa:
        # Init NL sessie + vriendelijke vrouwelijke stem
        session = {
            "type": "session.update",
            "session": {
                "voice": "alloy",               # vriendelijke female
                "input_audio_format": {"type": "pcm16", "sample_rate": 16000},
                "output_audio_format": {"type": "pcm16", "sample_rate": 16000},
                "instructions": (
                    "Je bent 'Mada', een vriendelijke Nederlandse restaurant-assistent. "
                    "Spreek en begrijp uitsluitend Nederlands. Antwoord kort en duidelijk."
                ),
                "turn_detection": {"type": "server_vad"}
            },
        }
        await oa.send(json.dumps(session))

        async def pump_twilio_to_openai():
            try:
                while True:
                    msg = await ws.receive_text()
                    data = json.loads(msg)
                    et = data.get("event")
                    if et == "media":
                        b64 = data["media"]["payload"]
                        pcm16 = mulaw_b64_to_pcm16k(b64)
                        await oa.send(json.dumps({"type": "input_audio_buffer.append",
                                                  "audio": base64.b64encode(pcm16).decode()}))
                    elif et == "start":
                        # start nieuwe response
                        await oa.send(json.dumps({"type": "response.create", "response": {"modalities": ["audio"], "instructions": ""}}))
                    elif et == "stop":
                        break
            except WebSocketDisconnect:
                pass

        async def pump_openai_to_twilio():
            try:
                async for raw in oa:
                    evt = json.loads(raw)
                    # audio fragment
                    if evt.get("type") in ("response.audio.delta", "output_audio.delta"):
                        pcm16_b64 = evt.get("delta") or evt.get("audio")
                        if pcm16_b64:
                            ulaw_b64 = pcm16k_to_mulaw_b64(base64.b64decode(pcm16_b64))
                            await ws.send_text(json.dumps({
                                "event": "media",
                                "media": {"payload": ulaw_b64}
                            }))
                    # einde response signaal
                    if evt.get("type") in ("response.completed", "response.stop"):
                        await ws.send_text(json.dumps({"event": "mark", "mark": {"name": "end"}}))
            except Exception:
                pass

        await asyncio.gather(pump_twilio_to_openai(), pump_openai_to_twilio())

# ---------- Admin API ----------
@app.post("/admin/toggles", dependencies=[Depends(auth)], response_model=RuntimeOut)
def set_toggles(body: TogglesIn):
    valid = {0, 10, 20, 30, 45, 60}
    if body.delay_pasta_minutes not in valid or body.delay_schotels_minutes not in valid:
        raise HTTPException(status_code=400, detail="Delay must be one of 0,10,20,30,45,60")
    _save_overrides(body)
    return evaluate_status()

# ---------- Auth-protected Admin UI ----------
BASE_DIR = Path(__file__).resolve().parent
ADMIN_UI_DIR = BASE_DIR / "admin_ui"

@app.get("/admin/ui/{path:path}", dependencies=[Depends(auth)])
def admin_ui_any(path: str):
    target = ADMIN_UI_DIR / (path or "index.html")
    if target.is_dir(): target = target / "index.html"
    if not target.exists(): raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(target)

@app.get("/admin/logout")
def admin_logout():
    raise HTTPException(status_code=401, detail="Logged out",
                        headers={"WWW-Authenticate": "Basic"})

# ---------- Health ----------
@app.get("/healthz")
def healthz():
    ok = True
    try:
        r.ping()
    except RedisError:
        ok = False
    return JSONResponse({"ok": ok, "redis": ok})
