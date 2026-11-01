"""Telegram-бот «Ответы на ваши вопросы таро» + мини-апп. Только стандартная библиотека."""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
from contextlib import closing
import urllib.parse
import urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cards

ROOT = Path(__file__).parent


def load_env():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
if not WEBAPP_URL:  # хостинги сами сообщают публичный адрес
    host = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if host:
        WEBAPP_URL = "https://" + host
PORT = int(os.environ.get("PORT", "8080"))
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "").rstrip("/")  # например https://имя.github.io
PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")
SYSTEM_PROMPT = (ROOT / "prompt.md").read_text(encoding="utf-8")
HISTORY_LIMIT = 10
COOLDOWN_SEC = 3
FREE_REQUESTS = 3  # бесплатных запросов на пользователя, дальше нужен тариф
# цены в рублях; оплата через Telegram (провайдер платежей из PAYMENT_PROVIDER_TOKEN)
PLANS = {
    "pack5": {"button": "5 запросов — 129 ₽", "title": "5 запросов", "description": "Пять запросов к таро",
              "price": 129, "requests": 5, "days": 0},
    "pack10": {"button": "10 запросов — 169 ₽", "title": "10 запросов", "description": "Десять запросов к таро",
               "price": 169, "requests": 10, "days": 0},
    "unlimited": {"button": "Безлимит на 30 дней — 499 ₽", "title": "Безлимит на 30 дней",
                  "description": "Безлимитные запросы к таро на 30 дней", "price": 499, "requests": 0, "days": 30},
}

class QuotaExceeded(Exception):
    pass


histories = {}  # user_id -> [{"role": "user"|"assistant", "text": str}]
last_call = {}
profiles = {}  # user_id -> "YYYY-MM-DD"
last_readings = {}  # user_id -> последний расклад (для followup и deep_dive)
state_lock = threading.Lock()


# ---------- База данных (пользователи, расклады, оплаты) ----------

DB_PATH = Path(os.environ.get("DATA_DIR", str(ROOT))) / "data.db"
ADMIN_COMMAND = "/adminsnetlie"


def db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with closing(db()) as c, c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, language TEXT,
                first_seen INTEGER, last_seen INTEGER, readings INTEGER DEFAULT 0, birth_date TEXT);
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, ts INTEGER, kind TEXT, spread TEXT, question TEXT, title TEXT);
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, ts INTEGER, amount REAL, currency TEXT,
                status TEXT, provider TEXT, note TEXT);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE INDEX IF NOT EXISTS idx_readings_uid ON readings(uid, ts);
            CREATE INDEX IF NOT EXISTS idx_payments_uid ON payments(uid, ts);
        """)
        # колонки тарифов появились позже, добавляем их в уже существующую базу
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        for name in ("free_used", "credits", "unlimited_until"):
            if name not in cols:
                c.execute(f"ALTER TABLE users ADD COLUMN {name} INTEGER DEFAULT 0")


def touch_user(u, birth=None):
    now = int(time.time())
    b = parse_birth(birth) if birth else None
    with closing(db()) as c, c:
        c.execute(
            """INSERT INTO users (id, username, first_name, last_name, language, first_seen, last_seen, birth_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name,
                 last_name=excluded.last_name, language=excluded.language, last_seen=excluded.last_seen,
                 birth_date=COALESCE(excluded.birth_date, users.birth_date)""",
            (u["id"], u.get("username"), u.get("first_name"), u.get("last_name"), u.get("language_code"),
             now, now, b.isoformat() if b else None))


def log_reading(uid, kind, spread, question, title):
    with closing(db()) as c, c:
        c.execute("INSERT INTO readings (uid, ts, kind, spread, question, title) VALUES (?,?,?,?,?,?)",
                  (uid, int(time.time()), kind, spread, question[:300], title))
        if kind == "reading":
            c.execute("UPDATE users SET readings = readings + 1 WHERE id = ?", (uid,))


def quota(uid):
    with closing(db()) as c:
        row = c.execute("SELECT free_used, credits, unlimited_until FROM users WHERE id=?", (uid,)).fetchone()
    used = row["free_used"] or 0 if row else 0
    return {"free": max(FREE_REQUESTS - used, 0), "credits": row["credits"] or 0 if row else 0,
            "unlimited_until": row["unlimited_until"] or 0 if row else 0}


def has_quota(uid):
    q = quota(uid)
    return q["unlimited_until"] > time.time() or q["free"] > 0 or q["credits"] > 0


def spend_request(uid):
    """Сначала списываются бесплатные запросы, потом купленные. По безлимиту ничего не списывается."""
    with closing(db()) as c, c:
        row = c.execute("SELECT free_used, credits, unlimited_until FROM users WHERE id=?", (uid,)).fetchone()
        if row is None or (row["unlimited_until"] or 0) > time.time():
            return
        if (row["free_used"] or 0) < FREE_REQUESTS:
            c.execute("UPDATE users SET free_used = free_used + 1 WHERE id=?", (uid,))
        elif (row["credits"] or 0) > 0:
            c.execute("UPDATE users SET credits = credits - 1 WHERE id=?", (uid,))


def grant_plan(uid, plan_id, charge_id, total_kop):
    plan = PLANS[plan_id]
    if total_kop != plan["price"] * 100:
        return False
    now = int(time.time())
    with closing(db()) as c, c:
        # Telegram может прислать одно и то же успешное платёж дважды: учитываем его один раз
        if c.execute("SELECT 1 FROM payments WHERE provider='telegram' AND note LIKE ?", (f"%{charge_id}",)).fetchone():
            return False
        c.execute("INSERT INTO payments (uid, ts, amount, currency, status, provider, note) VALUES (?,?,?,?,?,?,?)",
                  (uid, now, plan["price"], "RUB", "paid", "telegram", f"{plan_id} {charge_id}"))
        if plan["days"]:
            c.execute("UPDATE users SET unlimited_until = MAX(unlimited_until, ?) + ? WHERE id=?",
                      (now, plan["days"] * 86400, uid))
        else:
            c.execute("UPDATE users SET credits = credits + ? WHERE id=?", (plan["requests"], uid))
    return True


def get_admin_id():
    env = os.environ.get("ADMIN_ID", "").strip()
    if env.isdigit():
        return int(env)
    with closing(db()) as c:
        row = c.execute("SELECT value FROM meta WHERE key='admin_id'").fetchone()
    return int(row["value"]) if row else None


def claim_admin(uid):
    """Первый, кто отправил секретную команду, становится администратором."""
    with closing(db()) as c, c:
        c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('admin_id', ?)", (str(uid),))
    return get_admin_id() == uid


def admin_overview():
    lt = time.localtime()
    day0 = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    with closing(db()) as c:
        def one(q, *a):
            return c.execute(q, a).fetchone()[0]
        stats = {
            "users": one("SELECT COUNT(*) FROM users"),
            "new_today": one("SELECT COUNT(*) FROM users WHERE first_seen >= ?", day0),
            "active_7d": one("SELECT COUNT(*) FROM users WHERE last_seen >= ?", int(time.time()) - 7 * 86400),
            "readings": one("SELECT COUNT(*) FROM readings WHERE kind='reading'"),
            "readings_today": one("SELECT COUNT(*) FROM readings WHERE kind='reading' AND ts >= ?", day0),
            "messages": one("SELECT COUNT(*) FROM readings"),
            "paid_total": one("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='paid'"),
            "paid_count": one("SELECT COUNT(*) FROM payments WHERE status='paid'"),
        }
        series = {}
        queries = (("users", "SELECT date(first_seen,'unixepoch','localtime') d, COUNT(*) n FROM users WHERE first_seen >= ? GROUP BY d"),
                   ("readings", "SELECT date(ts,'unixepoch','localtime') d, COUNT(*) n FROM readings WHERE kind='reading' AND ts >= ? GROUP BY d"))
        for name, q in queries:
            got = {r["d"]: r["n"] for r in c.execute(q, (day0 - 13 * 86400,))}
            series[name] = []
            for i in range(13, -1, -1):
                d = time.strftime("%Y-%m-%d", time.localtime(day0 - i * 86400 + 3600))
                series[name].append({"d": d, "n": got.get(d, 0)})
        users = [dict(r) for r in c.execute("SELECT * FROM users ORDER BY last_seen DESC LIMIT 1000")]
        payments = [dict(r) for r in c.execute(
            """SELECT p.*, u.username, u.first_name FROM payments p LEFT JOIN users u ON u.id = p.uid
               ORDER BY p.ts DESC LIMIT 500""")]
    return {"stats": stats, "series": series, "users": users, "payments": payments}


def admin_user(uid):
    with closing(db()) as c:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        rs = [dict(r) for r in c.execute("SELECT * FROM readings WHERE uid=? ORDER BY ts DESC LIMIT 30", (uid,))]
        ps = [dict(r) for r in c.execute("SELECT * FROM payments WHERE uid=? ORDER BY ts DESC LIMIT 30", (uid,))]
    return {"user": dict(u) if u else None, "readings": rs, "payments": ps}


# ---------- Telegram Bot API ----------

def tg(method, **params):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def send_html(chat_id, text):
    text = text[:3500]
    try:
        tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")
    except urllib.error.HTTPError:
        # битая HTML-разметка от модели — шлём как обычный текст
        tg("sendMessage", chat_id=chat_id, text=re.sub(r"<[^>]+>", "", text))


def webapp_markup():
    return {"inline_keyboard": [[{"text": "🔮 Открыть таро", "web_app": {"url": WEBAPP_URL}}]]}


def start_markup():
    rows = [[{"text": "🔮 Открыть таро", "web_app": {"url": WEBAPP_URL}}]] if WEBAPP_URL else []
    rows.append([{"text": "💳 Тарифы", "callback_data": "tariffs"}])
    return {"inline_keyboard": rows}


def send_tariffs(chat_id, uid, intro=""):
    q = quota(uid)
    if q["unlimited_until"] > time.time():
        status = "Безлимит активен до " + time.strftime("%d.%m.%Y", time.localtime(q["unlimited_until"]))
    else:
        status = f"Бесплатных запросов осталось: {q['free']}. Купленных: {q['credits']}."
    text = "\n".join(p for p in (intro, status, "Выберите тариф:") if p)
    tg("sendMessage", chat_id=chat_id, text=text, reply_markup={"inline_keyboard": [
        [{"text": p["button"], "callback_data": "buy:" + k}] for k, p in PLANS.items()]})


def invoice_fields(plan_id):
    token = os.environ.get("PAYMENT_PROVIDER_TOKEN", "")
    if not token:
        raise ValueError("Оплата скоро появится. Пока тарифы можно только посмотреть.")
    plan = PLANS[plan_id]
    return {"title": plan["title"], "description": plan["description"], "payload": plan_id,
            "provider_token": token, "currency": "RUB", "prices": [{"label": plan["title"], "amount": plan["price"] * 100}]}


def send_invoice(chat_id, plan_id):
    try:
        tg("sendInvoice", chat_id=chat_id, **invoice_fields(plan_id))
    except ValueError as e:
        tg("sendMessage", chat_id=chat_id, text=str(e))


def quota_info(uid):
    return {**quota(uid), "plans": [{"id": k, "title": p["title"], "description": p["description"], "price": p["price"]}
                                    for k, p in PLANS.items()]}


def handle_callback(cq):
    tg("answerCallbackQuery", callback_query_id=cq["id"])
    chat_id, uid, data = cq["message"]["chat"]["id"], cq["from"]["id"], cq.get("data", "")
    if data == "tariffs":
        send_tariffs(chat_id, uid)
    elif data.startswith("buy:") and data[4:] in PLANS:
        send_invoice(chat_id, data[4:])


def handle_payment(chat_id, uid, pay):
    plan_id = pay.get("invoice_payload", "")
    if plan_id in PLANS and grant_plan(uid, plan_id, pay["telegram_payment_charge_id"], pay["total_amount"]):
        send_tariffs(chat_id, uid, f"Оплата прошла: «{PLANS[plan_id]['title']}» подключён. Спасибо!")


def handle_update(upd):
    if "pre_checkout_query" in upd:  # Telegram спрашивает перед списанием денег
        q = upd["pre_checkout_query"]
        tg("answerPreCheckoutQuery", pre_checkout_query_id=q["id"], ok=q["invoice_payload"] in PLANS)
        return
    if "callback_query" in upd:
        return handle_callback(upd["callback_query"])
    msg = upd.get("message")
    if not msg:
        return
    chat_id, uid = msg["chat"]["id"], msg["from"]["id"]
    touch_user(msg["from"])
    if "successful_payment" in msg:
        return handle_payment(chat_id, uid, msg["successful_payment"])
    if "text" not in msg:
        return
    text = msg["text"]
    if text.split("@")[0].strip() == ADMIN_COMMAND:
        # секретная команда: чужим не отвечаем вообще
        admin = get_admin_id()
        if admin is None and claim_admin(uid):
            admin = uid
        if admin == uid and WEBAPP_URL:
            tg("sendMessage", chat_id=chat_id, text="Админ-панель готова.",
               reply_markup={"inline_keyboard": [[{"text": "🛠 Открыть админ-панель", "web_app": {"url": WEBAPP_URL + "/admin.html"}}]]})
        return
    if text.startswith("/start"):
        hello = "Добро пожаловать в «Ответы на ваши вопросы таро». Откройте приложение, выберите расклад и задайте вопрос."
        note = "" if WEBAPP_URL else "\n\n(Адрес мини-аппа ещё не настроен.)"
        tg("sendMessage", chat_id=chat_id, text=hello + note, reply_markup=start_markup())
        return
    if text.startswith("/tariffs") or text.startswith("/buy"):
        return send_tariffs(chat_id, uid)
    if text.startswith("/"):
        return
    # обычное сообщение в чате: разговор без раскладки
    tg("sendChatAction", chat_id=chat_id, action="typing")
    try:
        res = consult(uid, text, request_type="followup")
    except QuotaExceeded:
        return send_tariffs(chat_id, uid, "Бесплатные запросы закончились. Выберите тариф:")
    except Exception as e:  # noqa: BLE001
        print("consult error:", e)
        tg("sendMessage", chat_id=chat_id, text="Сейчас не получается ответить, попробуйте чуть позже.")
        return
    kwargs = {"reply_markup": webapp_markup()} if WEBAPP_URL else {}
    tg("sendMessage", chat_id=chat_id, text=(res["reply"] or res["intro"] or "Откройте приложение, чтобы сделать расклад.")[:4000], **kwargs)


def poll_loop():
    offset = 0
    while True:
        try:
            r = tg("getUpdates", offset=offset, timeout=30,
                   allowed_updates=["message", "callback_query", "pre_checkout_query"])
            for upd in r["result"]:
                offset = upd["update_id"] + 1
                try:
                    handle_update(upd)
                except Exception as e:  # noqa: BLE001
                    print("update error:", e)
        except Exception as e:  # noqa: BLE001
            print("poll error:", e)
            time.sleep(5)


# ---------- Дата рождения ----------

# (месяц, день начала знака, название); до 20 января — Козерог
ZODIAC = [(1, 20, "Водолей"), (2, 19, "Рыбы"), (3, 21, "Овен"), (4, 20, "Телец"), (5, 21, "Близнецы"),
          (6, 21, "Рак"), (7, 23, "Лев"), (8, 23, "Дева"), (9, 23, "Весы"), (10, 23, "Скорпион"),
          (11, 23, "Стрелец"), (12, 22, "Козерог")]


def parse_birth(value):
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        return None
    return d if date(1900, 1, 1) <= d <= date.today() else None


def zodiac(d):
    name = "Козерог"
    for m, day, n in ZODIAC:
        if (d.month, d.day) >= (m, day):
            name = n
    return name


def life_path(d):
    n = sum(int(c) for c in d.strftime("%Y%m%d"))
    while n > 9 and n not in (11, 22, 33):
        n = sum(int(c) for c in str(n))
    return n


# ---------- LLM ----------

def http_json(url, headers, body, retried=False):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={**headers, "User-Agent": "tarot-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        if e.code == 429 and not retried:  # лимит токенов в минуту у бесплатного тарифа: ждём и повторяем один раз
            m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", text)
            wait = (int(m.group(1) or 0) * 60 + float(m.group(2))) if m else 20
            if wait <= 45:
                time.sleep(wait + 1)
                return http_json(url, headers, body, retried=True)
        raise RuntimeError(f"LLM HTTP {e.code}: {text[:500]}") from None


def call_llm(user_json):
    if PROVIDER == "openai":
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        data = http_json(
            f"{base}/chat/completions",
            {"Content-Type": "application/json", "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"},
            {"model": os.environ.get("OPENAI_MODEL", "gpt-4o"), "max_tokens": 4000,
             "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": user_json}]},
        )
        return data["choices"][0]["message"]["content"]
    data = http_json(
        "https://api.anthropic.com/v1/messages",
        {"Content-Type": "application/json", "anthropic-version": "2023-06-01",
         "x-api-key": os.environ.get("ANTHROPIC_API_KEY", "")},
        {"model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"), "max_tokens": 4000,
         "system": SYSTEM_PROMPT, "messages": [{"role": "user", "content": user_json}]},
    )
    return "".join(b.get("text", "") for b in data["content"])


MODES = ("reading", "deep_dive", "chat", "decline")


def _strs(v, limit):
    return [str(x) for x in v][:limit] if isinstance(v, list) else []


def parse_model_json(raw):
    raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}")
    try:
        obj = json.loads(raw[start:end + 1])
        if not isinstance(obj, dict):
            raise ValueError
    except ValueError:
        return {"mode": "chat", "title": "", "reply": raw, "telegram_message": ""}
    mode = obj.get("mode") if obj.get("mode") in MODES else "chat"
    res = {k: str(obj.get(k) or "") for k in ("title", "intro", "synthesis", "closing_question", "deep", "reply", "telegram_message")}
    res["title"] = res["title"][:60]
    res["mode"] = mode
    res["advice"] = _strs(obj.get("advice"), 3)
    res["followups"] = _strs(obj.get("followups"), 3)
    res["cards"] = [
        {"position": str(c.get("position", "")), "card": str(c.get("card", "")),
         "orientation": "reversed" if c.get("orientation") == "reversed" else "upright",
         "keywords": _strs(c.get("keywords"), 3), "meaning": str(c.get("meaning", ""))}
        for c in obj.get("cards", []) if isinstance(c, dict)
    ] if isinstance(obj.get("cards"), list) else []
    return res


def plain_text(res):
    """Текстовая версия ответа: для previous_reading и истории диалога."""
    if res["mode"] == "reading":
        parts = [res["intro"]] + [f"{c['position']}: {c['card']} ({'перевёрнутая' if c['orientation'] == 'reversed' else 'прямая'}) — {c['meaning']}"
                                  for c in res["cards"]]
        parts += [res["synthesis"], "Совет: " + "; ".join(res["advice"])]
    elif res["mode"] == "deep_dive":
        parts = [res["deep"]]
    else:
        parts = [res["reply"]]
    joined = "\n".join(p for p in parts if p)
    return joined.replace("**", "")[:3000]


def consult(uid, question, spread="", drawn=(), birth=None, request_type="reading", topic=""):
    with state_lock:
        now = time.time()
        if now - last_call.get(uid, 0) < COOLDOWN_SEC:
            raise PermissionError("too_fast")
        if not has_quota(uid):
            raise QuotaExceeded()
        last_call[uid] = now
        history = list(histories.get(uid, []))
        last = last_readings.get(uid)
        d = parse_birth(birth) if birth else None
        if d:
            profiles[uid] = d.isoformat()
        elif uid in profiles:
            d = parse_birth(profiles[uid])
    if request_type == "deep_dive" and not last:
        raise LookupError("no_reading")
    if request_type == "reading":
        card_lines = [f"{c['position']}: {c['name']}, {'перевёрнутая' if c['reversed'] else 'прямая'}" for c in drawn]
        previous = ""
    else:
        spread, card_lines, previous = (last["spread"], last["cards"], last["text"]) if last else ("", [], "")
        if request_type == "deep_dive":
            question = last["question"]
    payload = {"request_type": request_type, "required_mode": {"reading": "reading", "followup": "chat", "deep_dive": "deep_dive"}[request_type],
               "question": question, "spread": spread, "cards": card_lines}
    if topic and request_type == "reading":
        payload["topic"] = topic
    if d:
        payload["zodiac"] = zodiac(d)
        payload["life_path"] = life_path(d)
    if previous:
        payload["previous_reading"] = previous
    payload["history"] = history
    res = parse_model_json(call_llm(json.dumps(payload, ensure_ascii=False)))
    if request_type == "followup" and res["mode"] not in ("chat", "decline"):
        res["mode"] = "chat"
        res["reply"] = res["reply"] or res["deep"] or res["synthesis"] or res["intro"]
    if request_type == "deep_dive" and res["mode"] == "chat" and res["reply"] and not res["deep"]:
        res["mode"], res["deep"] = "deep_dive", res["reply"]
    if res["mode"] == "reading" and len(res["cards"]) == len(drawn):
        for mc, c in zip(res["cards"], drawn):  # карты и позиции берём у приложения, смысл — у модели
            mc.update(position=c["position"], card=c["name"], orientation="reversed" if c["reversed"] else "upright")
    text = plain_text(res)
    spend_request(uid)
    with state_lock:
        if request_type == "reading" and res["mode"] == "reading":
            last_readings[uid] = {"question": question, "spread": spread, "cards": card_lines, "text": text, "topic": topic}
        h = histories.setdefault(uid, [])
        if request_type != "deep_dive":
            h.append({"role": "user", "text": question + (f" [расклад: {spread}; {'; '.join(card_lines)}]" if request_type == "reading" else "")})
            h.append({"role": "assistant", "text": text[:1200]})
            del h[:-HISTORY_LIMIT]
    return res


# ---------- Проверка пользователя Telegram Mini App ----------

def verify_init_data(init_data):
    """Возвращает dict пользователя или None. Алгоритм: core.telegram.org/bots/webapps#validating-data"""
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
        got = pairs.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs["auth_date"]) > 86400:
            return None
        return json.loads(pairs["user"])
    except (KeyError, ValueError):
        return None


# ---------- HTTP-сервер ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def cors(self):
        origin = self.headers.get("Origin", "")
        if CORS_ORIGIN and origin == CORS_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Init-Data")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def reply_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/spreads":
            return self.reply_json(200, [{"id": k, "name": v["name"], "count": len(v["positions"])}
                                         for k, v in cards.SPREADS.items()])
        if path == "/config.js":
            body = b'window.API_BASE = "";'
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", "/index.html", "/admin", "/admin.html"):
            body = (ROOT / "docs" / ("admin.html" if path in ("/admin", "/admin.html") else "index.html")).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        user = verify_init_data(self.headers.get("X-Init-Data", ""))
        if not user:
            return self.reply_json(401, {"error": "Откройте приложение из Telegram."})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self.reply_json(400, {"error": "bad json"})
        uid = user["id"]
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/admin/"):
            if uid != get_admin_id():
                return self.reply_json(403, {"error": "forbidden"})
            if path == "/api/admin/overview":
                return self.reply_json(200, admin_overview())
            if path == "/api/admin/user":
                return self.reply_json(200, admin_user(int(body.get("id", 0))))
            return self.send_error(404)
        touch_user(user, body.get("birth_date"))
        if path == "/api/quota":
            return self.reply_json(200, quota_info(uid))
        if path == "/api/invoice":
            if body.get("plan") not in PLANS:
                return self.reply_json(400, {"error": "Неизвестный тариф."})
            try:
                link = tg("createInvoiceLink", **invoice_fields(body["plan"]))["result"]
            except ValueError as e:
                return self.reply_json(400, {"error": str(e)})
            return self.reply_json(200, {"url": link})
        question = str(body.get("question", "")).strip()[:1000]
        if not question and path != "/api/deepdive":
            return self.reply_json(400, {"error": "Напишите вопрос."})
        drawn, spread_name, rtype = [], "", "followup"
        if path == "/api/deepdive":
            rtype = "deep_dive"
        elif path == "/api/reading":
            rtype = "reading"
            sid = body.get("spread")
            if sid not in cards.SPREADS:
                return self.reply_json(400, {"error": "Неизвестный расклад."})
            drawn, spread_name = cards.draw(sid), cards.SPREADS[sid]["name"]
        elif path != "/api/chat":
            return self.send_error(404)
        try:
            res = consult(uid, question, spread_name, drawn, body.get("birth_date"), rtype, str(body.get("topic", ""))[:30])
        except LookupError:
            return self.reply_json(400, {"error": "Сначала сделайте расклад."})
        except PermissionError:
            return self.reply_json(429, {"error": "Не так быстро, подождите пару секунд."})
        except QuotaExceeded:
            try:
                send_tariffs(uid, uid, "Бесплатные запросы закончились. Выберите тариф:")
            except Exception as e:  # noqa: BLE001
                print("tariffs send error:", e)
            return self.reply_json(402, {"error": "Бесплатные запросы закончились. Тарифы — в чате с ботом (/tariffs)."})
        except Exception as e:  # noqa: BLE001
            print("consult error:", e)
            return self.reply_json(502, {"error": "Не удалось получить ответ. Попробуйте позже."})
        log_reading(uid, rtype, spread_name, (f"[{body['topic']}] " if body.get("topic") and rtype == "reading" else "") + question, res.get("title", ""))
        if res["telegram_message"].strip():
            try:
                send_html(uid, res["telegram_message"])
            except Exception as e:  # noqa: BLE001
                print("telegram send error:", e)
        self.reply_json(200, {**res, "card_readings": res["cards"], "cards": drawn})


def set_menu_button():
    """Связь с Telegram бывает нестабильной, поэтому пробуем несколько раз."""
    for attempt in range(1, 11):
        try:
            tg("setChatMenuButton", menu_button={"type": "web_app", "text": "Таро", "web_app": {"url": WEBAPP_URL}})
            print("Кнопка меню обновлена")
            return
        except Exception as e:  # noqa: BLE001
            print(f"menu button error (попытка {attempt}):", e)
            time.sleep(5)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN в .env")
    if WEBAPP_URL:
        threading.Thread(target=set_menu_button, daemon=True).start()
    init_db()
    threading.Thread(target=poll_loop, daemon=True).start()
    print(f"Мини-апп на http://localhost:{PORT}  (WEBAPP_URL={WEBAPP_URL or 'не задан'})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
