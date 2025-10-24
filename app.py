# Mada Belbot - Twilio Voice webhook + Dashboard API + Realtime bridge
import os, json, base64, asyncio, audioop, logging
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

# ===== Init =====
app = FastAPI(title="Mada AI Assistent")
security = HTTPBasic()
logging.basicConfig(level=logging.INFO)

# ===== Config & Auth =====
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "CHANGE_ME")
TZ = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview")

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

# ===== Teksten =====
NAME = "Ristorante Adam Spanbroek"
REC_NOTICE = "Dit gesprek kan tijdelijk worden opgenomen om onze service te verbeteren."
G_DAY = f"Goedemiddag, u spreekt met Mada, de digitale assistent van {NAME}."
G_EVE = f"Goedenavond, u spreekt met Mada, de digitale assistent van {NAME}."

def select_greeting(now: Optional[datetime] = None) -> str:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    t = now.time()
    st = evaluate_status(now)
    if st.mode == "closed":
        if t < time(18, 0):
            return f"{G_DAY} We zijn op dit moment gesloten. Onze openingstijden zijn van vier uur ’s middags tot tien uur ’s avonds."
        else:
            return f"{G_EVE} We zijn op dit moment gesloten. Onze openingstijden zijn van vier uur ’s middags tot tien uur ’s avonds."
    return G_DAY if t < time(18, 0) else G_EVE

# ===== Routes: runtime & voice =====
@app.get("/runtime/status", response_model=RuntimeOut)
def runtime_status():
    return evaluate_status()

@app.api_route("/voice/incoming", methods=["GET", "POST"])
def voice_incoming():
    # begroeting + stream bridge
    greet = select_greeting()
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="nl-NL">{greet}</Say>
  <Connect>
    <Stream url="wss://{{{{DomainName}}}}/twilio/stream"/>
  </Connect>
</Response>"""
    # Render/Cloudflare zetten Host header correct; DomainName hierboven is handige placeholder.
    twiml = twiml.replace("{{DomainName}}", os.getenv("PUBLIC_HOST", "")) if "{{DomainName}}" in twiml else twiml
    if "wss:///" in twiml:  # fallback: bouw absolute ws-url vanaf request host via CF/Render
        # eenvoudige vervanging met onze public host als aanwezig
        host = os.getenv("PUBLIC_HOST", "")
        if host:
            twiml = twiml.replace("wss:///", f"wss://{host}/")
    return Response(content=twiml, media_type="text/xml")

# ===== Admin API =====
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

@app.get("/admin/logout")
def admin_logout():
    raise HTTPException(status_code=401, detail="Logged out",
                        headers={"WWW-Authenticate": "Basic"})

# ===== Health =====
@app.get("/healthz")
def healthz():
    ok = True
    try:
        r.ping()
    except RedisError:
        ok = False
    return JSONResponse({"ok": ok, "redis": ok})

# ===== Twilio <-> OpenAI Realtime WebSocket bridge =====
# Twilio stuurt 8kHz μ-law (PCMU) base64 in "media" events.
# We vragen OpenAI audio-uit naar μ-law 8kHz zodat we zonder conversie terug kunnen sturen.
OPENAI_WS_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}"

async def openai_ws():
    headers = [
        ("Authorization", f"Bearer {OPENAI_API_KEY}"),
        ("OpenAI-Beta", "realtime=v1"),
    ]
    return await websockets.connect(OPENAI_WS_URL, extra_headers=headers, ping_interval=20, ping_timeout=20)

async def pump_twilio_to_openai(twilio_ws: WebSocket, openai_conn, stream_sid: str):
    """
    Ontvang Twilio 'media' events en push als input_audio_buffer naar OpenAI.
    """
    try:
        while True:
            msg = await twilio_ws.receive_text()
            evt = json.loads(msg)
            et = evt.get("event")
            if et == "media":
                payload_b64 = evt["media"]["payload"]
                # μ-law -> linear16 (8k)
                mulaw = base64.b64decode(payload_b64)
                lin16_8k = audioop.ulaw2lin(mulaw, 2)  # 2 bytes = 16-bit
                # 8k -> 16k upsampelen voor betere herkenning
                lin16_16k, _ = audioop.ratecv(lin16_8k, 2, 1, 8000, 16000, None)
                await openai_conn.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(lin16_16k).decode("ascii")
                }))
            elif et == "mark":  # negeren
                pass
            elif et == "stop":
                # commit open buffer en vraag om respons
                await openai_conn.send(json.dumps({"type": "input_audio_buffer.commit"}))
                await openai_conn.send(json.dumps({
                    "type": "response.create",
                    "response": {
                        "instructions": "Je bent Mada, een korte en vriendelijke assistent van een restaurant. Antwoord in het Nederlands.",
                        "modalities": ["audio"],
                        "audio": {"voice": "alloy", "format": "pcm_mulaw", "sample_rate": 8000}
                    }
                }))
                break
            # 'start' en andere events worden elders afgehandeld
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logging.exception(f"pump_twilio_to_openai error: {e}")

async def pump_openai_to_twilio(twilio_ws: WebSocket, openai_conn, stream_sid: str):
    """
    Lees OpenAI events en stream audio-delta's terug naar Twilio.
    We configureren OpenAI op μ-law 8k zodat de payload direct door kan.
    """
    try:
        async for raw in openai_conn:
            try:
                data = json.loads(raw)
            except Exception:
                # sommige frames kunnen binary zijn; sla over
                continue
            t = data.get("type")
            if t == "response.output_audio.delta":
                chunk_b64 = data.get("audio", "")
                # stuur naar Twilio als outbound media
                await twilio_ws.send_text(json.dumps({
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": chunk_b64}
                }))
            elif t == "response.completed":
                # optioneel markeren
                await twilio_ws.send_text(json.dumps({
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": "response_completed"}
                }))
            elif t == "error":
                logging.error(f"OpenAI error: {data}")
    except Exception as e:
        logging.exception(f"pump_openai_to_twilio error: {e}")

@app.websocket("/twilio/stream")
async def twilio_stream(ws: WebSocket):
    await ws.accept()
    stream_sid = None
    # open OpenAI realtime
    if not OPENAI_API_KEY:
        logging.error("OPENAI_API_KEY ontbreekt")
    try:
        oa = await openai_ws()
        # initialiseer sessie met voorkeuren: VAD server-side en μ-law uit
        await oa.send(json.dumps({
            "type": "session.update",
            "session": {
                "voice": "alloy",
                "input_audio_format": {"type": "wav", "sample_rate": 16000},
                "turn_detection": {"type": "server_vad", "silence_duration_ms": 600},
                "output_audio_format": {"type": "pcm_mulaw", "sample_rate": 8000},
                "instructions": "Je bent Mada, assistent van een restaurant. Wees kort, duidelijk en vriendelijk. Nederlands."
            }
        }))
        # wacht eerste 'start' van Twilio om streamSid te kennen
        while stream_sid is None:
            msg = await ws.receive_text()
            evt = json.loads(msg)
            if evt.get("event") == "start":
                stream_sid = evt["start"]["streamSid"]
                # bevestig ontvangst
                await ws.send_text(json.dumps({
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": "bridge_ready"}
                }))
            elif evt.get("event") == "media":
                # als Twilio direct media pusht, zet stream_sid defensief
                stream_sid = evt.get("streamSid", stream_sid)
                # plaats bericht terug in flow door lokaal te verwerken
                await oa.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(
                        audioop.ratecv(
                            audioop.ulaw2lin(base64.b64decode(evt["media"]["payload"]), 2),
                            2, 1, 8000, 16000, None
                        )[0]
                    ).decode("ascii")
                }))
                # we gaan door naar pomp-taken
                break

        # start pompen in beide richtingen
        tasks = [
            asyncio.create_task(pump_twilio_to_openai(ws, oa, stream_sid)),
            asyncio.create_task(pump_openai_to_twilio(ws, oa, stream_sid)),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logging.exception(f"/twilio/stream top-level error: {e}")
    finally:
        try:
            await ws.close()
        except Exception:
            pass
