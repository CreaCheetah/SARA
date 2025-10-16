# app.py
from datetime import datetime, time
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from zoneinfo import ZoneInfo

app = FastAPI(title="Adams Belbot")

# Tijdzone en vensters
TZ = ZoneInfo("Europe/Amsterdam")
OPEN_START, OPEN_END       = time(16, 0), time(22, 0)   # open 16:00–22:00
DELIVERY_START, DELIVERY_END = time(17, 0), time(21, 30) # bezorging 17:00–21:30

# Begroetingsteksten (juridisch correct + stijl)
REC_NOTICE = "Dit gesprek kan tijdelijk worden opgenomen om onze service te verbeteren."
NAME = "Ristorante Adams Spanbroek"

GREET_DAY = f"Goedemiddag, u spreekt met Mada, de digitale assistent van {NAME}. {REC_NOTICE} Waar kan ik u vandaag mee helpen?"
GREET_EVE = f"Goedenavond, u spreekt met Mada, de digitale assistent van {NAME}. {REC_NOTICE} Waar kan ik u vandaag mee helpen?"
GREET_LATE = f"Goedenavond, u spreekt met Mada, de digitale assistent van {NAME}. {REC_NOTICE} We zijn nog geopend tot tien uur. Bezorging is nu gesloten, afhalen kan nog. Waar kan ik u vandaag mee helpen?"
GREET_CLOSED = f"Goedenavond, u spreekt met Mada, de digitale assistent van {NAME}. We zijn op dit moment gesloten. Onze openingstijden zijn van vier uur ’s middags tot tien uur ’s avonds. U kunt uw bestelling plaatsen zodra we weer geopend zijn."

def eval_status(now: datetime | None = None):
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    t = now.time()
    open_now = OPEN_START <= t < OPEN_END
    delivery = DELIVERY_START <= t < DELIVERY_END
    pickup = OPEN_START <= t < OPEN_END
    return {
        "now": now.isoformat(),
        "open": open_now,
        "delivery_enabled": delivery if open_now else False,
        "pickup_enabled": pickup if open_now else False,
    }

def select_greeting(now: datetime | None = None) -> str:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    t = now.time()
    st = eval_status(now)
    if not st["open"]:
        return GREET_CLOSED
    # 21:30–22:00 alleen afhalen
    if time(21,30) <= t < OPEN_END:
        return GREET_LATE
    # 16:00–18:00 dag, 18:00–21:30 avond
    return GREET_DAY if t < time(18,0) else GREET_EVE

@app.get("/runtime/status")
def runtime_status():
    return JSONResponse(eval_status())

@app.post("/voice/incoming")
def voice_incoming():
    # TwiML als plain text
    text = select_greeting()
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="nl-NL">{text}</Say>
  <Pause length="0.2"/>
  <Gather input="speech" language="nl-NL" action="/voice/continue" bargeIn="true" speechTimeout="auto"/>
</Response>"""
    return PlainTextResponse(twiml, media_type="text/xml")

# Optioneel vervolg-endpoint (stub)
@app.post("/voice/continue")
def voice_continue():
    return PlainTextResponse("<?xml version='1.0' encoding='UTF-8'?><Response><Say language='nl-NL'>Een ogenblik alstublieft.</Say></Response>", media_type="text/xml")
