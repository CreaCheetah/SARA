# Mada Belbot – OpenAI TTS intro + GPT-4o Realtime dialoog (NL) + Dashboard API

import os, json, base64, audioop, asyncio
from datetime import datetime, time
from pathlib import Path
from typing import Optional, Literal

from fastapi import FastAPI, Response, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from zoneinfo import ZoneInfo
from redis import Redis
from redis.exceptions import RedisError
import httpx
import websockets

# ===== Init =====
app = FastAPI(title="Adams Belbot")
security = HTTPBasic()

# ===== Config =====
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "CHANGE_ME")
TZ = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
OPENAI_REALTIME_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}"
RECORDING_ENABLED = os.getenv("RECORD_CALLS", "false").lower() == "true"

# ===== Auth =====
def auth(creds: HTTPBasicCredentials = Depends(security)) -> bool:
    if creds.username != ADMIN_USER or creds.password != ADMIN_PASS:
        raise HTTPException(status_code=401, detail="Unauthorized",
                            headers={"WWW-Authenticate": "Basic"})
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

# ===== Teksten =====
NAME = "Ristorante Adam Spanbroek"
REC_TXT = " Dit gesprek kan tijdelijk worden opgenomen om onze service te verbeteren." if RECORDING_ENABLED else ""

def select_greeting(now: Optional[datetime] = None) -> str:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    t = now.time()
    st = evaluate_status(now)
    dag = "Goedemiddag" if t < time(18, 0) else "Goedenavond"
    if st.mode == "closed":
        return f"{dag}, u spreekt met Mada, de digitale assistent van {NAME}. We zijn op dit moment gesloten. Onze openingstijden zijn van vier uur ’s middags tot tien uur ’s avonds."
    return f"{dag}, u spreekt met Mada, de digitale assistent van {NAME}. Waarmee kan ik u helpen vandaag?{REC_TXT}"

# ===== API: runtime =====
@app.get("/runtime/status", response_model=RuntimeOut)
def runtime_status():
    return evaluate_status()

# ===== OpenAI TTS MP3 voor begroeting =====
@app.get("/voice/intro.mp3")
async def voice_intro_mp3():
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY missing")
    text = select_greeting()
    url = "https://api.openai.com/v1/audio/speech"
    payload = {"model":"gpt-4o-mini-tts","voice":"alloy","input":text,"format":"mp3"}
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type":"application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        r = await client.post(url, headers=headers, json=payload)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"OpenAI TTS error {r.status_code}")
    return StreamingResponse(iter([r.content]), media_type="audio/mpeg")

# ===== TwiML: Play intro + start stream =====
@app.api_route("/voice/incoming", methods=["GET","POST"])
def voice_incoming():
    host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "mada-3ijw.onrender.com")
    from time import time as _now
    cb = int(_now())
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>https://{host}/voice/intro.mp3?cb={cb}</Play>
  <Connect>
    <Stream url="wss://{host}/twilio/stream"/>
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

# ===== Audio conversie helpers (Twilio μ-law 8kHz <-> PCM16 16kHz) =====
def mulaw_b64_to_pcm16k(b64: str) -> bytes:
    pcm8k = audioop.ulaw2lin(base64.b64decode(b64), 2)
    pcm16, _ = audioop.ratecv(pcm8k, 2, 1, 8000, 16000, None)
    return pcm16

def pcm16k_to_mulaw_b64(pcm16: bytes) -> str:
    pcm8k, _ = audioop.ratecv(pcm16, 2, 1, 16000, 8000, None)
    ulaw = audioop.lin2ulaw(pcm8k, 2)
    return base64.b64encode(ulaw).decode()

# ===== GPT-4o Realtime bridge (fail-safe) =====
async def openai_connect():
    if not OPENAI_API_KEY:
        return None
    headers = [("Authorization", f"Bearer {OPENAI_API_KEY}"), ("OpenAI-Beta", "realtime=v1")]
    try:
        # compat voor oude/nieuwe websockets argumentnaam
        try:
            conn = await websockets.connect(OPENAI_REALTIME_URL, extra_headers=headers, ping_interval=20, ping_timeout=20)
        except TypeError:
            conn = await websockets.connect(OPENAI_REALTIME_URL, additional_headers=headers, ping_interval=20, ping_timeout=20)
        # sessie NL + korte antwoorden
        await conn.send(json.dumps({
            "type":"session.update",
            "session":{
                "voice":"alloy",
                "input_audio_format":{"type":"pcm16","sample_rate":16000},
                "output_audio_format":{"type":"pcm16","sample_rate":16000},
                "instructions":(
                    "Je heet Mada. Spreek alleen Nederlands. Antwoord kort en duidelijk. "
                    "Respecteer deze statusregels: geen bezorging als bezorging uit staat; "
                    "geen bestellingen als gesloten; noem eventuele extra bereidingstijden. "
                    "Bied een alternatief als iets niet kan."
                ),
                "turn_detection":{"type":"server_vad"}
            }
        }))
        return conn
    except Exception:
        return None

@app.websocket("/twilio/stream")
async def twilio_stream(ws: WebSocket):
    await ws.accept()
    oa = await openai_connect()  # mag None zijn, we blijven fail-safe

    async def pump_twilio_to_openai():
        try:
            while True:
                msg = await ws.receive_text()
                evt = json.loads(msg)
                et = evt.get("event")
                if et == "media" and oa:
                    b64 = evt["media"]["payload"]
                    pcm16 = mulaw_b64_to_pcm16k(b64)
                    await oa.send(json.dumps({
                        "type":"input_audio_buffer.append",
                        "audio": base64.b64encode(pcm16).decode()
                    }))
                elif et == "start" and oa:
                    await oa.send(json.dumps({"type":"response.create", "response":{"modalities":["audio"]}}))
                elif et == "stop":
                    break
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    async def pump_openai_to_twilio():
        if not oa:
            return
        try:
            async for raw in oa:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                t = data.get("type")
                if t in ("response.audio.delta","output_audio.delta"):
                    pcm16_b64 = data.get("delta") or data.get("audio")
                    if pcm16_b64:
                        ulaw_b64 = pcm16k_to_mulaw_b64(base64.b64decode(pcm16_b64))
                        await ws.send_text(json.dumps({"event":"media","media":{"payload":ulaw_b64}}))
                elif t in ("response.completed","response.stop"):
                    await ws.send_text(json.dumps({"event":"mark","mark":{"name":"eor"}}))
        except Exception:
            pass

    await asyncio.wait([
        asyncio.create_task(pump_twilio_to_openai()),
        asyncio.create_task(pump_openai_to_twilio()),
    ], return_when=asyncio.FIRST_COMPLETED)

    try:
        await ws.close()
    except Exception:
        pass
    try:
        if oa:
            await oa.close()
    except Exception:
        pass

# ===== Admin API =====
@app.post("/admin/toggles", dependencies=[Depends(auth)], response_model=RuntimeOut)
def set_toggles(body: TogglesIn):
    valid = {0, 10, 20, 30, 45, 60}
    if body.delay_pasta_minutes not in valid or body.delay_schotels_minutes not in valid:
        raise HTTPException(status_code=400, detail="Delay must be one of 0,10,20,30,45,60")
    _save_overrides(body)
    return evaluate_status()

# ===== Admin UI (Basic Auth) =====
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

@app.get("/admin/logout")
def admin_logout():
    raise HTTPException(status_code=401, detail="Logged out",
                        headers={"WWW-Authenticate":"Basic"})

# ===== Health =====
@app.get("/healthz")
def healthz():
    ok = True
    try:
        r.ping()
    except RedisError:
        ok = False
    return JSONResponse({"ok": ok, "redis": ok})
