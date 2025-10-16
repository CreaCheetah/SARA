# app.py — Adams Belbot (bot-toggle, status, begroetingen, toggles)
from datetime import datetime, time, timedelta
from fastapi import FastAPI, Response, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from zoneinfo import ZoneInfo

app = FastAPI(title="Adams Belbot")

# ===== Config =====
TZ = ZoneInfo("Europe/Amsterdam")
OPEN_START, OPEN_END = time(16, 0), time(22, 0)          # 16:00–22:00
DELIVERY_START, DELIVERY_END = time(17, 0), time(21, 30) # 17:00–21:30

# Telefoonconfig
CALLER_ID      = "0226354645"  # Twilio-uitgaand callerId
FALLBACK_PHONE = "0226427541"  # doelnummer bij bot_enabled=False

# Beheer-auth
security = HTTPBasic()
ADMIN_USER = "admin"
ADMIN_PASS = "AdamAdam2513"

def auth(creds: HTTPBasicCredentials = Depends(security)):
    if creds.username != ADMIN_USER or creds.password != ADMIN_PASS:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

# ===== In-memory overrides + TTL =====
_over = None            # type: TogglesIn | None
_over_expiry = None     # type: datetime | None

# ===== Modellen =====
class TogglesIn(BaseModel):
    # Hoofdschakelaar
    bot_enabled: bool = True

    # Keuken/assortiment
    kitchen_closed: bool = False
    pasta_available: bool = True

    # Vertragingen
    delay_pasta_minutes: int = Field(default=0, ge=0)     # 0|10|20|30|45|60
    delay_schotels_minutes: int = Field(default=0, ge=0)  # 0|10|20|30|45|60

    # Basis-overrides (optioneel)
    is_open_override: str | None = "auto"                 # "open"|"closed"|"auto"
    delivery_enabled: bool | None = None                  # None = auto-venster
    pickup_enabled: bool | None = None                    # None = auto-venster

    ttl_minutes: int | None = 180

class RuntimeOut(BaseModel):
    now: str
    mode: str                                 # "open"|"closed"
    delivery_enabled: bool
    pickup_enabled: bool
    close_reason: str | None = None           # alleen voor gesloten buiten uren
    kitchen_closed: bool = False
    bot_enabled: bool = True
    # categorie-status
    pasta_available: bool
    delay_pasta_minutes: int
    delay_schotels_minutes: int
    # venster-info
    window: dict

# ===== Helpers =====
def _load_overrides() -> TogglesIn | None:
    global _over, _over_expiry
    if _over and _over_expiry and datetime.now(TZ) > _over_expiry:
        _over, _over_expiry = None, None
    return _over

def _auto(now: datetime):
    t = now.time()
    open_now = OPEN_START <= t < OPEN_END
    delivery_auto = DELIVERY_START <= t < DELIVERY_END
    pickup_auto = OPEN_START <= t < OPEN_END
    return open_now, delivery_auto, pickup_auto

def evaluate_status(now: datetime | None = None) -> RuntimeOut:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    over = _load_overrides()
    open_auto, delivery_auto, pickup_auto = _auto(now)

    # open/closed mode
    if over and over.is_open_override == "closed":
        open_now = False
    elif over and over.is_open_override == "open":
        open_now = True
    else:
        open_now = open_auto

    # Keuken gesloten ⇒ alles dicht
    if over and over.kitchen_closed:
        return RuntimeOut(
            now=now.isoformat(), mode="closed",
            delivery_enabled=False, pickup_enabled=False,
            close_reason=None, kitchen_closed=True,
            bot_enabled=(over.bot_enabled if over else True),
            pasta_available=over.pasta_available if over else True,
            delay_pasta_minutes=over.delay_pasta_minutes if over else 0,
            delay_schotels_minutes=over.delay_schotels_minutes if over else 0,
            window={"open":"16:00","delivery":"17:00-21:30","close":"22:00"}
        )

    # Buiten open-uren ⇒ dicht
    if not open_now:
        return RuntimeOut(
            now=now.isoformat(), mode="closed",
            delivery_enabled=False, pickup_enabled=False,
            close_reason="We zijn op dit moment gesloten.",
            kitchen_closed=False,
            bot_enabled=(over.bot_enabled if over else True),
            pasta_available=over.pasta_available if over else True,
            delay_pasta_minutes=over.delay_pasta_minutes if over else 0,
            delay_schotels_minutes=over.delay_schotels_minutes if over else 0,
            window={"open":"16:00","delivery":"17:00-21:30","close":"22:00"}
        )

    # Binnen open-uren ⇒ kanalen bepalen
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
        pasta_available=over.pasta_available if over else True,
        delay_pasta_minutes=over.delay_pasta_minutes if over else 0,
        delay_schotels_minutes=over.delay_schotels_minutes if over else 0,
        window={"open":"16:00","delivery":"17:00-21:30","close":"22:00"}
    )

# ===== Begroetingen =====
NAME = "Ristorante Adams Spanbroek"
REC = "Dit gesprek kan tijdelijk worden opgenomen om onze service te verbeteren."
G_DAY    = f"Goedemiddag, u spreekt met Mada, de digitale assistent van {NAME}. {REC} Waar kan ik u vandaag mee helpen?"
G_EVE    = f"Goedenavond, u spreekt met Mada, de digitale assistent van {NAME}. {REC} Waar kan ik u vandaag mee helpen?"
G_LATE   = f"Goedenavond, u spreekt met Mada, de digitale assistent van {NAME}. {REC} We zijn nog geopend tot tien uur. Bezorging is nu gesloten, afhalen kan nog. Waar kan ik u vandaag mee helpen?"
G_CLOSED = f"Goedenavond, u spreekt met Mada, de digitale assistent van {NAME}. We zijn op dit moment gesloten. Onze openingstijden zijn van vier uur ’s middags tot tien uur ’s avonds. U kunt uw bestelling plaatsen zodra we weer geopend zijn."
G_KITCHEN= f"Goedenavond, u spreekt met Mada, de digitale assistent van {NAME}. Excuses, de keuken is gesloten. We nemen nu geen bestellingen aan."

def select_greeting(now: datetime | None = None) -> str:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    t = now.time()
    st = evaluate_status(now)

    if st.kitchen_closed:
        return G_KITCHEN
    if st.mode == "closed":
        return G_CLOSED
    if time(21,30) <= t < OPEN_END:
        return G_LATE
    return G_DAY if t < time(18, 0) else G_EVE

# ===== Endpoints =====
@app.get("/runtime/status", response_model=RuntimeOut)
def runtime_status():
    return evaluate_status()

@app.api_route("/voice/incoming", methods=["GET","POST"])
def voice_incoming():
    st = evaluate_status()
    # Hoofdschakelaar: bot uit -> direct doorverbinden
    if not st.bot_enabled:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Dial callerId="{CALLER_ID}">{FALLBACK_PHONE}</Dial>
</Response>"""
        return Response(content=twiml, media_type="text/xml")

    # Bot aan -> begroeting + gather
    text = select_greeting()
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="nl-NL">{text}</Say>
  <Pause length="0.2"/>
  <Gather input="speech" language="nl-NL" action="/voice/continue" bargeIn="true" speechTimeout="auto"/>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

@app.post("/voice/continue")
def voice_continue():
    return Response(content="<?xml version='1.0' encoding='UTF-8'?><Response><Say language='nl-NL'>Een ogenblik alstublieft.</Say></Response>", media_type="text/xml")

@app.post("/admin/toggles", dependencies=[Depends(auth)], response_model=RuntimeOut)
def set_toggles(body: TogglesIn):
    valid = {0,10,20,30,45,60}
    if body.delay_pasta_minutes not in valid or body.delay_schotels_minutes not in valid:
        raise HTTPException(status_code=400, detail="Delay must be one of 0,10,20,30,45,60")
    global _over, _over_expiry
    _over = body
    _over_expiry = datetime.now(TZ) + timedelta(minutes=(body.ttl_minutes or 180))
    return evaluate_status()
