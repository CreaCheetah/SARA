import os, json, csv, re, unicodedata
from datetime import datetime, time, timedelta
from typing import Dict, Any, List
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))

# ---------- Files ----------
REPO_ROOT = Path(__file__).resolve().parent
MENU_PATH = Path(os.getenv("MENU_PATH", REPO_ROOT / "menu_ristorante_adam.json"))
CONFIG_DELIVERY_PATH = Path(os.getenv("CONFIG_DELIVERY", REPO_ROOT / "config_delivery.json"))
CUSTOMER_CSV = os.getenv("CUSTOMER_CSV", "/mnt/data/klanten.csv")

# ---------- Redis ----------
def _redis():
    from redis import Redis
    return Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)

# ---------- Hours ----------
OPEN_START, OPEN_END = time(16, 0), time(22, 0)
DEL_START,  DEL_END  = time(17, 0), time(21, 30)

# ---------- Helpers: text norm ----------
def _norm_txt(s: str) -> str:
    s = (s or "").lower().strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")  # strip accenten
    s = s.replace("’", "'").replace("‘", "'").replace("`", "'")
    s = s.replace("’s", "s")  # pizza’s -> pizzas
    s = re.sub(r"[^a-z0-9\s\-\&]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def _tokens(s: str) -> List[str]:
    return [t for t in _norm_txt(s).split() if t]

# ---------- Menu loader (supports flat and categories->items) ----------
def _load_menu() -> List[Dict[str, Any]]:
    path = MENU_PATH
    out: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return out

    def _add_item(it: dict):
        name = it.get("name") or it.get("naam") or ""
        price = it.get("price") or it.get("prijs") or 0
        try:
            price = float(price)
        except Exception:
            price = 0.0
        if not name or price <= 0:
            return
        code = it.get("code") or it.get("id") or name.lower().replace(" ", "_")[:24]
        norm = _norm_txt(name)
        out.append({
            "code": code,
            "name": name,
            "price": price,
            "norm": norm,
            "tokens": [t for t in norm.split() if len(t) >= 3]
        })

    # 3 mogelijke structuren: [items], {"categories":[...]} of [{"items":[...]}...]
    if isinstance(data, dict) and "categories" in data:
        for cat in data.get("categories", []):
            for it in cat.get("items", []):
                _add_item(it)
    elif isinstance(data, list):
        # lijst met items of met categorieobjecten
        for elem in data:
            if isinstance(elem, dict) and "items" in elem:
                for it in elem.get("items", []):
                    _add_item(it)
            elif isinstance(elem, dict):
                _add_item(elem)
    elif isinstance(data, dict) and "items" in data:
        for it in data.get("items", []):
            _add_item(it)

    return out

MENU = _load_menu()

# ---------- Config ----------
def _jload(path: Path, fb: dict) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fb

CFG = _jload(CONFIG_DELIVERY_PATH, {
    "zones": [],
    "sla": {"pickup_minutes": 15, "pickup_combo_minutes": 30, "delivery_minutes": 60}
})

# ---------- Overrides ----------
OVERRIDES_KEY = "mada:overrides"
DEFAULT_OVERRIDES = {
    "bot_enabled": True,
    "pasta_available": True,
    "delay_pasta_minutes": 0,
    "delay_schotels_minutes": 0,
    "is_open_override": "auto",
    "delivery_enabled": False,
}
OVR_TTL = int(os.getenv("OVERRIDES_TTL_MIN", "180"))

def _ovr_load() -> dict:
    try:
        r = _redis(); raw = r.get(OVERRIDES_KEY)
        if not raw: return DEFAULT_OVERRIDES.copy()
        data = json.loads(raw)
        out = DEFAULT_OVERRIDES.copy()
        out.update({k: v for k, v in data.items() if k in out})
        return out
    except Exception:
        return DEFAULT_OVERRIDES.copy()

def _ovr_save(body: dict) -> dict:
    def _norm_int(v):
        try: n = int(v)
        except Exception: n = 0
        allowed = [0,10,20,30,45,60]
        return min(allowed, key=lambda a: abs(a-n))
    body["delay_pasta_minutes"] = _norm_int(body.get("delay_pasta_minutes", 0))
    body["delay_schotels_minutes"] = _norm_int(body.get("delay_schotels_minutes", 0))
    saved = DEFAULT_OVERRIDES.copy()
    saved.update({k: body.get(k, saved[k]) for k in saved.keys()})
    try:
        r = _redis(); r.set(OVERRIDES_KEY, json.dumps(saved, ensure_ascii=False), ex=OVR_TTL*60)
    except Exception:
        pass
    return saved

# ---------- Runtime ----------
def _auto(now=None):
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    t = now.time()
    open_now = OPEN_START <= t < OPEN_END
    delivery_now = OPEN_START <= t < OPEN_END and (DEL_START <= t < DEL_END)
    return {"now": now, "mode": "open" if open_now else "closed", "delivery_window": delivery_now}

def runtime_status():
    ov = _ovr_load(); au = _auto()
    mode = au["mode"]
    if ov.get("is_open_override") == "open": mode = "open"
    elif ov.get("is_open_override") == "closed": mode = "closed"
    delivery_enabled = False if mode == "closed" else bool(ov.get("delivery_enabled") or au["delivery_window"])
    return {
        "now": au["now"].isoformat(),
        "mode": mode,
        "delivery_enabled": delivery_enabled,
        "window": {"open":"16:00","delivery":"17:00-21:30","close":"22:00"},
        "bot_enabled": bool(ov.get("bot_enabled", True)),
        "pasta_available": bool(ov.get("pasta_available", True)),
        "delay_pasta_minutes": int(ov.get("delay_pasta_minutes", 0)),
        "delay_schotels_minutes": int(ov.get("delay_schotels_minutes", 0)),
    }

def is_closed() -> bool:
    return runtime_status()["mode"] == "closed"

def greeting(P):
    st = runtime_status()
    if st["mode"] == "open":
        now = datetime.now(TZ).time()
        if now < time(12, 0): return P["greet_open_morning"]
        elif now < time(18, 0): return P["greet_open_afternoon"]
        else: return P["greet_open_evening"]
    return P["greet_closed"]

# ---------- Call state ----------
CALL_TTL = 2*3600
def _ck(sid: str) -> str: return f"call:{sid}"

def _getc(sid: str) -> dict:
    try:
        r = _redis(); raw = r.get(_ck(sid))
        if raw: return json.loads(raw)
    except Exception:
        pass
    return {"state":"greet","items":[],"total":0.0,"fulfilment":None,"customer":{},"payment":None}

def _savec(sid: str, data: dict):
    try:
        r = _redis(); r.set(_ck(sid), json.dumps(data, ensure_ascii=False), ex=CALL_TTL)
    except Exception:
        pass

# ---------- Parser: quantities + fuzzy menu match ----------
NUMWORDS = {
    "een":1,"één":1,"1":1,
    "twee":2,"2":2,
    "drie":3,"3":3,
    "vier":4,"4":4,
    "vijf":5,"5":5,
    "zes":6,"6":6,
    "zeven":7,"7":7,
    "acht":8,"8":8,
    "negen":9,"9":9,
    "tien":10,"10":10
}

def _num_from_word(w: str) -> int | None:
    return NUMWORDS.get(w)

def _split_phrases(txt: str) -> List[str]:
    # split op natuurlijke verbindingswoorden
    return [p for p in re.split(r"\s*(?:,| en | plus | & | en dan )\s*", txt) if p]

def _hawai_norm(s: str) -> str:
    # normaliseer hawai-varianten; werkt ook voor “hawaï”, “hawaii”
    s = s.replace("hawaii", "hawai").replace("hawa i", "hawai")
    s = s.replace("hawaï", "hawai").replace("hawaÃ¯", "hawai")
    return s

def _match_menu_segment(seg: str) -> Dict[str, Any] | None:
    seg = _hawai_norm(_norm_txt(seg))
    if not seg: return None

    # 1) directe substring-match op genormaliseerde naam
    for it in MENU:
        n = _hawai_norm(it["norm"])
        if n in seg or seg in n:
            return it

    # 2) token-overlap (minstens 1 token)
    segtoks = [t for t in seg.split() if len(t) >= 3]
    best = None; best_score = 0
    for it in MENU:
        toks = it["tokens"]
        inter = len(set(toks) & set(segtoks))
        if inter > best_score:
            best = it; best_score = inter
    if best_score >= 1:
        return best

    return None

def _parse_items(utt: str) -> List[dict]:
    txt = _hawai_norm(_norm_txt(utt))
    res: List[dict] = []
    used = set()
    parts = _split_phrases(txt)

    # 1) "2 margherita", "twee quattro formaggi"
    for p in parts:
        m = re.match(r"^((\d+)|([a-zé]+))\s+(.+)$", p)
        if m:
            qty = int(m.group(2)) if m.group(2) else _num_from_word(m.group(3) or "")
            tail = m.group(4) if m else ""
            if qty:
                hit = _match_menu_segment(tail)
                if hit and hit["norm"] not in used:
                    used.add(hit["norm"])
                    res.append({"code": hit["code"], "name": hit["name"], "price": hit["price"], "qty": max(1, qty)})

    # 2) zonder hoeveelheid: "margherita", "quattro formaggi"
    if not res:
        for p in parts:
            hit = _match_menu_segment(p)
            if hit:
                res.append({"code": hit["code"], "name": hit["name"], "price": hit["price"], "qty": 1})

    return res

# ---------- Money/time ----------
def _items_text(items: List[dict]) -> str:
    return ", ".join([f'{i["qty"]}× {i["name"]}' for i in items]) if items else "geen items"

def _total(items: List[dict]) -> float:
    return round(sum(i["qty"] * float(i["price"]) for i in items), 2)

def _delivery_fee(pc: str) -> float:
    if not pc: return 0.0
    p = pc.replace(" ","").upper()
    for z in CFG.get("zones", []):
        if any(p.startswith(xx.replace(" ","").upper()) for xx in z.get("postcodes", [])):
            try: return float(z.get("fee", 0))
            except Exception: return 0.0
    return 0.0

def _eta_minutes(kind: str, d_pasta: int, d_schotels: int) -> int:
    base = CFG.get("sla", {}).get("delivery_minutes" if kind=="delivery" else "pickup_minutes", 30)
    return int(base) + int(max(d_pasta, d_schotels))

def _fmt_eur(x: float) -> str:
    return f"{x:0.2f}".replace(".", ",")

# ---------- Public ----------
class FlowManager:
    @staticmethod
    def runtime_status(): return runtime_status()
    @staticmethod
    def save_overrides_api(body: dict): return {"ok": True, "saved": _ovr_save(body), "ttl_minutes": OVR_TTL}
    @staticmethod
    def is_closed(): return is_closed()
    @staticmethod
    def greeting(P): return greeting(P)

    @staticmethod
    def handle_utterance(sid: str, speech: str, P: dict) -> dict:
        s = _getc(sid)
        utt = (speech or "").strip().lower()
        utt_norm = _norm_txt(utt)

        def out(msgs: List[str], nxt: str):
            s["state"] = nxt; _savec(sid, s); return {"messages": msgs, "next": nxt}

        # greet -> vraag om bestelling
        if s["state"] in ("greet", None):
            return out([P["ask_items"]], "ask_items")

        # expliciete start
        if any(k in utt_norm for k in ["ik wil bestellen", "bestelling plaatsen", "mag ik wat bestellen"]):
            return out([P["reply_start_order"]], "ask_items")

        # items verzamelen
        if s["state"] in ("ask_items", "collecting"):
            items = _parse_items(utt_norm)

            # klant zegt "pizza's" maar geen soort
            if not items and re.search(r"\bpizza?s?\b", utt_norm):
                return out([P["ask_pizza_which"]], "ask_items")

            if items:
                s["items"] += items
                _savec(sid, s)
                last = items[-1]
                return out([P["item_added"].format(qty=last["qty"], name=last["name"]), P["ask_items_more"]], "confirm_more")

            return out([P["ask_items"]], "ask_items")

        # confirm_more
        if s["state"] == "confirm_more":
            if any(k in utt_norm for k in ["ja","nog","meer","toevoegen"]):
                return out([P["ask_items"]], "ask_items")
            if any(k in utt_norm for k in ["nee","dat is alles","klaar","niets"]):
                s["total"] = _total(s["items"]); _savec(sid, s)
                return out([P["confirm_items"].format(items=_items_text(s["items"])), P["ask_items_confirm_ok"]], "confirm_summary")
            # klant noemt hier alsnog extra items
            items = _parse_items(utt_norm)
            if items:
                s["items"] += items; _savec(sid, s)
                last = items[-1]
                return out([P["item_added"].format(qty=last["qty"], name=last["name"]), P["ask_items_more"]], "confirm_more")
            return out([P["ask_items_more"]], "confirm_more")

        # confirm_summary
        if s["state"] == "confirm_summary":
            if any(k in utt_norm for k in ["ja","klopt","correct"]):
                amt = s.get("total", 0.0)
                return out([P["total_after_confirm"].format(amount=int(round(amt))), P["ask_fulfilment"]], "fulfilment")
            if any(k in utt_norm for k in ["nee","klopt niet","anders"]):
                return out([P["ask_items"]], "ask_items")
            return out([P["ask_items_confirm_ok"]], "confirm_summary")

        # fulfilment
        if s["state"] == "fulfilment":
            if any(k in utt_norm for k in ["afhaal","afhalen","ophalen"]):
                st = runtime_status()
                mins = _eta_minutes("pickup", st["delay_pasta_minutes"], st["delay_schotels_minutes"])
                ready = (datetime.now(TZ) + timedelta(minutes=mins)).strftime("%H:%M")
                return {"messages":[P["pickup_eta"].format(time=ready), P["closing_pickup"]], "next":"end"}
            if any(k in utt_norm for k in ["bezorg","bezorgen","thuis"]):
                return out([P["ask_phone_for_delivery"]], "phone")
            return out([P["ask_fulfilment"]], "fulfilment")

        # phone → CRM
        if s["state"] == "phone":
            tel = "".join(ch for ch in utt if ch.isdigit())
            s.setdefault("customer", {})["tel"] = tel
            found = None
            if os.path.exists(CUSTOMER_CSV) and tel:
                try:
                    with open(CUSTOMER_CSV, newline="", encoding="utf-8") as f:
                        r = csv.DictReader(f)
                        for row in r:
                            phones = [
                                ''.join(ch for ch in (row.get("phone") or "") if ch.isdigit()),
                                ''.join(ch for ch in (row.get("mobile") or "") if ch.isdigit())
                            ]
                            if tel in phones:
                                found = {
                                    "postcode": row.get("postcode") or "",
                                    "straat": row.get("street1") or "",
                                    "huisnr": row.get("house_number") or "",
                                }
                                break
                except Exception:
                    found = None
            if found and (found["straat"] or found["postcode"]):
                s["customer"].update(found); _savec(sid, s)
                return out([P["confirm_lookup_found"].format(straat=found["straat"], huisnr=found["huisnr"], postcode=found["postcode"])], "crm_confirm")
            return out([P["confirm_lookup_missing"]], "address")

        # crm_confirm
        if s["state"] == "crm_confirm":
            if any(k in utt_norm for k in ["ja","klopt","correct"]):
                st = runtime_status()
                mins = _eta_minutes("delivery", st["delay_pasta_minutes"], st["delay_schotels_minutes"])
                ready = (datetime.now(TZ) + timedelta(minutes=mins)).strftime("%H:%M")
                tot = _total(s["items"])
                fee = _delivery_fee(s.get("customer",{}).get("postcode",""))
                tot = int(round(tot + fee))
                return {"messages":[P["delivery_eta"].format(time=ready), P["total_after_confirm"].format(amount=tot), P["closing_delivery"]], "next":"end"}
            if any(k in utt_norm for k in ["nee","klopt niet","anders"]):
                return out([P["confirm_lookup_missing"]], "address")
            c = s.get("customer",{})
            return out([P["confirm_lookup_found"].format(straat=c.get("straat",""), huisnr=c.get("huisnr",""), postcode=c.get("postcode",""))], "crm_confirm")

        # address
        if s["state"] == "address":
            pc = re.search(r"\b\d{4}\s?[a-zA-Z]{2}\b", utt)
            hn = re.search(r"\b(\d{1,4}[a-zA-Z]?)\b", utt)
            if pc: s["customer"]["postcode"] = pc.group(0).replace(" ","").upper()
            if hn: s["customer"]["huisnr"] = hn.group(1)
            _savec(sid, s)
            if s["customer"].get("postcode") and s["customer"].get("huisnr"):
                st = runtime_status()
                mins = _eta_minutes("delivery", st["delay_pasta_minutes"], st["delay_schotels_minutes"])
                ready = (datetime.now(TZ) + timedelta(minutes=mins)).strftime("%H:%M")
                tot = _total(s["items"])
                fee = _delivery_fee(s.get("customer",{}).get("postcode",""))
                tot = int(round(tot + fee))
                return {"messages":[P["delivery_eta"].format(time=ready), P["total_after_confirm"].format(amount=tot), P["closing_delivery"]], "next":"end"}
            return out([P["ask_postcode_house"]], "address")

        # fallback
        return {"messages":[P["fallback1"]], "next":"ask_items"}
