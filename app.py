# Mada Belbot - Twilio Voice webhook + Dashboard API

import os
import json
import re
import threading
from datetime import datetime, time
from pathlib import Path
from typing import Optional, Literal

from fastapi import FastAPI, Response, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, JSONResponse
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError
from zoneinfo import ZoneInfo
from redis import Redis
from redis.exceptions import RedisError

# ===== Init =====
app = FastAPI(title="Adams Belbot")
security = HTTPBasic()

# ===== Config & Auth =====
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "CHANGE_ME")
TZ = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
CUSTOMERS_PATH = os.getenv("CUSTOMERS_FILE", "customers_clean.json")

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

# ===== Twilio greeting =====
NAME = "Ristorante Adam Spanbroek"
REC = "Dit gesprek kan tijdelijk worden opgenomen om onze service te verbeteren."
G_DAY = f"Goedemiddag, u spreekt met Mada, de digitale assistent van {NAME}. {REC}"
G_EVE = f"Goedenavond, u spreekt met Mada, de digitale assistent van {NAME}. {REC}"

def select_greeting(now: Optional[datetime] = None) -> str:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    t = now.time()
    st = evaluate_status(now)
    if st.mode == "closed":
        if t < time(18, 0):
            return f"Goedemiddag, u spreekt met Mada, de digitale assistent van {NAME}. We zijn op dit moment gesloten. Onze openingstijden zijn van vier uur ’s middags tot tien uur ’s avonds."
        else:
            return f"Goedenavond, u spreekt met Mada, de digitale assistent van {NAME}. We zijn op dit moment gesloten. Onze openingstijden zijn van vier uur ’s middags tot tien uur ’s avonds."
    return G_DAY if t < time(18, 0) else G_EVE

# ===== Routes: runtime & voice =====
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

# ===== Twilio Media Stream WebSocket =====
@app.websocket("/twilio/stream")
async def twilio_stream(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            _ = await ws.receive_text()
    except WebSocketDisconnect:
        pass

# ===== Customers lookup (protected) =====
_customers_lock = threading.Lock()
_customers_idx = {}
_customers_mtime = None
_phone_rx = re.compile(r"\D+")

def _norm_phone(v: str) -> str:
    if not v:
        return ""
    digits = _phone_rx.sub("", v)
    if digits.startswith("31") and len(digits) >= 11:
        digits = "0" + digits[2:]
    return digits

def _load_customers(force: bool = False):
    global _customers_idx, _customers_mtime
    p = Path(CUSTOMERS_PATH)
    if not p.exists():
        return
    stat = p.stat()
    if not force and _customers_mtime == stat.st_mtime:
        return
    with _customers_lock:
        data = json.loads(p.read_text(encoding="utf-8"))
        idx = {}
        for row in data:
            phones = filter(None, [
                _norm_phone(row.get("telefoon", "")),
                _norm_phone(row.get("telefoon2", "")),
            ])
            for tel in set([t for t in phones if t]):
                idx[tel] = {
                    "naam": row.get("naam", ""),
                    "telefoon": row.get("telefoon", ""),
                    "telefoon2": row.get("telefoon2", ""),
                    "straat": row.get("straat", ""),
                    "huisnr": row.get("huisnr", ""),
                    "postcode": row.get("postcode", ""),
                }
        _customers_idx = idx
        _customers_mtime = stat.st_mtime

try:
    _load_customers(force=True)
except Exception:
    pass

@app.get("/admin/customers/lookup", dependencies=[Depends(auth)])
def customers_lookup(phone: str):
    _load_customers()
    tel = _norm_phone(phone)
    rec = _customers_idx.get(tel)
    if not rec and len(tel) >= 8:
        rec = _customers_idx.get(tel[-8:])
    return JSONResponse({"found": bool(rec), "match_key": tel, "record": rec})

@app.post("/admin/customers/reload", dependencies=[Depends(auth)])
def customers_reload():
    _load_customers(force=True)
    return {"ok": True, "count": len(_customers_idx)}

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

# ===== Logout (forceer nieuwe Basic Auth prompt) =====
@app.get("/admin/logout")
def admin_logout():
    raise HTTPException(
        status_code=401,
        detail="Logged out",
        headers={"WWW-Authenticate": "Basic"},
    )

# ===== Health & diagnostics =====
@app.get("/healthz")
def healthz():
    ok = True
    try:
        r.ping()
    except RedisError:
        ok = False
    return JSONResponse({"ok": ok, "redis": ok})
