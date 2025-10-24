# Mada Belbot - Twilio Voice webhook + Dashboard API + OpenAI TTS
import os
import json
import hashlib
import base64
from datetime import datetime, time
from pathlib import Path
from typing import Optional, Literal
from urllib.parse import quote

import requests
from fastapi import FastAPI, Response, Depends, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError
from zoneinfo import ZoneInfo
from redis import Redis
from redis.exceptions import RedisError

# ===== Init =====
app = FastAPI(title="Mada AI Assistent")
security = HTTPBasic()

# ===== Config & Auth =====
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "CHANGE_ME")
TZ = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("TTS_VOICE", "aria")  # vriendelijke NL stem
TTS_FORMAT = "mp3"

def auth(creds: HTTPBasicCredentials = Depends(security)) -> bool:
    if creds.username != ADMIN_USER or creds.password != ADMIN_PASS:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
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
    ttl_minutes: int = Field(default=180, ge=1, le=720)  # max 12 uur

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

# ===== Teksten / begroeting =====
NAME = "Ristorante Adam Spanbroek"

def select_greeting(now: Optional[datetime] = None) -> str:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    t = now.time()
    st = evaluate_status(now)

    if st.mode == "closed":
        dagdeel = "Goedemiddag" if t < time(18, 0) else "Goedenavond"
        return (
            f"{dagdeel}, u spreekt met Mada, de digitale assistent van {NAME}. "
            "We zijn op dit moment gesloten. Vanaf vier uur ’s middags zijn we weer bereikbaar."
        )

    dagdeel = "Goedemiddag" if t < time(18, 0) else "Goedenavond"
    return f"{dagdeel}, u spreekt met Mada, de digitale assistent van {NAME}. Waarmee kan ik u helpen?"

# ===== Runtime status =====
@app.get("/runtime/status", response_model=RuntimeOut)
def runtime_status():
    return evaluate_status()

# ===== OpenAI TTS helper =====
def _tts_cache_key(text: str) -> str:
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()
    return f"tts:{TTS_MODEL}:{TTS_VOICE}:{h}"

def synthesize_tts_mp3(text: str) -> bytes:
    """Synthesize speech via OpenAI TTS and return MP3 bytes. Uses Redis cache."""
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY missing")

    key = _tts_cache_key(text)
    cached_b64 = r.get(key)
    if cached_b64:
        try:
            return base64.b64decode(cached_b64)
        except Exception:
            pass  # jat het opnieuw

    url = "https://api.openai.com/v1/audio/speech"
    payload = {
        "model": TTS_MODEL,
        "voice": TTS_VOICE,
        "input": text,
        "format": TTS_FORMAT,
        # NL uitspraak hint:
        "language": "nl-NL"
    }
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"TTS error {resp.status_code}: {resp.text[:200]}")
    audio_bytes = resp.content
    # cache ~ 12 uur
    try:
        r.setex(key, 12 * 3600, base64.b64encode(audio_bytes).decode("ascii"))
    except RedisError:
        pass
    return audio_bytes

@app.get("/tts")
def tts_endpoint(text: str):
    """Return MP3 for given text. Used by Twilio <Play>."""
    audio = synthesize_tts_mp3(text)
    return Response(content=audio, media_type="audio/mpeg")

# ===== Twilio Voice webhook =====
@app.api_route("/voice/incoming", methods=["GET", "POST"])
def voice_incoming(request: Request):
    text = select_greeting()
    # Twilio speelt MP3 af vanaf onze /tts endpoint
    host = os.getenv("RENDER_EXTERNAL_HOSTNAME", request.headers.get("host", "localhost"))
    base_url = f"https://{host}"
    tts_url = f"{base_url}/tts?text={quote(text)}"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>{tts_url}</Play>
  <!-- Stilte zodat de lijn open blijft voor het vervolg in de huidige fase -->
  <Pause length="2"/>
  <Say language="nl-NL">Een ogenblik alstublieft.</Say>
</Response>"""
    return Response(content=twiml.strip(), media_type="text/xml")

# (WebSocket placeholder; wordt niet gebruikt in deze TTS-only fase)
@app.websocket("/twilio/stream")
async def twilio_stream(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.receive_text()
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
    raise HTTPException(
        status_code=401,
        detail="Logged out",
        headers={"WWW-Authenticate": "Basic"},
    )

# ===== Health =====
@app.get("/healthz")
def healthz():
    ok = True
    try:
        r.ping()
    except RedisError:
        ok = False
    return JSONResponse({"ok": ok, "redis": ok})
