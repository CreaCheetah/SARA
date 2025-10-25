# Mada Belbot - Twilio Voice webhook + Dashboard + TTS + State Machine (NL)
import os, json, re, base64, hashlib, requests, threading
from datetime import datetime, time
from pathlib import Path
from typing import Optional, Literal
from urllib.parse import quote

from fastapi import FastAPI, Response, Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError
from zoneinfo import ZoneInfo
from redis import Redis
from redis.exceptions import RedisError

app = FastAPI(title="Mada AI Assistent")
security = HTTPBasic()

# ===== Config =====
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "CHANGE_ME")
TZ = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
r = Redis.from_url(REDIS_URL, decode_responses=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("TTS_VOICE", "marin")
TTS_FORMAT = "mp3"

CUSTOMERS_PATH = os.getenv("CUSTOMERS_FILE", "customers_clean.json")
KEY_OVERRIDES = "belbot:overrides"
OPEN_START, OPEN_END = time(16, 0), time(22, 0)
DELIVERY_START, DELIVERY_END = time(17, 0), time(21, 30)

# ===== Auth =====
def auth(creds: HTTPBasicCredentials = Depends(security)) -> bool:
    if creds.username != ADMIN_USER or creds.password != ADMIN_PASS:
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate":"Basic"})
    return True

# ===== Models =====
class TogglesIn(BaseModel):
    bot_enabled: bool = True
    kitchen_closed: bool = False
    pasta_available: bool = True
    delay_pasta_minutes: int = Field(default=0, ge=0)
    delay_schotels_minutes: int = Field(default=0, ge=0)
    is_open_override: Literal["auto","open","closed"] = "auto"
    delivery_enabled: Optional[bool] = None
    pickup_enabled: Optional[bool] = None
    ttl_minutes: int = Field(default=180, ge=1, le=720)

class RuntimeOut(BaseModel):
    now: str
    mode: Literal["open","closed"]
    delivery_enabled: bool
    pickup_enabled: bool
    close_reason: Optional[str] = None
    kitchen_closed: bool = False
    bot_enabled: bool = True
    pasta_available: bool
    delay_pasta_minutes: int
    delay_schotels_minutes: int
    window: dict

# ===== Runtime calc =====
def _auto(now: datetime):
    t = now.time()
    open_now = OPEN_START <= t < OPEN_END
    delivery_auto = DELIVERY_START <= t < DELIVERY_END
    pickup_auto = OPEN_START <= t < OPEN_END
    return open_now, delivery_auto, pickup_auto

def _load_overrides() -> Optional[TogglesIn]:
    try:
        raw = r.get(KEY_OVERRIDES)
        return TogglesIn(**json.loads(raw)) if raw else None
    except Exception:
        return None

def _save_overrides(body: TogglesIn):
    try:
        r.set(KEY_OVERRIDES, body.model_dump_json(), ex=int(body.ttl_minutes)*60)
    except RedisError:
        raise HTTPException(status_code=503, detail="Cache unavailable")

def evaluate_status(now: Optional[datetime]=None) -> RuntimeOut:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    over = _load_overrides()
    open_auto, delivery_auto, pickup_auto = _auto(now)

    if over and over.is_open_override == "closed": open_now=False
    elif over and over.is_open_override == "open": open_now=True
    else: open_now=open_auto

    if over and over.kitchen_closed:
        return RuntimeOut(now=now.isoformat(), mode="closed",
            delivery_enabled=False, pickup_enabled=False, close_reason=None,
            kitchen_closed=True, bot_enabled=(over.bot_enabled if over else True),
            pasta_available=(over.pasta_available if over else True),
            delay_pasta_minutes=(over.delay_pasta_minutes if over else 0),
            delay_schotels_minutes=(over.delay_schotels_minutes if over else 0),
            window={"open":"16:00","delivery":"17:00-21:30","close":"22:00"})

    if not open_now:
        return RuntimeOut(now=now.isoformat(), mode="closed",
            delivery_enabled=False, pickup_enabled=False,
            close_reason="We zijn op dit moment gesloten.",
            kitchen_closed=False, bot_enabled=(over.bot_enabled if over else True),
            pasta_available=(over.pasta_available if over else True),
            delay_pasta_minutes=(over.delay_pasta_minutes if over else 0),
            delay_schotels_minutes=(over.delay_schotels_minutes if over else 0),
            window={"open":"16:00","delivery":"17:00-21:30","close":"22:00"})

    delivery = delivery_auto
    pickup = pickup_auto
    if over:
        if over.delivery_enabled is not None: delivery = delivery and over.delivery_enabled
        if over.pickup_enabled is not None: pickup = pickup and over.pickup_enabled

    return RuntimeOut(now=now.isoformat(), mode="open",
        delivery_enabled=delivery, pickup_enabled=pickup,
        close_reason=None, kitchen_closed=False,
        bot_enabled=(over.bot_enabled if over else True),
        pasta_available=(over.pasta_available if over else True),
        delay_pasta_minutes=(over.delay_pasta_minutes if over else 0),
        delay_schotels_minutes=(over.delay_schotels_minutes if over else 0),
        window={"open":"16:00","delivery":"17:00-21:30","close":"22:00"})

# ===== Greeting text =====
NAME = "Ristorante Adam Spanbroek"
def select_greeting(now: Optional[datetime]=None) -> str:
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    t = now.time()
    st = evaluate_status(now)
    dagdeel = "Goedemiddag" if t < time(18,0) else "Goedenavond"
    if st.mode == "closed":
        return f"{dagdeel}, u spreekt met Mada, de digitale assistent van {NAME}. We zijn op dit moment gesloten. Vanaf vier uur ’s middags zijn we weer bereikbaar."
    return f"{dagdeel}, u spreekt met Mada, de digitale assistent van {NAME}. Waarmee kan ik u helpen?"

# ===== OpenAI TTS (cached) =====
def _tts_key(text:str)->str:
    return f"tts:{TTS_MODEL}:{TTS_VOICE}:{hashlib.sha1(text.encode('utf-8')).hexdigest()}"

def tts_bytes(text:str)->bytes:
    if not OPENAI_API_KEY: raise HTTPException(500,"OPENAI_API_KEY missing")
    key=_tts_key(text); cached=r.get(key)
    if cached:
        try: return base64.b64decode(cached)
        except Exception: pass
    url="https://api.openai.com/v1/audio/speech"
    payload={"model":TTS_MODEL,"voice":TTS_VOICE,"input":text,"format":TTS_FORMAT,"language":"nl-NL"}
    headers={"Authorization":f"Bearer {OPENAI_API_KEY}","Content-Type":"application/json"}
    resp=requests.post(url,headers=headers,json=payload,timeout=60)
    if resp.status_code!=200: raise HTTPException(502,f"TTS error {resp.status_code}: {resp.text[:200]}")
    audio=resp.content
    try: r.setex(key, 12*3600, base64.b64encode(audio).decode("ascii"))
    except RedisError: pass
    return audio

@app.get("/tts")
def tts(text:str):
    return Response(content=tts_bytes(text), media_type="audio/mpeg")

# ===== Customers index =====
_customers_lock = threading.Lock()
_customers_idx = {}; _customers_mtime=None; _phone_rx = re.compile(r"\D+")

def _norm_phone(v:str)->str:
    if not v: return ""
    d=_phone_rx.sub("",v)
    if d.startswith("31") and len(d)>=11: d="0"+d[2:]
    return d

def _load_customers(force:bool=False):
    global _customers_idx,_customers_mtime
    p=Path(CUSTOMERS_PATH)
    if not p.exists(): return
    st=p.stat()
    if not force and _customers_mtime==st.st_mtime: return
    with _customers_lock:
        data=json.loads(p.read_text(encoding="utf-8"))
        idx={}
        for row in data:
            phones=[_norm_phone(row.get("telefoon","")), _norm_phone(row.get("telefoon2",""))]
            for tel in {t for t in phones if t}:
                idx[tel]={
                    "naam": row.get("naam",""),
                    "telefoon": row.get("telefoon",""),
                    "telefoon2": row.get("telefoon2",""),
                    "straat": row.get("straat",""),
                    "huisnr": row.get("huisnr",""),
                    "postcode": row.get("postcode",""),
                }
        _customers_idx=idx; _customers_mtime=st.st_mtime

try: _load_customers(force=True)
except Exception: pass

@app.get("/admin/customers/lookup", dependencies=[Depends(auth)])
def customers_lookup(phone:str):
    _load_customers()
    tel=_norm_phone(phone)
    rec=_customers_idx.get(tel) or (_customers_idx.get(tel[-8:]) if len(tel)>=8 else None)
    return {"found":bool(rec),"match_key":tel,"record":rec}

@app.post("/admin/customers/reload", dependencies=[Depends(auth)])
def customers_reload():
    _load_customers(force=True)
    return {"ok":True,"count":len(_customers_idx)}

# ===== Admin & Health =====
@app.get("/runtime/status", response_model=RuntimeOut)
def runtime_status(): return evaluate_status()

@app.post("/admin/toggles", dependencies=[Depends(auth)], response_model=RuntimeOut)
def set_toggles(body:TogglesIn):
    valid={0,10,20,30,45,60}
    if body.delay_pasta_minutes not in valid or body.delay_schotels_minutes not in valid:
        raise HTTPException(400,"Delay must be one of 0,10,20,30,45,60")
    _save_overrides(body); return evaluate_status()

BASE_DIR=Path(__file__).resolve().parent; ADMIN_UI_DIR=BASE_DIR/"admin_ui"
@app.get("/admin/ui/{path:path}", dependencies=[Depends(auth)])
def admin_ui_any(path:str):
    tgt=ADMIN_UI_DIR/(path or "index.html")
    if tgt.is_dir(): tgt=tgt/"index.html"
    if not tgt.exists(): raise HTTPException(404,"Not found")
    return FileResponse(tgt)

@app.get("/admin/logout")
def admin_logout():
    raise HTTPException(401,"Logged out", headers={"WWW-Authenticate":"Basic"})

@app.get("/healthz")
def healthz():
    ok=True
    try: r.ping()
    except RedisError: ok=False
    return {"ok":ok,"redis":ok}

# ======== STATE MACHINE (Twilio Gather speech, NL) ========
# State keys per CallSid
SM_PREFIX="sm:"
# states: greet -> ask_items -> ask_more -> ask_fulfillment -> ask_phone -> confirm_address -> ask_payment -> confirm_eta -> finalize
ITEMS_KEY="items"; ORDER_KEY="order"
YES=set(["ja","jazeker","klopt","is goed","oke","oké","is prima"])
NO=set(["nee","neem","nee dank","nee hoor","dat was het","is alles","klaar"])

def _sm_key(callsid:str, k:str)->str: return f"{SM_PREFIX}{callsid}:{k}"
def _sm_get(callsid:str, k:str, default=None):
    v=r.get(_sm_key(callsid,k)); return json.loads(v) if v else default
def _sm_set(callsid:str, k:str, val):
    r.setex(_sm_key(callsid,k), 60*30, json.dumps(val) if not isinstance(val,str) else val)
def _sm_del_all(callsid:str):
    # redis scan-delete by pattern
    # lightweight: just set a ttl short; or skip cleanup (30m expiry). Keep simple:
    pass

def say_play(text:str, host:str)->str:
    url=f"https://{host}/tts?text={quote(text)}"
    return f"<Play>{url}</Play>"

def gather_block(next_url:str, prompt:str, host:str, timeout:int=6)->str:
    return f"""
<Gather input="speech" language="nl-NL" action="{next_url}" method="POST" speechTimeout="{timeout}">
  {say_play(prompt, host)}
</Gather>
"""

def parse_yesno(s:str)->Optional[bool]:
    if not s: return None
    t=s.lower().strip()
    for y in YES:
        if y in t: return True
    for n in NO:
        if n in t: return False
    return None

def extract_phone(s:str)->Optional[str]:
    if not s: return None
    d=re.sub(r"\D+","",s)
    if len(d)>=8: 
        if d.startswith("31") and len(d)>=11: d="0"+d[2:]
        return d
    return None

# naive item parsing: zoekt naar bekende woorden
MENU_KEYWORDS = {
  "pizza": "pizza", "margherita":"pizza margherita", "salami":"pizza salami", "hawaii":"pizza hawaii",
  "pasta":"pasta", "bolognese":"pasta bolognese", "carbonara":"pasta carbonara",
  "shoarma":"shoarma", "döner":"döner", "doner":"döner", "kapsalon":"kapsalon",
}
NUM_WORDS = {"een":1,"twee":2,"drie":3,"vier":4,"vijf":5,"zes":6,"zeven":7,"acht":8,"negen":9,"tien":10}

def extract_items(s:str):
    if not s: return []
    t=s.lower()
    qty=1
    for w,n in NUM_WORDS.items():
        if re.search(rf"\b{w}\b", t): qty=n; break
    found=[]
    for k,v in MENU_KEYWORDS.items():
        if k in t: found.append({"name":v,"qty":qty})
    return found

def format_items(items):
    return ", ".join([f"{i['qty']}× {i['name']}" for i in items]) if items else "geen"

# -------- Twilio routes ----------
def twiml(body:str)->Response:
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>', media_type="text/xml")

@app.api_route("/voice/incoming", methods=["GET","POST"])
def voice_incoming(req:Request):
    st=evaluate_status()
    host=os.getenv("RENDER_EXTERNAL_HOSTNAME", req.headers.get("host","localhost"))
    callsid = (req.query_params.get("CallSid") or req.headers.get("X-Twilio-CallSid") or "")
    greeting=select_greeting()
    if st.mode=="closed":
        # alleen begroeting
        return twiml(say_play(greeting,host))
    # start dialog
    _sm_set(callsid,"state","ask_items")
    _sm_set(callsid,ITEMS_KEY,[])
    prompt = greeting + " Zegt u maar, wat wilt u bestellen?"
    return twiml(gather_block("/voice/step", prompt, host))

@app.api_route("/voice/step", methods=["POST"])
def voice_step(req:Request):
    form = dict(req.query_params)
    if req.headers.get("content-type","").startswith("application/x-www-form-urlencoded"):
        form.update({k:v for k,v in (req.body().decode() if False else {}).items()})  # noop
    # FastAPI shortcut:
    # use request.form() async normally; to keep sync, read from headers commonly sent by Twilio:
    # Twilio posts form fields; extract via starlette:
    import urllib.parse
    body = req.scope.get("_body")
    if body is None:
        # read body now
        body_bytes = req._receive()  # unsafe; but we will fallback to state-based handling
    # safer: use toolkit
    # Instead, use starlette to read form:
    # Implement simple parser:
    try:
        raw = req.scope.get("body")
    except Exception:
        raw=None
    # we will use Request.form() properly:
    import anyio
    async def get_form_sync(rq:Request):
        return await rq.form()
    # convert to sync
    with anyio.from_thread.start_blocking_portal() as portal:
        f = portal.call(get_form_sync, req)
    data = {k:v for k,v in f.items()}

    speech = data.get("SpeechResult","") or data.get("Transcription","")
    callsid = data.get("CallSid","")
    host=os.getenv("RENDER_EXTERNAL_HOSTNAME", req.headers.get("host","localhost"))
    state = _sm_get(callsid,"state","ask_items")

    # ---- state handlers ----
    if state=="ask_items":
        its = extract_items(speech)
        items = _sm_get(callsid,ITEMS_KEY,[])
        items.extend(its or [])
        _sm_set(callsid,ITEMS_KEY, items)
        if not items:
            prompt="Ik heb u niet goed verstaan. Welke gerechten wilt u bestellen?"
            return twiml(gather_block("/voice/step", prompt, host))
        _sm_set(callsid,"state","ask_more")
        prompt=f"Dat is genoteerd: {format_items(items)}. Wilt u nog iets toevoegen?"
        return twiml(gather_block("/voice/step", prompt, host))

    if state=="ask_more":
        yn=parse_yesno(speech)
        if yn is None:
            prompt="Wilt u nog iets toevoegen? Zeg alstublieft ja of nee."
            return twiml(gather_block("/voice/step", prompt, host))
        if yn:
            _sm_set(callsid,"state","ask_items")
            prompt="Zegt u maar, wat wilt u er nog bij?"
            return twiml(gather_block("/voice/step", prompt, host))
        _sm_set(callsid,"state","ask_fulfillment")
        prompt="Wilt u dat wij het bezorgen, of komt u het afhalen?"
        return twiml(gather_block("/voice/step", prompt, host))

    if state=="ask_fulfillment":
        t = (speech or "").lower()
        fulf="bezorging" if "bezorg" in t else ("afhalen" if "afhaal" in t or "halen" in t else None)
        if not fulf:
            prompt="Bezorging of afhalen?"
            return twiml(gather_block("/voice/step", prompt, host))
        _sm_set(callsid,"fulfillment",fulf)
        _sm_set(callsid,"state","ask_phone")
        prompt="Welk telefoonnummer mogen we gebruiken voor de bestelling?"
        return twiml(gather_block("/voice/step", prompt, host))

    if state=="ask_phone":
        tel = extract_phone(speech)
        if not tel:
            prompt="Kunt u uw telefoonnummer noemen? Bijvoorbeeld nul zes, twaalf, drieënveertig, zesenvijftig, achtennegentig."
            return twiml(gather_block("/voice/step", prompt, host))
        _sm_set(callsid,"phone",tel)
        # lookup
        _load_customers()
        rec = _customers_idx.get(tel) or (_customers_idx.get(tel[-8:]) if len(tel)>=8 else None)
        if rec:
            addr = f"{rec.get('straat','')} {rec.get('huisnr','')}, {rec.get('postcode','')}"
            _sm_set(callsid,"address",rec)
            _sm_set(callsid,"state","confirm_address")
            prompt=f"Ik heb als adres: {addr}. Klopt dat?"
            return twiml(gather_block("/voice/step", prompt, host))
        else:
            _sm_set(callsid,"state","ask_address")
            prompt="Wat is uw straat en huisnummer?"
            return twiml(gather_block("/voice/step", prompt, host))

    if state=="confirm_address":
        yn=parse_yesno(speech)
        if yn is None:
            addr=_sm_get(callsid,"address",{})
            addr_txt=f"{addr.get('straat','')} {addr.get('huisnr','')}, {addr.get('postcode','')}"
            prompt=f"Klopt dit adres: {addr_txt}? Zeg ja of nee."
            return twiml(gather_block("/voice/step", prompt, host))
        if not yn:
            _sm_set(callsid,"state","ask_address")
            prompt="Geeft u dan alstublieft uw straat, huisnummer en postcode door."
            return twiml(gather_block("/voice/step", prompt, host))
        _sm_set(callsid,"state","ask_payment")
        prompt="Hoe wilt u betalen, contant of met iDeal?"
        return twiml(gather_block("/voice/step", prompt, host))

    if state=="ask_address":
        t=(speech or "")
        # simpele parsing: zoek postcode en huisnr
        pc=re.search(r"\b[1-9]\d{3}\s?[A-Za-z]{2}\b", t)
        hn=re.search(r"\b\d+[A-Za-z]?\b", t)
        street=t
        if pc: street=street.replace(pc.group(0),"")
        if hn: street=street.replace(hn.group(0),"")
        street=street.strip(",. ").title()
        addr={"straat":street, "huisnr":hn.group(0) if hn else "", "postcode":pc.group(0).replace(" ","").upper() if pc else ""}
        _sm_set(callsid,"address",addr)
        _sm_set(callsid,"state","ask_payment")
        prompt=f"Bedankt. Ik heb genoteerd: {addr.get('straat','')} {addr.get('huisnr','')}, {addr.get('postcode','')}. Hoe wilt u betalen, contant of met iDeal?"
        return twiml(gather_block("/voice/step", prompt, host))

    if state=="ask_payment":
        t=(speech or "").lower()
        pay="contant" if "contant" in t or "cash" in t else ("ideal" if "ideal" in t or "i deal" in t else None)
        if not pay:
            prompt="Contant of iDeal?"
            return twiml(gather_block("/voice/step", prompt, host))
        _sm_set(callsid,"payment",pay)
        _sm_set(callsid,"state","confirm_eta")
        # ETA: eenvoudige schatting met vertragingen
        rt=evaluate_status()
        delay=max(rt.delay_pasta_minutes, rt.delay_schotels_minutes)
        base=30  # basis 30 min
        eta=base+delay
        _sm_set(callsid,"eta_min",eta)
        items=_sm_get(callsid,ITEMS_KEY,[])
        fulf=_sm_get(callsid,"fulfillment","bezorging")
        prompt=f"Prima. Samenvatting: {format_items(items)}. {fulf}. Betaling {pay}. Het duurt ongeveer {eta} minuten. Klopt alles zo?"
        return twiml(gather_block("/voice/step", prompt, host))

    if state=="confirm_eta":
        yn=parse_yesno(speech)
        if yn is None:
            prompt="Klopt alles zo? Zeg alstublieft ja of nee."
            return twiml(gather_block("/voice/step", prompt, host))
        _sm_set(callsid,"state","finalize")
        if yn:
            order={
              "items":_sm_get(callsid,ITEMS_KEY,[]),
              "fulfillment":_sm_get(callsid,"fulfillment","bezorging"),
              "phone":_sm_get(callsid,"phone",""),
              "address":_sm_get(callsid,"address",{}),
              "payment":_sm_get(callsid,"payment",""),
              "eta_min":_sm_get(callsid,"eta_min",30),
              "ts":datetime.now(TZ).isoformat()
            }
            r.lpush("orders:queue", json.dumps(order))
            prompt="Dank u wel. Uw bestelling is doorgegeven aan de keuken. Een fijne avond."
            return twiml(say_play(prompt, host))
        else:
            _sm_set(callsid,"state","ask_items")
            prompt="Geen probleem. Zegt u maar opnieuw wat u wilt bestellen."
            return twiml(gather_block("/voice/step", prompt, host))

    # fallback
    _sm_set(callsid,"state","ask_items")
    return twiml(gather_block("/voice/step","Kunt u herhalen wat u wilt bestellen?", host))
