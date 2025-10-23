# Mada Belbot + Dashboard security (Basic Auth via env)

from datetime import datetime, time
import os, json, secrets, base64
from fastapi import FastAPI, Response, Depends, HTTPException, status, APIRouter
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from zoneinfo import ZoneInfo
from pydantic import BaseModel, Field
from redis import Redis

app = FastAPI(title="Adams Belbot")

# ===== Config =====
TZ = ZoneInfo("Europe/Amsterdam")
OPEN_START, OPEN_END = time(16, 0), time(22, 0)
DELIVERY_START, DELIVERY_END = time(17, 0), time(21, 30)

CALLER_ID = "0226354645"
FALLBACK_PHONE = "0226427541"

# ===== Auth =====
security = HTTPBasic()
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "changeme")

def require_admin(creds: HTTPBasicCredentials = Depends(security)):
    u_ok = secrets.compare_digest(creds.username, ADMIN_USER)
    p_ok = secrets.compare_digest(creds.password, ADMIN_PASS)
    if not (u_ok and p_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True

# ===== Middleware voor dashboardbeveiliging =====
class BasicAuthStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path.startswith("/admin/ui"):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                return Response(status_code=401, headers={"WWW-Authenticate": "Basic"})
            try:
                user, pwd = base64.b64decode(auth.split(" ", 1)[1]).decode().split(":", 1)
            except Exception:
                return Response(status_code=401, headers={"WWW-Authenticate": "Basic"})
            ok = secrets.compare_digest(user, ADMIN_USER) and secrets.compare_digest(pwd, ADMIN_PASS)
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": "Basic"})
        return await call_next(request)

app.add_middleware(BasicAuthStaticMiddleware)

# ===== Redis =====
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
r = Redis.from_url(REDIS_URL, decode_responses=True)
KEY_OVERRIDES = "belbot:overrides"

# ===== Datamodellen =====
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

# ===== Logica =====
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
    data = json.loads(raw)
    return TogglesIn(**data)

def _save_overrides(body: TogglesIn):
    ttl = (body.ttl_minutes or 180) * 60
    r.set(KEY_OVERRIDES, body.model_dump_json(), ex=ttl)

def evaluate_status(now: datetime | None = None) -> RuntimeOut:
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
            bot_enabled=over.bot_enabled,
            pasta_available=over.pasta_available,
            delay_pasta_minutes=over.delay_pasta_minutes,
            delay_schotels_minutes=over.delay_schotels_minutes,
            window={"open": "16:00", "delivery": "17:00-21:30", "close": "22:00"}
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

# ===== Begroetingen =====
NAME = "Ristorante Adams Spanbroek"
REC = "Dit gesprek kan tijdelijk worden opgenomen om onze service te verbeteren."

def select_greeting(now: datetime | None = None) -> str:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    t = now.time()
    st = evaluate_status(now)
    if st.mode == "closed":
        if t < time(18, 0):
            return f"Goedemiddag, u spreekt met Mada, de digitale assistent van {NAME}. We zijn op dit moment gesloten. Onze openingstijden zijn van vier uur ’s middags tot tien uur ’s avonds."
        else:
            return f"Goedenavond, u spreekt met Mada, de digitale assistent van {NAME}. We zijn op dit moment gesloten. Onze openingstijden zijn van vier uur ’s middags tot tien uur ’s avonds."
    return "Goedemiddag" if t < time(18, 0) else "Goedenavond"

# ===== Endpoints =====
@app.get("/runtime/status", response_model=RuntimeOut, dependencies=[Depends(require_admin)])
def runtime_status():
    return evaluate_status()

@app.api_route("/voice/incoming", methods=["GET", "POST"])
def voice_incoming():
    st = evaluate_status()
    text = select_greeting()
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response><Say language="nl-NL">{text}</Say></Response>"""
    return Response(content=twiml, media_type="text/xml")

@app.post("/admin/toggles", dependencies=[Depends(require_admin)], response_model=RuntimeOut)
def set_toggles(body: TogglesIn):
    valid = {0, 10, 20, 30, 45, 60}
    if body.delay_pasta_minutes not in valid or body.delay_schotels_minutes not in valid:
        raise HTTPException(status_code=400, detail="Delay must be one of 0,10,20,30,45,60")
    _save_overrides(body)
    return evaluate_status()

# ===== Static dashboard =====
app.mount("/admin/ui", StaticFiles(directory="admin_ui", html=True), name="admin_ui")

@app.get("/healthz")
def healthz():
    return {"ok": True}
