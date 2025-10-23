# Mada Belbot - Twilio Voice webhook + Dashboard API

import os
import json
from datetime import datetime, time
from fastapi import FastAPI, Response, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from zoneinfo import ZoneInfo
from redis import Redis
from pathlib import Path

# ===== Init =====
app = FastAPI(title="Adams Belbot")
security = HTTPBasic()

# ===== Config & Auth =====
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "mada12")
TZ = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

def auth(creds: HTTPBasicCredentials = Depends(security)):
    if creds.username != ADMIN_USER or creds.password != ADMIN_PASS:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

# ===== Redis =====
r = Redis.from_url(REDIS_URL, decode_responses=True)
KEY_OVERRIDES = "belbot:overrides"

# ===== Openingstijden =====
OPEN_START, OPEN_END = time(16, 0), time(22, 0)
DELIVERY_START, DELIVERY_END = time(17, 0), time(21, 30)
CALLER_ID = "0226354645"
FALLBACK_PHONE = "0226427541"

# ===== Modellen =====
class TogglesIn(BaseModel):
    bot_enabled: bool = True
    kitchen_closed: bool = False
    pasta_available: bool = True
    delay_pasta_minutes: int = Field(default=0, ge=0)
    delay_schotels_minutes: int = Field(default=0, ge=0)
    is_open_override: str | None = "auto"
    delivery_enabled: bool | None = None
    pickup_enabled: bool | None = None
    ttl_minutes: int | None = 180

class RuntimeOut(BaseModel):
    now: str
    mode: str
    delivery_enabled: bool
    pickup_enabled: bool
    close_reason: str | None = None
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

def _load_overrides() -> TogglesIn | None:
    raw = r.get(KEY_OVERRIDES)
    if not raw:
        return None
    return TogglesIn(**json.loads(raw))

def _save_overrides(body: TogglesIn):
    ttl = (body.ttl_minutes or 180) * 60
    r.set(KEY_OVERRIDES, body.model_dump_json(), ex=ttl)

def evaluate_status(now: datetime | None = None) -> RuntimeOut:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    over = _load_overrides()
    open_auto, delivery_auto, pickup_auto = _auto(now)

    # bepaal open/dicht
    if over and over.is_open_override == "closed":
        open_now = False
    elif over and over.is_open_override == "open":
        open_now = True
    else:
        open_now = open_auto

    # keuken dicht
    if over and over.kitchen_closed:
        return RuntimeOut(
            now=now.isoformat(), mode="closed",
            delivery_enabled=False, pickup_enabled=False,
            close_reason=None, kitchen_closed=True,
            bot_enabled=over.bot_enabled,
            pasta_available=over.pasta_available,
            delay_pasta_minutes=over.delay_pasta_minutes,
            delay_schotels_minutes=over.delay_schotels_minutes,
            window={"open": "16:00", "delivery": "17:00-21:30", "close": "22:00"}
        )

    # buiten openingstijden
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
            window={"open": "16:00", "delivery": "17:00-21:30", "close": "22:00"}
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
        window={"open": "16:00", "delivery": "17:00-21:30", "close": "22:00"}
    )

# ===== Twilio greeting =====
NAME = "Ristorante Adams Spanbroek"
REC = "Dit gesprek kan tijdelijk worden opgenomen om onze service te verbeteren."
G_DAY = f"Goedemiddag, u spreekt met Mada, de digitale assistent van {NAME}. {REC}"
G_EVE = f"Goedenavond, u spreekt met Mada, de digitale assistent van {NAME}. {REC}"
G_CLOSED = f"Goedenavond, u spreekt met Mada, de digitale assistent van {NAME}. We zijn op dit moment gesloten. Onze openingstijden zijn van vier uur ’s middags tot tien uur ’s avonds."

def select_greeting(now: datetime | None = None) -> str:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    t = now.time()
    st = evaluate_status(now)

    if st.mode == "closed":
        if t < time(18, 0):
            return f"Goedemiddag, u spreekt met Mada, de digitale assistent van {NAME}. We zijn op dit moment gesloten. Onze openingstijden zijn van vier uur ’s middags tot tien uur ’s avonds."
        else:
            return f"Goedenavond, u spreekt met Mada, de digitale assistent van {NAME}. We zijn op dit moment gesloten. Onze openingstijden zijn van vier uur ’s middags tot tien uur ’s avonds."
    return G_DAY if t < time(18, 0) else G_EVE

# ===== Routes =====
@app.get("/runtime/status", response_model=RuntimeOut)
def runtime_status():
    return evaluate_status()

@app.api_route("/voice/incoming", methods=["GET", "POST"])
def voice_incoming():
    st = evaluate_status()
    text = select_greeting()
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="nl-NL">{text}</Say>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

@app.post("/admin/toggles", dependencies=[Depends(auth)], response_model=RuntimeOut)
def set_toggles(body: TogglesIn):
    valid = {0, 10, 20, 30, 45, 60}
    if body.delay_pasta_minutes not in valid or body.delay_schotels_minutes not in valid:
        raise HTTPException(status_code=400, detail="Delay must be one of 0,10,20,30,45,60")
    _save_overrides(body)
    return evaluate_status()

# ===== Static Admin UI =====
BASE_DIR = Path(__file__).resolve().parent
app.mount("/admin/ui", StaticFiles(directory=BASE_DIR / "admin_ui", html=True), name="admin_ui")
