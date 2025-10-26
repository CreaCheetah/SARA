import os, json, base64
from datetime import datetime, time
from urllib.parse import quote_plus
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request, Response, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.staticfiles import StaticFiles

from conversation_flow import FlowManager

# ---------- App ----------
app = FastAPI(title="SARA Belassistent")

# ---------- Config ----------
TZ = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))
_host = os.getenv("PUBLIC_BASE_URL", os.getenv("RENDER_EXTERNAL_HOSTNAME", "mada-3ijw.onrender.com"))
BASE_URL = _host if str(_host).startswith("http") else f"https://{_host}"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("TTS_VOICE", "marin")

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin")

REPO_ROOT = Path(__file__).resolve().parent
PROMPTS_PATH = Path(os.getenv("PROMPTS_PATH", REPO_ROOT / "prompts_order_nl.json"))
ADMIN_UI_DIR = Path(os.getenv("ADMIN_UI_DIR", REPO_ROOT / "admin_ui"))

# ---------- Helpers ----------
def _load_json(path: Path, fallback: dict) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback

PROMPTS = _load_json(PROMPTS_PATH, {})

def say_url(text: str) -> str:
    return f"{BASE_URL}/tts?text={quote_plus(text)}"

# ---------- Admin Basic Auth ----------
def _is_basic_auth_ok(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "): return False
    try:
        dec = base64.b64decode(auth.split(" ",1)[1]).decode("utf-8")
        user, pw = dec.split(":",1)
        return (user == ADMIN_USER and pw == ADMIN_PASS)
    except Exception:
        return False

@app.middleware("http")
async def admin_auth_mw(request: Request, call_next):
    p = request.url.path or ""
    if p.startswith("/admin/ui") or p.startswith("/admin/toggles"):
        if not _is_basic_auth_ok(request):
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Admin"'}, content="Unauthorized", media_type="text/plain")
    return await call_next(request)

if ADMIN_UI_DIR.exists():
    app.mount("/admin/ui", StaticFiles(directory=str(ADMIN_UI_DIR), html=True), name="admin-ui")

# ---------- Health ----------
@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "time": datetime.now(TZ).isoformat(), "tz": str(TZ)})

# ---------- Runtime ----------
@app.get("/runtime/status")
def runtime_status():
    return FlowManager.runtime_status()

# ---------- Admin toggles ----------
@app.post("/admin/toggles")
async def admin_toggles(request: Request):
    body = await request.json()
    return FlowManager.save_overrides_api(body)

# ---------- TTS ----------
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
        return Response(content=r.content, media_type="audio/mpeg")

# ---------- Voice ----------
@app.api_route("/voice/incoming", methods=["GET","POST"])
def voice_incoming():
    greet = FlowManager.greeting(PROMPTS)
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>{say_url(greet)}</Play>
  <Redirect method="POST">{BASE_URL}/voice/step</Redirect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

@app.api_route("/voice/step", methods=["GET","POST"])
def voice_step():
    hints = "bestellen, pizza, schotel, pasta, afhalen, bezorgen, contant, ideal, postcode, huisnummer, telefoonnummer, dat is alles, klaar, ja, nee"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="speech" language="nl-NL" hints="{hints}"
          action="{BASE_URL}/voice/handle" method="POST"
          timeout="8" speechTimeout="auto" bargeIn="true" />
  <Redirect method="POST">{BASE_URL}/voice/step</Redirect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

@app.post("/voice/handle")
async def voice_handle(request: Request):
    form = await request.form()
    call_sid = (form.get("CallSid") or "no-sid").strip()
    speech = (form.get("SpeechResult") or "").strip()

    if FlowManager.is_closed():
        tw = f"""<?xml version="1.0" encoding="UTF-8"?><Response><Play>{say_url(PROMPTS["greet_closed"])}</Play></Response>"""
        return Response(content=tw, media_type="text/xml")

    out = FlowManager.handle_utterance(call_sid, speech, PROMPTS)
    parts = "".join([f"<Play>{say_url(m)}</Play>" for m in out.get("messages", [])])
    if out.get("next") == "end":
        return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{parts}</Response>', media_type="text/xml")
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{parts}<Redirect method="POST">{BASE_URL}/voice/step</Redirect></Response>', media_type="text/xml")

# ---------- Twilio status callback ----------
@app.post("/voice/status")
async def voice_status(request: Request):
    try:
        data = await request.form()
        payload = {k: data.get(k) for k in data.keys()}
    except Exception:
        payload = {}
    try:
        os.makedirs("/mnt/data", exist_ok=True)
        with open("/mnt/data/twilio_status.log", "a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return PlainTextResponse("ok")
