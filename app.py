import os
import json
from datetime import datetime, time
from typing import Optional, Literal
from urllib.parse import quote_plus

import httpx
from fastapi import FastAPI, Response, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from zoneinfo import ZoneInfo

# ---------------- App & config ----------------
app = FastAPI(title="Mada Belassistent")
security = HTTPBasic()

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "CHANGE_ME")

TZ = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))

# Render zet RENDER_EXTERNAL_HOSTNAME. Beter: PUBLIC_BASE_URL="https://mada-3ijw.onrender.com"
_host = os.getenv("PUBLIC_BASE_URL", os.getenv("RENDER_EXTERNAL_HOSTNAME", "mada-3ijw.onrender.com"))
BASE_URL = _host if _host.startswith("http") else f"https://{_host}"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TTS_MODEL = os.getenv("ENFTTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("ENFTTS_VOICE", "marin")  # geldige waarden: alloy, echo, fable, onyx, nova, shimmer, coral, verse, ballad, ash, sage, marin, cedar
RECORD_CALLS = os.getenv("RECORD_CALLS", "false").lower() == "true"

def require_auth(creds: HTTPBasicCredentials = Depends(security)) -> bool:
    if creds.username != ADMIN_USER or creds.password != ADMIN_PASS:
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return True

# ---------------- Openingstijden / status ----------------
OPEN_START, OPEN_END = time(16, 0), time(22, 0)
DEL_START, DEL_END   = time(17, 0), time(21, 30)

class RuntimeOut(BaseModel):
    now: str
    mode: Literal["open", "closed"]
    delivery_enabled: bool
    window: dict

def evaluate_status(now: Optional[datetime] = None) -> RuntimeOut:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    t = now.time()
    open_now = OPEN_START <= t < OPEN_END
    delivery = open_now and (DEL_START <= t < DEL_END)
    return RuntimeOut(
        now=now.isoformat(),
        mode="open" if open_now else "closed",
        delivery_enabled=delivery,
        window={"open":"16:00","delivery":"17:00-21:30","close":"22:00"},
    )

@app.get("/runtime/status", response_model=RuntimeOut)
def runtime_status():
    return evaluate_status()

# ---------------- Health ----------------
@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "time": datetime.now(TZ).isoformat()})

# ---------------- OpenAI TTS ----------------
@app.get("/tts")
async def tts(text: str):
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY missing")

    url = "https://api.openai.com/v1/audio/speech"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": TTS_MODEL, "voice": TTS_VOICE, "input": text, "format": "mp3"}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"TTS error {r.status_code}: {r.text}")
        audio = r.content

    return Response(content=audio, media_type="audio/mpeg")

# ---------------- Twilio Voice flow ----------------
def greeting() -> str:
    now = datetime.now(TZ).time()
    dagdeel = "Goedemiddag" if now < time(18, 0) else "Goedenavond"
    rec = " Dit gesprek kan tijdelijk worden opgenomen om onze service te verbeteren." if RECORD_CALLS else ""
    return f"{dagdeel}, u spreekt met Mada, de digitale assistent van Ristorante Adam Spanbroek.{rec} Waarmee kan ik u helpen vandaag?"

def say_url(text: str) -> str:
    # absolute URL voor Twilio Play
    return f"{BASE_URL}/tts?text={quote_plus(text)}"

@app.api_route("/voice/incoming", methods=["GET", "POST"])
def voice_incoming():
    # begroeting afspelen en door naar /voice/step
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>{say_url(greeting())}</Play>
  <Redirect method="POST">{BASE_URL}/voice/step</Redirect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

@app.api_route("/voice/step", methods=["GET", "POST"])
def voice_step():
    # verzamel NL spraak
    # hints helpen de herkenning
    hints = "bestellen, afhalen, bezorgen, pizza, schotel, postcode, huisnummer, telefoonnummer, klaar, stop"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="speech" language="nl-NL" hints="{hints}" action="{BASE_URL}/voice/handle" method="POST" timeout="6">
    <Play>{say_url("Zegt u maar.")}</Play>
  </Gather>
  <Redirect method="POST">{BASE_URL}/voice/step</Redirect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

def _contains(text: str, *keys: str) -> bool:
    t = text.lower()
    return any(k in t for k in keys)

@app.post("/voice/handle")
async def voice_handle(request: Request):
    form = await request.form()
    speech = (form.get("SpeechResult") or "").strip()

    if not speech:
        # niets gehoord
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>{say_url("Ik heb niets verstaan. Kunt u het herhalen?")}</Play>
  <Redirect method="POST">{BASE_URL}/voice/step</Redirect>
</Response>"""
        return Response(content=twiml, media_type="text/xml")

    # heel eenvoudige routering voor nu
    if _contains(speech, "bezorg", "bezorgen"):
        msg = "Prima. Wilt u uw bezorgadres en postcode noemen?"
    elif _contains(speech, "afhaal", "afhalen", "ophalen"):
        msg = "Afhalen is genoteerd. Wat wilt u bestellen?"
    elif _contains(speech, "telefoon", "nummer"):
        msg = "Noemt u alstublieft uw telefoonnummer langzaam."
    elif _contains(speech, "postcode"):
        msg = "Dank u. En het huisnummer alstublieft."
    elif _contains(speech, "klaar", "dat is alles", "niets meer"):
        msg = "Dank u. Ik vat zo samen. Een ogenblik alstublieft."
    else:
        msg = "Begrepen. Wilt u nog iets toevoegen of is dit uw volledige bestelling?"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>{say_url(msg)}</Play>
  <Redirect method="POST">{BASE_URL}/voice/step</Redirect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

# ---------------- Basic-auth test route (optioneel) ----------------
@app.get("/admin/ping", dependencies=[Depends(require_auth)])
def admin_ping():
    return {"ok": True}
