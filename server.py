"""
METALLBOT Mini App — локальный HTTP-сервер.
Автономный: читает Google Sheets напрямую через .env родительской папки.
Запуск: python server.py  (из папки webapp/)
"""
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

# ── env ──────────────────────────────────────────────────────────────────────
WEBAPP_DIR = Path(__file__).parent
ROOT_DIR   = WEBAPP_DIR.parent

# Локально: грузим .env из корня проекта (на Render env vars заданы напрямую)
ENV_FILE = ROOT_DIR / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

GOOGLE_SHEET_ID   = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON", "")
PORT = int(os.getenv("PORT", os.getenv("WEBAPP_PORT", "8765")))  # Render задаёт $PORT

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("metallbot.webapp")

# ── fastapi ───────────────────────────────────────────────────────────────────
from fastapi import FastAPI, HTTPException, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from decimal import Decimal, InvalidOperation       # КП-модуль: деньги только Decimal
from datetime import datetime

app = FastAPI(title="METALLBOT Mini App",
              docs_url=None, redoc_url=None, openapi_url=None)   # скрыть карту эндпоинтов (анти-разведка)

# Наценка КП по умолчанию (env QUOTE_MARKUP, 0.30 = +30%). Перенесено из v2.1.
QUOTE_MARKUP = Decimal(os.getenv("QUOTE_MARKUP", "0.30"))
PRICE_SHEET  = "Прайс"
_MONEY_CLEAN = re.compile(r"[^\d.,\-]")

# ── CORS ──────────────────────────────────────────────────────────────────────
# Mini App открывается из того же origin (этот сервер отдаёт и HTML, и API),
# поэтому широкий "*" не нужен. Сужаем до известных доменов; список можно
# переопределить переменной окружения CORS_ORIGINS (через запятую) или "*".
_DEFAULT_ORIGINS = [
    "https://metallbot-webapp.onrender.com",
    "https://mt073.ru",
    "https://soyuzprom.tech",
    "http://localhost:8765",
    "http://127.0.0.1:8765",
]
_cors_env = os.getenv("CORS_ORIGINS", "").strip()
if _cors_env == "*":
    _cors_origins = ["*"]
elif _cors_env:
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
else:
    _cors_origins = _DEFAULT_ORIGINS
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
logger.info("CORS origins: %s", _cors_origins)

# ── Простой rate-limit (анти-скрейп/DoS), in-memory на инстанс ────────────────
# Защищает /api/* от массового скрейпа/завала. Щедрый порог, чтобы не задеть
# поллинг бота (~30-40/мин с одного IP) и обычную работу мини-аппа. /api/img
# (прокси фото, может давать бурсты при просмотре карточек) и health — исключены.
from collections import deque as _deque
import time as _time_rl
_RL_WINDOW = 60
_RL_MAX    = int(os.getenv("RATE_LIMIT_PER_MIN", "300"))
_RL_EXEMPT = ("/api/health", "/api/img", "/static")
_rl_hits: dict = {}
@app.middleware("http")
async def _rate_limit_mw(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or any(path.startswith(e) for e in _RL_EXEMPT):
        return await call_next(request)
    ip  = request.client.host if request.client else "?"
    now = _time_rl.time()
    dq  = _rl_hits.get(ip)
    if dq is None:
        if len(_rl_hits) > 20000:        # грубая защита от роста словаря по уник. IP
            _rl_hits.clear()
        dq = _deque(); _rl_hits[ip] = dq
    while dq and now - dq[0] > _RL_WINDOW:
        dq.popleft()
    if len(dq) >= _RL_MAX:
        from fastapi.responses import JSONResponse as _JR
        return _JR({"error": "Слишком много запросов, попробуйте позже"}, status_code=429)
    dq.append(now)
    return await call_next(request)

# ── Авторизация Mini App (Telegram initData, HMAC-SHA256 от токена бота) ───────
import hashlib as _hashlib
import hmac as _hmac
from urllib.parse import parse_qsl as _parse_qsl
from fastapi import Header, Depends

_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

def _load_allowed_ids() -> set:
    """ID Telegram, которым разрешены изменения (админы + менеджеры).
    Источники: env ALLOWED_IDS (через запятую/пробел) — ОСНОВНОЙ на Render, т.к.
    users.json в webapp-репо нет; плюс users.json (если примонтирован локально)."""
    ids: set = set()
    # из окружения ALLOWED_IDS (на Render users.json отсутствует → это главный источник)
    raw = os.getenv("ALLOWED_IDS", "")
    for tok in raw.replace(",", " ").replace(";", " ").split():
        if tok.strip().isdigit():
            ids.add(int(tok.strip()))
    # из users.json (локальный запуск рядом с ботом)
    try:
        uj = ROOT_DIR / "users.json"
        if uj.exists():
            data = json.loads(uj.read_text(encoding="utf-8"))
            for key in ("admins", "managers"):
                for v in (data.get(key) or []):
                    try:
                        ids.add(int(v))
                    except (ValueError, TypeError):
                        pass
    except Exception as e:
        logger.warning("[auth] users.json: %s", e)
    return ids

def _verify_init_data(init_data: str) -> Optional[dict]:
    """Проверяет подпись Telegram WebApp initData. Возвращает данные пользователя
    при валидной подписи, иначе None. Алгоритм — официальный (HMAC-SHA256,
    секрет = HMAC('WebAppData', bot_token))."""
    if not init_data or not _BOT_TOKEN:
        return None
    try:
        pairs = dict(_parse_qsl(init_data, keep_blank_values=True))
        recv_hash = pairs.pop("hash", None)
        if not recv_hash:
            return None
        check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = _hmac.new(b"WebAppData", _BOT_TOKEN.encode(), _hashlib.sha256).digest()
        calc_hash = _hmac.new(secret, check_string.encode(), _hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(calc_hash, recv_hash):
            return None
        # Анти-replay: подписанный initData не должен быть старше суток. Без этой
        # проверки однажды перехваченная валидная строка даёт доступ навсегда.
        try:
            ad = int(pairs.get("auth_date", "0"))
        except (ValueError, TypeError):
            return None
        if ad <= 0 or (time.time() - ad) > 86400:
            return None
        user = pairs.get("user")
        return json.loads(user) if user else {}
    except Exception as e:
        logger.warning("[auth] verify: %s", e)
        return None

def _user_from_init_unverified(init_data: str) -> dict:
    """Достаёт user из initData БЕЗ проверки подписи.
    На Render может не быть TELEGRAM_TOKEN → _verify_init_data вернёт None и мы
    не узнаем заказчика. Для уведомления-заявки (с кем связаться) подпись не
    критична — поэтому здесь парсим имя/username/id без HMAC. Для изменяющих
    операций по-прежнему используется строгий _verify_init_data."""
    if not init_data:
        return {}
    try:
        pairs = dict(_parse_qsl(init_data, keep_blank_values=True))
        u = pairs.get("user")
        return json.loads(u) if u else {}
    except Exception:
        return {}

def require_manager(x_telegram_init_data: str = Header(default="")) -> dict:
    """FastAPI-зависимость для защиты изменяющих эндпоинтов (PUT/DELETE).

    Поэтапное включение без риска сломать продакшен:
      • TELEGRAM_TOKEN НЕ задан в окружении  → пропускаем (предупреждение в лог),
        поведение прежнее. Чтобы включить защиту — задать TELEGRAM_TOKEN на Render.
      • TELEGRAM_TOKEN задан → требуем валидный initData; id пользователя должен
        быть в users.json (admins/managers). Иначе 401/403.
    """
    if not _BOT_TOKEN:
        logger.warning("[auth] TELEGRAM_TOKEN не задан — изменения БЕЗ авторизации "
                       "(задайте TELEGRAM_TOKEN на Render, чтобы включить защиту)")
        return {"_unverified": True}
    user = _verify_init_data(x_telegram_init_data)
    if user is None:
        raise HTTPException(401, "Требуется авторизация Telegram (initData)")
    allowed = _load_allowed_ids()
    uid = user.get("id")
    if allowed and uid not in allowed:
        raise HTTPException(403, "Нет прав на изменение базы")
    return user

# Секрет для служебных эндпоинтов (дренирующие очереди + сброс кэша), которые дёргает
# ТОЛЬКО бот. = sha256(TELEGRAM_TOKEN) — тот же токен у бота и на Render → совпадает
# без новых env-переменных. Защищает от анонимного дренажа очередей (кража действий
# бота) и DoS сбросом кэша.
_WEBAPP_SECRET = _hashlib.sha256(_BOT_TOKEN.encode()).hexdigest() if _BOT_TOKEN else ""
def require_pull_secret(x_webapp_secret: str = Header(default="")):
    """Если TELEGRAM_TOKEN не задан → пропускаем (поведение прежнее, без риска).
    Иначе требуем совпадения секрета (constant-time)."""
    if not _BOT_TOKEN:
        return
    if not _hmac.compare_digest(x_webapp_secret or "", _WEBAPP_SECRET):
        raise HTTPException(401, "Требуется секрет служебного эндпоинта")

# Раздаём скачанные фото партнёров
STATIC_DIR = WEBAPP_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
(STATIC_DIR / "images").mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── Google Sheets: прямое подключение ────────────────────────────────────────
_ss      = None   # spreadsheet object
_cache: dict[str, tuple[float, list]] = {}  # sheet_name -> (ts, rows)
CACHE_TTL = 120   # сек

def _get_spreadsheet():
    global _ss
    if _ss is not None:
        return _ss
    import gspread
    from google.oauth2.service_account import Credentials
    SCOPES = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    if GOOGLE_CREDS_JSON and GOOGLE_CREDS_JSON.strip().startswith("{"):
        creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDS_JSON), scopes=SCOPES)
    else:
        creds_file = ROOT_DIR / "google_creds.json"
        creds = Credentials.from_service_account_file(str(creds_file), scopes=SCOPES)
    gc  = gspread.authorize(creds)
    _ss = gc.open_by_key(GOOGLE_SHEET_ID)
    logger.info("Google Sheets подключён")
    return _ss

def _sheet_rows(sheet_name: str) -> list[dict]:
    """Возвращает строки листа как список dict; кэш 120 сек."""
    now = time.time()
    if sheet_name in _cache:
        ts, rows = _cache[sheet_name]
        if now - ts < CACHE_TTL:
            return rows
    try:
        ss = _get_spreadsheet()
        ws = ss.worksheet(sheet_name)
        all_vals = ws.get_all_values()
        if not all_vals or len(all_vals) < 2:
            _cache[sheet_name] = (now, [])
            return []
        headers = all_vals[0]
        rows = [dict(zip(headers, r)) for r in all_vals[1:]]
        _cache[sheet_name] = (now, rows)
        logger.info(f"[cache] {sheet_name}: {len(rows)} строк")
        return rows
    except Exception as e:
        logger.warning(f"Sheets ({sheet_name}): {e}")
        return _cache.get(sheet_name, (0, []))[1]

# ── Helpers ───────────────────────────────────────────────────────────────────
def _s(v: Any) -> str:
    s = str(v).strip() if v else ""
    # Фильтруем ошибки Google Sheets (#ERROR!, #REF!, #N/A и т.д.)
    if s and s.startswith('#') and ('!' in s or s in ('#N/A', '#REF', '#DIV/0')):
        return ""
    return s

def _clean_phone(p: str) -> str:
    """
    Нормализует телефон до формата +7 (XXX) XXX-XX-XX.
    Принимает любой мусор: кавычки, тире, пробелы, скобки.
    Извлекает только цифры и собирает стандартный российский формат.
    """
    import unicodedata, re as _re
    if not p:
        return p
    p = unicodedata.normalize("NFKC", p)
    digits = _re.sub(r"\D", "", p)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return _re.sub(r"\s+", " ", p).strip()
    return f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
def _split(val: str) -> list[str]:
    """Разбивает строку вида 'a | b | c' в список."""
    if not val:
        return []
    return [x.strip() for x in val.split("|") if x.strip()]

def _row_to_contact(row: dict, kind: str) -> dict:
    name = _s(row.get("Название") or row.get("Компания") or row.get("Поставщик"))
    # Объединяем данные из таблицы + данные от парсера
    phone  = _s(row.get("Телефон_парс") or row.get("Телефон"))
    email  = _s(row.get("Email_парс")   or row.get("Email"))

    # Услуги и оборудование — парсер дополняет ручные данные
    service   = _s(row.get("Услуги_парс") or row.get("Виды_работ") or
                   row.get("Вид услуги")  or row.get("Вид покрытия"))
    equipment = _s(row.get("Оборудование_парс") or row.get("Оборудование"))
    materials = _s(row.get("Материалы_парс")    or row.get("Материалы"))
    spec      = _s(row.get("Специализация") or row.get("Вид металла/услуги"))
    # Парк оборудования клиента — file_id фото станков (Фаза 3 ч.2b, пишет бот)
    park_photos = [x.strip() for x in _s(row.get("Парк_Фото")).split(",") if x.strip()][:8]

    return {
        "id":             name,
        "kind":           kind,
        "name":           name,
        "city":           _s(row.get("Город") or row.get("Адрес_парс")),
        "phone":          phone,
        "email":          email,
        "contact":        _s(row.get("Контакт") or row.get("Менеджер")),
        "specialization": spec,
        "service":        service,
        "equipment":      equipment,
        "materials":      materials,
        "park_photos":    park_photos,
        "status":         _s(row.get("Статус")),
        "rating":         _s(row.get("Рейтинг")),
        "price_level":    _s(row.get("Цена_уровень")),
        "notes":          _s(row.get("Заметки") or row.get("Примечание")),
        "requisites":     _s(row.get("Реквизиты")),
        "added":          _s(row.get("Добавлено") or row.get("Дата")),
        "added_by":       _s(row.get("Кто_добавил")),
        # Парсинговые данные — читаем из листа "Парсинг" по имени компании
        "_parsed_name":   name,   # для поиска в _parsed_data()
        "website":        _s(row.get("Сайт", "")),
        "raw":            {k: _s(v) for k, v in row.items()},
    }

def _norm(s: str) -> str:
    """Убирает всё кроме букв и цифр — для сравнения названий."""
    import re
    return re.sub(r'[^\w]', '', s.lower(), flags=re.UNICODE)

def _dom(u: str) -> str:
    """Домен из URL/строки: 'https://www.X.ru/price?y' → 'x.ru'. Для связи карточки с парсингом."""
    import re
    u = (u or "").strip().lower()
    if not u:
        return ""
    u = re.sub(r'^https?://', '', u)
    u = re.sub(r'^www\.', '', u)
    return u.split('/')[0].split('?')[0].split('#')[0].strip()

# бесплатные почтовые домены — по ним связывать НЕЛЬЗЯ (это не сайт компании)
_FREE_EMAIL = {"mail.ru", "gmail.com", "yandex.ru", "ya.ru", "bk.ru", "inbox.ru",
               "list.ru", "rambler.ru", "outlook.com", "icloud.com", "internet.ru",
               "mail.com", "hotmail.com", "yahoo.com"}

def _email_dom(e: str) -> str:
    """Домен из e-mail, кроме бесплатных провайдеров: 'info@x.ru' → 'x.ru', 'a@mail.ru' → ''."""
    e = (e or "").strip().lower()
    if "@" not in e:
        return ""
    d = e.split("@")[-1].split()[0].strip()
    return "" if d in _FREE_EMAIL else d

def _parsed_data() -> tuple[dict, dict]:
    """Лист 'Парсинг' → (by_name {norm(Компания): row}, by_domain {домен(Сайт): row}).
    Два индекса, т.к. имена карточек переименовывались (чистка/восстановление), а домен
    сайта стабилен — связь по домену возвращает парсинг сотням карточек со сменённым именем."""
    by_name: dict = {}
    by_dom: dict = {}
    try:
        rows = _sheet_rows("Парсинг")
    except Exception:
        return by_name, by_dom
    for row in rows:
        name = _s(row.get("Компания", ""))
        if name:
            by_name.setdefault(_norm(name), row)
        d = _dom(_s(row.get("Сайт", "")))
        if d:
            by_dom.setdefault(d, row)
    return by_name, by_dom


def _load_contacts() -> list[dict]:
    coop_list = [c for c in (_row_to_contact(r, "coop") for r in _sheet_rows("Кооперация")) if c["name"]]
    supp_list = [c for c in (_row_to_contact(r, "supplier") for r in _sheet_rows("Поставщики")) if c["name"]]

    coop_map = {_norm(c["name"]): c for c in coop_list}
    supp_map = {_norm(c["name"]): c for c in supp_list}
    both_keys = set(coop_map) & set(supp_map)

    contacts = []
    seen = set()

    for c in coop_list:
        key = _norm(c["name"])
        if key in both_keys:
            # Объединяем: берём coop-данные, дополняем supplier-данными
            merged = {**supp_map[key], **{k: v for k, v in c.items() if v}}
            merged["kind"] = "both"
            contacts.append(merged)
        else:
            contacts.append(c)
        seen.add(key)

    for c in supp_list:
        key = _norm(c["name"])
        if key not in seen:
            contacts.append(c)

    return _attach_smart_stars(contacts)

def _purchase_history(supplier: str) -> list[dict]:
    if not supplier:
        return []
    needle = supplier.lower().strip()
    out = []
    for r in _sheet_rows("Закупки"):
        if needle and needle in _s(r.get("Поставщик")).lower():
            out.append({
                "date":    _s(r.get("Дата счёта")),
                "invoice": _s(r.get("Номер счёта")),
                "name":    _s(r.get("Наименование")),
                "mark":    _s(r.get("Марка")),
                "size":    _s(r.get("Размер")),
                "qty":     _s(r.get("Кол-во")),
                "unit":    _s(r.get("Единица")),
                "price":   _s(r.get("Цена факт")),
                "sum":     _s(r.get("Сумма")),
            })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:30]

def _money(v) -> float:
    """Парсит сумму ('540 000', '540000,50', '12 300 ₽') в float."""
    s = _s(v).replace("\xa0", "").replace(" ", "").replace("₽", "").replace(",", ".")
    try:
        return float(s) if s and s not in ("-",) else 0.0
    except ValueError:
        return 0.0

# ── Звёзды по ВНУТРЕННЕЙ АКТИВНОСТИ в экосистеме ─────────────────────────────────
# Источник — лист «Активность» (заполняет бот + кнопка «отметить отклик» в CRM):
#   Карточка | Лист | Заявок | Просчётов | Откликов | Последняя_активность
# Вес сигналов: отклик на рассылку/КП = самый ценный, заявка, затем калькулятор.
ACT_SHEET = "Активность"
ACT_HEADERS = ["Карточка", "Лист", "Заявок", "Просчётов", "Откликов", "Последняя_активность"]
ACT_W = {"resp": 5, "order": 2, "calc": 1}
ACT_KIND_COL = {"resp": "Откликов", "order": "Заявок", "calc": "Просчётов"}

def _int(v) -> int:
    try:
        return int(float(_s(v).replace(" ", "").replace(",", ".") or 0))
    except ValueError:
        return 0

def _activity_map() -> dict:
    """{norm(card): {order,calc,resp,score,last}} из листа «Активность»."""
    out: dict = {}
    for r in _sheet_rows(ACT_SHEET):
        name = _s(r.get("Карточка") or r.get("Контрагент") or r.get("Карточка_контрагента"))
        if not name:
            continue
        o, cc, rp = _int(r.get("Заявок")), _int(r.get("Просчётов")), _int(r.get("Откликов"))
        score = rp * ACT_W["resp"] + o * ACT_W["order"] + cc * ACT_W["calc"]
        out[_norm(name)] = {"order": o, "calc": cc, "resp": rp, "score": score,
                            "last": _s(r.get("Последняя_активность"))}
    return out

def _attach_smart_stars(contacts: list[dict]) -> list[dict]:
    """Звёзды контрагентов = ВНУТРЕННЯЯ АКТИВНОСТЬ в экосистеме (заявки/заказы,
    отклики на рассылки ТЗ/КП, использование калькулятора), а НЕ загруженные счета.
    Перцентильная шкала среди контрагентов с активностью (0★ если активности нет)."""
    import bisect
    act = _activity_map()
    for c in contacts:
        a = act.get(_norm(c.get("name") or ""))
        c["activity"] = a or {"order": 0, "calc": 0, "resp": 0, "score": 0, "last": ""}
        c["stars"] = 0
    rated = [c for c in contacts if c["activity"]["score"] > 0]
    m = len(rated)
    if m:
        keys = sorted(c["activity"]["score"] for c in rated)
        for c in rated:
            le = bisect.bisect_right(keys, c["activity"]["score"])
            q = le / m
            c["stars"] = 5 if q >= 0.85 else 4 if q >= 0.65 else 3 if q >= 0.35 else 2 if q >= 0.15 else 1
    return contacts

def _activity_bump(card_name: str, kind: str, sheet: str = "", delta: int = 1) -> dict:
    """Увеличивает счётчик активности kind ('resp'|'order'|'calc') для карточки в
    листе «Активность» (upsert). Создаёт лист при отсутствии. Возвращает новые счётчики."""
    col = ACT_KIND_COL.get(kind)
    if not col:
        raise HTTPException(400, f"Неизвестный тип активности: {kind}")
    ss = _get_spreadsheet()
    try:
        ws = ss.worksheet(ACT_SHEET)
    except Exception:
        ws = ss.add_worksheet(title=ACT_SHEET, rows=200, cols=len(ACT_HEADERS))
        ws.update(values=[ACT_HEADERS], range_name="A1")
    vals = ws.get_all_values()
    headers = vals[0] if vals else ACT_HEADERS
    cidx = {h: i for i, h in enumerate(headers)}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    row_idx = None
    for i, row in enumerate(vals[1:], start=2):
        nm = row[cidx["Карточка"]].strip() if "Карточка" in cidx and len(row) > cidx["Карточка"] else ""
        if nm.lower() == card_name.lower():
            row_idx = i
            break
    if row_idx is None:
        new = {"Карточка": card_name, "Лист": sheet, "Заявок": 0, "Просчётов": 0,
               "Откликов": 0, "Последняя_активность": now}
        new[col] = max(0, delta)
        ws.append_row([new.get(h, "") for h in ACT_HEADERS], value_input_option="RAW")
        counts = {"order": new["Заявок"], "calc": new["Просчётов"], "resp": new["Откликов"]}
    else:
        cur = vals[row_idx - 1]
        def g(h):
            return cur[cidx[h]] if h in cidx and len(cur) > cidx[h] else ""
        counts = {"order": _int(g("Заявок")), "calc": _int(g("Просчётов")), "resp": _int(g("Откликов"))}
        kmap = {"Заявок": "order", "Просчётов": "calc", "Откликов": "resp"}[col]
        counts[kmap] = max(0, counts[kmap] + delta)
        ws.update_cell(row_idx, cidx[col] + 1, counts[kmap])
        if "Последняя_активность" in cidx:
            ws.update_cell(row_idx, cidx["Последняя_активность"] + 1, now)
        if "Лист" in cidx and sheet and not g("Лист"):
            ws.update_cell(row_idx, cidx["Лист"] + 1, sheet)
    _cache.pop(ACT_SHEET, None)   # сбросить кэш, чтобы звёзды пересчитались
    _dirty_sheets.add(ACT_SHEET)
    return counts

# ── API routes ────────────────────────────────────────────────────────────────
def _sort_key(c: dict) -> tuple:
    """vip_own=0, vip=1, остальные=2; внутри группы — по рейтингу убыв."""
    st = (c.get("status") or "").lower().strip()
    if st == "vip_own":
        tier = 0
    elif st == "vip":
        tier = 1
    else:
        tier = 2
    try:
        rat = -float(c.get("stars") or 0)   # умные звёзды (реальные закупки)
    except Exception:
        rat = 0
    return (tier, rat)

@app.get("/api/contacts")
def api_contacts(q: str = "", kind: str = "", _auth: dict = Depends(require_manager)):
    items = _load_contacts()
    if kind:
        items = [x for x in items if x["kind"] == kind]
    if q:
        n = q.lower().strip()
        items = [x for x in items
                 if n in x["name"].lower()
                 or n in x["city"].lower()
                 or n in x["service"].lower()
                 or n in x["materials"].lower()
                 or n in x["equipment"].lower()
                 or n in x["specialization"].lower()
                 or n in x["notes"].lower()
                 or n in (x.get("email") or "").lower()
                 or n in (x.get("phone") or "").lower()
                 or n in (x.get("contact") or "").lower()]
    items.sort(key=_sort_key)
    return JSONResponse(
        {"items": items, "total": len(items)},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )

@app.get("/api/contact/{contact_id:path}")
def api_contact(contact_id: str, _auth: dict = Depends(require_manager)):
    items = _load_contacts()
    target = next(
        (x for x in items
         if x["id"].lower() == contact_id.lower()
         or x["name"].lower() == contact_id.lower()),
        None,
    )
    if not target:
        raise HTTPException(404, f"Не найден: {contact_id}")

    # Добавляем парсинговые данные из листа "Парсинг".
    # Связь: по имени → по домену сайта → по домену e-mail (имена карточек
    # переименовывались при чистке, а домен стабилен — иначе карточка теряет
    # описание/фото/услуги и выглядит «минимальной»).
    by_name, by_dom = _parsed_data()
    p = by_name.get(_norm(target["name"]), {})
    if not p:
        d = _dom(target.get("website", ""))
        if d:
            p = by_dom.get(d, {})
    if not p:
        ed = _email_dom(target.get("email", ""))
        if ed:
            p = by_dom.get(ed, {})
    target["description"]    = _s(p.get("Описание_парс", ""))
    target["services_list"]  = _split(p.get("Услуги_парс", ""))
    target["equipment_list"] = _split(p.get("Оборудование_парс", ""))
    target["materials_list"] = _split(p.get("Материалы_парс", ""))
    target["certificates"]   = _split(p.get("Сертификаты_парс", ""))
    target["address"]        = _s(p.get("Адрес_парс", ""))
    target["work_hours"]     = _s(p.get("Режим_работы", ""))
    target["founded_year"]   = _s(p.get("Год_основания", ""))
    target["employees"]      = _s(p.get("Сотрудников", ""))
    target["area_sqm"]       = _s(p.get("Площадь", ""))
    target["extra_facts"]    = _split(p.get("Факты_парс", ""))
    target["photo_urls"]     = _split(p.get("Фото_URLs", ""))
    target["parsed_at"]      = _s(p.get("Парсинг_дата", ""))
    if not target.get("website"):
        target["website"]    = _s(p.get("Сайт", ""))
    # Подтягиваем телефон/email/город из Парсинга если в основном листе пусто
    if not target.get("phone"):
        target["phone"]      = _s(p.get("Телефон_парс", ""))
    if not target.get("email"):
        target["email"]      = _s(p.get("Email_парс", ""))
    if not target.get("city"):
        target["city"]       = _s(p.get("Адрес_парс", ""))

    history   = _purchase_history(target["name"])
    total_sum = sum(
        float(h["sum"].replace(",", ".").replace(" ", "") or 0)
        for h in history
        if h["sum"] not in ("", "-")
    )
    target["stats"]   = {
        "purchases_count": len(history),
        "total_sum":       round(total_sum, 2),
        "last_date":       history[0]["date"] if history else "",
    }
    target["history"] = history
    return target

class ContactUpdate(BaseModel):
    name:           Optional[str] = None   # переименование карточки (ключ /api/contact/<name>)
    phone:          Optional[str] = None
    email:          Optional[str] = None
    city:           Optional[str] = None
    website:        Optional[str] = None
    specialization: Optional[str] = None
    services:       Optional[str] = None   # Виды_работ + Услуги_парс
    equipment:      Optional[str] = None   # Оборудование + Оборудование_парс
    materials:      Optional[str] = None   # Материалы + Материалы_парс
    status:         Optional[str] = None
    rating:         Optional[str] = None
    notes:          Optional[str] = None
    requisites:     Optional[str] = None   # реквизиты (ИНН/КПП/ОГРН/р.с./банк/адрес/директор)

# Маппинг поля → возможные названия колонок в листах
_FIELD_COLS: dict[str, list[str]] = {
    "name":           ["Название", "Поставщик", "Компания"],   # переименование — в существующую колонку имени
    "phone":          ["Телефон"],
    "email":          ["Email"],
    "city":           ["Город"],
    "website":        ["Сайт"],
    "specialization": ["Специализация", "Вид металла/услуги"],
    "services":       ["Виды_работ", "Услуги_парс"],
    "equipment":      ["Оборудование", "Оборудование_парс"],
    "materials":      ["Материалы", "Материалы_парс"],
    "status":         ["Статус"],
    "rating":         ["Рейтинг"],
    "notes":          ["Заметки", "Примечание"],
    "requisites":     ["Реквизиты"],   # колонка создаётся автоматически при первом сохранении
}

# ── Сигнал боту о грязном кэше ───────────────────────────────────────────────
_dirty_sheets: set[str] = set()

@app.get("/api/cache-status")
def api_cache_status():
    """Бот спрашивает: были ли изменения? Возвращает список грязных листов и сбрасывает флаг."""
    global _dirty_sheets
    dirty = list(_dirty_sheets)
    _dirty_sheets = set()
    return {"dirty": dirty}

@app.post("/api/cache-bust")
async def api_cache_bust(request: Request, _sec=Depends(require_pull_secret)):
    """Явный сброс кэша листов. Принимает {"sheets": [...]} (формат бота) или голый список."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    sheets = body.get("sheets") if isinstance(body, dict) else body
    if not isinstance(sheets, list) or not sheets:
        sheets = ["Кооперация"]
    for s in sheets:
        _cache.pop(s, None)
        _dirty_sheets.add(s)
    return {"ok": True}

@app.put("/api/contact/{contact_id:path}")
def api_contact_update(contact_id: str, body: ContactUpdate, _auth: dict = Depends(require_manager)):
    """Обновляет поля компании в Google Sheets."""
    import gspread as _gs

    update_data = {k: v for k, v in body.dict().items() if v is not None}
    if not update_data:
        raise HTTPException(400, "Нет данных для обновления")

    # Санитизация телефона
    if "phone" in update_data:
        update_data["phone"] = _clean_phone(update_data["phone"])

    updated_sheets: list[str] = []
    ss = _get_spreadsheet()

    for sheet_name in ("Кооперация", "Поставщики"):
        try:
            ws = ss.worksheet(sheet_name)
            all_vals = ws.get_all_values()
            if not all_vals:
                continue
            headers = all_vals[0]
            col_map = {h: i + 1 for i, h in enumerate(headers)}

            # Ищем строку по имени компании
            name_cols = [i for i, h in enumerate(headers)
                         if h in ("Название", "Компания", "Поставщик")]
            row_idx = None
            for r_i, row in enumerate(all_vals[1:], start=2):
                for nc in name_cols:
                    cell = row[nc].strip() if len(row) > nc else ""
                    if cell.lower() == contact_id.lower():
                        row_idx = r_i
                        break
                if row_idx:
                    break

            if row_idx is None:
                continue

            # Если нужной колонки нет — создаём
            cells: list[_gs.Cell] = []
            for field, value in update_data.items():
                col_names = _FIELD_COLS.get(field, [field])
                target_col = None
                for cn in col_names:
                    if cn in col_map:
                        target_col = col_map[cn]
                        break
                if target_col is None:
                    # Создаём колонку
                    new_idx = len(headers) + 1
                    ws.update_cell(1, new_idx, col_names[0])
                    col_map[col_names[0]] = new_idx
                    headers.append(col_names[0])
                    target_col = new_idx
                cells.append(_gs.Cell(row_idx, target_col, value))

            if cells:
                ws.update_cells(cells, value_input_option="RAW")
                updated_sheets.append(sheet_name)
                _cache.pop(sheet_name, None)   # сброс кэша этого листа
                _dirty_sheets.add(sheet_name)  # сигнал боту: кэш устарел

        except Exception as e:
            logger.warning("[update] %s: %s", sheet_name, e)

    if not updated_sheets:
        raise HTTPException(404, f"Компания не найдена: {contact_id}")

    return {"ok": True, "updated": updated_sheets}


class ActivityBump(BaseModel):
    kind: str = "resp"   # 'resp' | 'order' | 'calc'
    delta: int = 1

@app.post("/api/contact/{contact_id:path}/activity")
def api_contact_activity(contact_id: str, body: ActivityBump, _auth: dict = Depends(require_manager)):
    """Отметить активность контрагента в экосистеме (ручная отметка отклика менеджером
    + точка для авто-сигналов бота). Пишет в лист «Активность», звёзды пересчитаются."""
    items = _load_contacts()
    target = next((x for x in items
                   if x["id"].lower() == contact_id.lower() or x["name"].lower() == contact_id.lower()),
                  None)
    if not target:
        raise HTTPException(404, f"Не найден: {contact_id}")
    sheet = {"coop": "Кооперация", "supplier": "Поставщики", "both": "Кооперация"}.get(target.get("kind"), "")
    counts = _activity_bump(target["name"], body.kind, sheet=sheet, delta=body.delta)
    score = counts["resp"] * ACT_W["resp"] + counts["order"] * ACT_W["order"] + counts["calc"] * ACT_W["calc"]
    return {"ok": True, "name": target["name"], "counts": counts, "score": score}


@app.delete("/api/contact/{contact_id:path}")
def api_contact_delete(contact_id: str, _auth: dict = Depends(require_manager)):
    """Удаляет компанию из всех листов Google Sheets (Кооперация, Поставщики, Парсинг)."""
    ss = _get_spreadsheet()
    deleted_from: list[str] = []

    for sheet_name in ("Кооперация", "Поставщики", "Парсинг"):
        try:
            ws = ss.worksheet(sheet_name)
            all_vals = ws.get_all_values()
            if not all_vals:
                continue
            headers = all_vals[0]

            # Определяем колонку с именем компании
            name_col_idx = None
            for i, h in enumerate(headers):
                if h in ("Название", "Компания", "Поставщик"):
                    name_col_idx = i
                    break
            if name_col_idx is None:
                continue

            # Ищем все строки с этим именем
            rows_found = []
            for r_i, row in enumerate(all_vals[1:], start=2):
                cell = row[name_col_idx].strip() if len(row) > name_col_idx else ""
                if cell.lower() == contact_id.lower():
                    rows_found.append(r_i)

            # Удаляем только ОДНУ строку (последнюю — дубль).
            # Если запись единственная — удаляем её.
            # Никогда не удаляем больше одной за один запрос.
            rows_to_delete = [rows_found[-1]] if rows_found else []
            for r_i in rows_to_delete:
                ws.delete_rows(r_i)
                logger.info("[delete] %s: удалена строка %d ('%s') из %d найденных",
                            sheet_name, r_i, contact_id, len(rows_found))

            if rows_to_delete:
                deleted_from.append(sheet_name)
                _cache.pop(sheet_name, None)

        except Exception as e:
            logger.warning("[delete] %s: %s", sheet_name, e)

    if not deleted_from:
        raise HTTPException(404, f"Компания не найдена: {contact_id}")

    return {"ok": True, "deleted_from": deleted_from, "name": contact_id}


# Операции и ключи (единый источник истины для счётчика чипов и фильтра).
# Лейблы содержат подстроки-ключи, чтобы фронт-фильтр в contact.html (OP_KW) совпадал с сервером.
OP_KW_LIST = [
    ("Токарная",             ["токарн", "токарк", "lathe"]),
    ("Фрезерная",            ["фрезер", "milling"]),
    ("Шлифовальная",         ["шлифов", "шлифовк", "grinding"]),
    ("Сверлильно-расточная", ["сверл", "расточ"]),
    ("Термообработка",       ["термо", "закалк", "отжиг", "улучшение", "heat"]),
    ("Покрытие",             ["покрыт", "гальван", "цинков", "хромиров", "никелир", "анодир"]),
    ("Порошковая покраска",  ["порошк", "покрас"]),
    ("Сварка",               ["сварк", "сварочн", "welding"]),
    ("Лазерная резка",       ["лазерн", "laser"]),
    ("Плазменная резка",     ["плазм"]),
    ("Гибка",                ["гибк", "гнут"]),
    ("Штамповка",            ["штамп", "прессов", "stamp"]),
    ("Литьё",                ["литьё", "литья", "литейн", "casting"]),
    ("Слесарная",            ["слесарн", "слесарк"]),
    ("Электроэрозионная",    ["эрозионн", "электроэроз", "wire", "edm"]),
]


@app.get("/api/operations")
def api_operations():
    """
    Возвращает топ операций по базе кооператоров для фильтр-чипов в Mini App.
    Ищет ключи в name + Виды_работ + Оборудование + Специализация + Материалы + Заметки.
    """
    import re as _re
    OP_KEYS = OP_KW_LIST
    contacts = _load_contacts()
    coops = [c for c in contacts if c["kind"] in ("coop", "both")]
    counts: dict[str, int] = {}
    for op_label, kws in OP_KEYS:
        cnt = 0
        for c in coops:
            # Операции — по полям ВОЗМОЖНОСТЕЙ: name + Виды_работ(service) + Оборудование + Специализация.
            # НЕ по «Материалы»/«Заметки»: иначе металлоторговец с «оцинкованным прокатом» в материалах
            # ложно попадёт в «Покрытие». name учитываем (карточки названы по услуге: «ФРЕЗЕРИСТ»).
            text = " ".join([
                c.get("name", ""), c.get("service", ""), c.get("equipment", ""),
                c.get("specialization", ""),
            ]).lower()
            if any(kw in text for kw in kws):
                cnt += 1
        if cnt > 0:
            counts[op_label] = cnt
    # Сортируем по убыванию
    result = [{"op": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]
    return {"operations": result}


# Материалы — ключи синхронны с contact.html MAT_KW и _reclassify_mat.py / _fill_mat.py
MAT_KW_LIST = [
    ("Сталь",      ["сталь", "стальн", "ст3", "ст20", "ст45", "40х", "09г2с", "30хгса", "конструкцион", "углеродист"]),
    ("Нержавейка", ["нержав", "12х18", "08х18", "инокс", "коррозионностойк"]),
    ("Алюминий",   ["алюмин", "д16", "амг", "ад31", "дюрал", "силумин"]),
    ("Латунь",     ["латун", "л63", "лс59"]),
    ("Бронза",     ["бронз", "браж", "броф"]),
    ("Медь",       ["медь", "медн"]),
    ("Титан",      ["титан", "вт1", "вт6", "от4"]),
    ("Чугун",      ["чугун", "сч10", "сч20", "вч50"]),
    ("Пластик",    ["пластик", "капролон", "фторопласт", "полиамид", "текстолит", "оргстекл", "паронит"]),
]


@app.get("/api/materials")
def api_materials():
    """Топ материалов по кооператорам для фильтр-чипов «кто работает с …». Источник — поле «Материалы»."""
    coops = [c for c in _load_contacts() if c["kind"] in ("coop", "both")]
    counts: dict[str, int] = {}
    for mat_label, kws in MAT_KW_LIST:
        cnt = sum(1 for c in coops if any(kw in (c.get("materials", "") or "").lower() for kw in kws))
        if cnt > 0:
            counts[mat_label] = cnt
    result = [{"mat": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]
    return {"materials": result}


@app.get("/api/stats")
def api_stats():
    coops     = [c for c in _load_contacts() if c["kind"] == "coop"]
    suppliers = [c for c in _load_contacts() if c["kind"] == "supplier"]
    return {
        "coops":     len(coops),
        "suppliers": len(suppliers),
        "total":     len(coops) + len(suppliers),
    }

@app.get("/api/health")
def api_health():
    return {"ok": True, "port": PORT}

@app.post("/api/cache/clear")
def api_cache_clear(_sec=Depends(require_pull_secret)):
    """Сбрасывает кэш Google Sheets — вызывается после парсинга."""
    global _ss
    _cache.clear()
    _ss = None
    logger.info("Кэш сброшен")
    return {"ok": True, "cleared": True}

_IMG_MAX_BYTES = 10 * 1024 * 1024  # 10 МБ — потолок размера проксируемой картинки

def _is_public_host(host: str) -> bool:
    """SSRF-защита: хост не должен резолвиться в приватный/локальный/служебный IP
    (защищает от запросов прокси к внутренним сервисам)."""
    import socket, ipaddress
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True

def _safe_resolve(host: str):
    """Резолвит хост ОДИН раз и проверяет, что ВСЕ адреса публичные.
    Возвращает первый публичный IP (для pin-соединения против DNS-rebinding) или None."""
    import socket, ipaddress
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return None
    first = None
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return None
        if first is None:
            first = ip
    return first

@app.get("/api/img")
async def api_img_proxy(url: str):
    """Проксирует изображение с сайта партнёра — обходит CORS и hotlink-защиту."""
    import httpx
    from fastapi.responses import StreamingResponse
    from urllib.parse import urlparse

    parsed = urlparse(url)
    # Только http/https и только картинки
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "Недопустимый URL")
    allowed_ext = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    path = parsed.path.lower()
    if not any(path.endswith(e) for e in allowed_ext):
        raise HTTPException(400, "Только изображения")
    # SSRF-защита: резолвим хост ОДИН раз, проверяем что IP публичный, и
    # соединяемся К ЭТОМУ IP (pin против DNS-rebinding). Редиректы запрещены —
    # они могли бы увести на внутренний хост уже после проверки.
    from urllib.parse import urlunparse
    ip = _safe_resolve(parsed.hostname or "")
    if not ip:
        raise HTTPException(400, "Недопустимый адрес источника")
    _host_in_url = f"[{ip}]" if ":" in ip else ip
    _netloc = _host_in_url + (f":{parsed.port}" if parsed.port else "")
    target_url = urlunparse((parsed.scheme, _netloc, parsed.path,
                             parsed.params, parsed.query, ""))

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "image/*,*/*;q=0.8",
        "Referer": f"{parsed.scheme}://{parsed.netloc}/",
        "Host": parsed.netloc,   # сохраняем vhost при соединении по IP
    }
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=15,
                                     verify=False) as client:
            async with client.stream("GET", target_url, headers=headers) as r:
                if r.status_code != 200:
                    raise HTTPException(502, f"Источник вернул {r.status_code}")
                ct = r.headers.get("content-type", "image/jpeg")
                if not ct.startswith("image/"):
                    raise HTTPException(415, "Источник вернул не изображение")
                clen = r.headers.get("content-length")
                if clen and clen.isdigit() and int(clen) > _IMG_MAX_BYTES:
                    raise HTTPException(413, "Изображение слишком большое")
                # Читаем с ограничением размера
                chunks = []
                total = 0
                async for chunk in r.aiter_bytes():
                    total += len(chunk)
                    if total > _IMG_MAX_BYTES:
                        raise HTTPException(413, "Изображение слишком большое")
                    chunks.append(chunk)
            return StreamingResponse(
                iter([b"".join(chunks)]),
                media_type=ct,
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except httpx.RequestError as e:
        raise HTTPException(502, f"Ошибка загрузки: {e}")

@app.get("/api/images/{company_slug}")
def api_images(company_slug: str):
    """Список сохранённых фото для компании."""
    folder = STATIC_DIR / "images" / company_slug
    if not folder.exists():
        return {"images": []}
    images = []
    for f in sorted(folder.iterdir()):
        if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            images.append(f"/static/images/{company_slug}/{f.name}")
    return {"images": images, "company": company_slug}

# ════════════════════════════════════════════════════════════════════════════════
#  Block 4 — История документов + Калькулятор цены
# ════════════════════════════════════════════════════════════════════════════════

DOCS_DIR = ROOT_DIR / "excel_docs"

@app.get("/api/docs")
def api_docs_list():
    """Возвращает список папок деталей с перечнем файлов внутри."""
    if not DOCS_DIR.exists():
        return []
    result = []
    for folder in sorted(DOCS_DIR.iterdir(), key=lambda p: -p.stat().st_mtime):
        if not folder.is_dir():
            continue
        files = {}
        latest_mtime = 0
        for f in folder.iterdir():
            if f.suffix.lower() in (".xlsx", ".html", ".png"):
                tag = _doc_tag(f.stem)
                files[tag] = f.name
                latest_mtime = max(latest_mtime, f.stat().st_mtime)
        if files:
            result.append({
                "folder":  folder.name,
                "files":   files,
                "mtime":   int(latest_mtime),
                "has_3d":  "3d" in files,
                "has_kp":  "kp" in files,
                "has_mk":  "mk" in files,
            })
    return result


def _doc_tag(stem: str) -> str:
    """КП_1_... → 'kp', МК_001_... → 'mk', 3D_preview → '3d', Расчёт... → 'calc'"""
    s = stem.lower()
    if s.startswith("кп") or s.startswith("kp"):       return "kp"
    if s.startswith("мк") or s.startswith("mk"):       return "mk"
    if "3d" in s or "preview" in s:                    return "3d"
    if "расч" in s or "calc" in s or "price" in s:     return "calc"
    return "other"


@app.get("/api/docs/{folder}/{filename}")
def api_doc_file(folder: str, filename: str):
    """Отдаёт файл документа для скачивания / просмотра."""
    # Защита от path traversal
    safe_folder = Path(folder).name
    safe_file   = Path(filename).name
    path = DOCS_DIR / safe_folder / safe_file
    if not path.exists():
        raise HTTPException(404, "Файл не найден")
    suffix = path.suffix.lower()
    media = {
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".html": "text/html; charset=utf-8",
        ".png":  "image/png",
    }.get(suffix, "application/octet-stream")
    # Для HTML — inline, для xlsx — attachment
    disp = "inline" if suffix == ".html" else f'attachment; filename="{safe_file}"'
    return FileResponse(
        str(path),
        media_type=media,
        headers={"Content-Disposition": disp, "Cache-Control": "no-cache"},
    )


# ── Калькулятор цены ─────────────────────────────────────────────────────────

_MATS = {
    "aluminium": {"density": 2.78, "price_kg": 2000, "name": "Алюминий Д16Т"},
    "brass":     {"density": 8.45, "price_kg": 2000, "name": "Латунь ЛС59-1"},
    "bronze":    {"density": 7.65, "price_kg": 2000, "name": "Бронза БрАЖ9-4"},
    "steel":     {"density": 7.85, "price_kg": 100,  "name": "Сталь 45"},
}
_STD_D = [6,8,10,12,14,16,18,20,22,25,28,30,32,35,40,45,50,55,60,65,70,80,90,100]
_MACHINE_RATES = {"lathe": 3500, "bench": 1800}
_VAT = 1.22
_MARGIN = 0.28   # 28% от цены продажи (формула: цена = с/с ÷ (1−маржа))


@app.get("/api/calc")
def api_calc(
    material: str  = "aluminium",
    d_out:    float = 20.0,
    length:   float = 30.0,
    d_hole:   float = 0.0,
    complex_:  int  = 1,       # 1=simple, 2=stepped, 3=with_threads
):
    """
    Быстрый расчёт ориентировочной цены.
    complex_: 1 — простая, 2 — ступенчатая, 3 — с резьбами/рад.отв.
    """
    mat = _MATS.get(material, _MATS["aluminium"])

    # ── Заготовка ────────────────────────────────────────────────────────────
    needed_d = d_out + 4.0
    blank_d  = next((x for x in _STD_D if x >= needed_d), _STD_D[-1])
    blank_l  = length + 10.0

    import math
    V_blank = math.pi / 4 * (blank_d / 10) ** 2 * (blank_l / 10)   # cm³
    m_blank  = V_blank * mat["density"]                              # g
    mat_cost = m_blank / 1000 * mat["price_kg"]                      # ₽

    # КИМ — масса детали / масса заготовки
    if d_hole > 0:
        V_hole = math.pi / 4 * (d_hole / 10) ** 2 * (length / 10)
        V_part = math.pi / 4 * (d_out / 10) ** 2 * (length / 10) - V_hole
    else:
        V_part = math.pi / 4 * (d_out / 10) ** 2 * (length / 10)
    m_part = V_part * mat["density"]
    kim     = round(m_part / m_blank, 2) if m_blank > 0 else 0

    # ── Нормы времени ────────────────────────────────────────────────────────
    t_lathe = {1: 3.0, 2: 5.0, 3: 8.0}.get(complex_, 3.0)   # мин
    t_bench  = {1: 0.5, 2: 0.5, 3: 5.0}.get(complex_, 0.5)  # мин
    t_ctrl   = 5.0                                             # мин

    op_cost_lathe = t_lathe / 60 * _MACHINE_RATES["lathe"]
    op_cost_bench  = t_bench / 60 * _MACHINE_RATES["bench"]
    op_cost_ctrl   = t_ctrl  / 60 * _MACHINE_RATES["bench"]

    # ── Тиражи ───────────────────────────────────────────────────────────────
    qtys    = [1, 10, 50, 100, 500, 1000]
    setup_h  = {1: 0.5, 10: 0.5, 50: 0.5, 100: 1.0, 500: 1.0, 1000: 1.0}
    tiers = []
    for q in qtys:
        setup_cost = setup_h.get(q, 1.0) * _MACHINE_RATES["lathe"] / q
        cost_per   = mat_cost + setup_cost + op_cost_lathe + op_cost_bench + op_cost_ctrl
        price_net  = round(cost_per * (1 + _MARGIN / (1 - _MARGIN)), 0)
        price_vat  = round(price_net * _VAT, 0)
        tiers.append({
            "qty":        q,
            "cost":       round(cost_per, 0),
            "price_net":  price_net,
            "price_vat":  price_vat,
        })

    return {
        "material":    mat["name"],
        "blank_d":     blank_d,
        "blank_l":     round(blank_l, 0),
        "blank_mass_g": round(m_blank, 1),
        "part_mass_g":  round(m_part, 1),
        "kim":          kim,
        "mat_cost":    round(mat_cost, 0),
        "tiers":        tiers,
        "vat_rate":    _VAT,
    }


# ── Заявка из «Калькулятора металла» (inline Mini App → очередь → бот доставит) ──
#   Inline-Mini-App не может Telegram.WebApp.sendData(), а TELEGRAM_TOKEN на Render
#   может быть не задан. Поэтому копим заявки в очередь; локальный бот забирает их
#   (GET /api/metal-orders, каждые ~2 мин) и доставляет владельцу своим токеном/SMTP.
_metal_orders: list[dict] = []

@app.post("/api/metal-order")
async def api_metal_order(request: Request, x_telegram_init_data: str = Header(default="")):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if body.get("dryrun"):
        return {"ok": True}
    items = body.get("items") or []
    if not items:
        return JSONResponse({"ok": False, "reason": "empty"}, status_code=400)
    # Заказчик: сначала строгая проверка подписи; если токена на Render нет —
    # парсим initData без подписи; в крайнем случае берём user из тела запроса
    # (frontend шлёт TG.initDataUnsafe.user). Нужно лишь знать, с кем связаться.
    user = _verify_init_data(x_telegram_init_data)
    if not user:
        user = _user_from_init_unverified(x_telegram_init_data)
    if not user and isinstance(body.get("user"), dict):
        user = body.get("user")
    user = user or {}
    who = (str(user.get("first_name", "")) +
           (" @" + user.get("username") if user.get("username") else "")).strip()
    _metal_orders.append({
        "kind":         (body.get("kind") or "order"),   # order = заявка владельцу; download = файл заказчику
        "items":        items,
        "total_mass_g": body.get("total_mass_g") or 0,
        "total_cost":   body.get("total_cost") or 0,
        "who":          who,
        "uid":          user.get("id"),
        "username":     user.get("username") or "",
        "first_name":   user.get("first_name") or "",
        "last_name":    user.get("last_name") or "",
        "ts":           time.time(),
    })
    if len(_metal_orders) > 200:
        del _metal_orders[:-200]
    return {"ok": True, "queued": True}

@app.get("/api/metal-orders")
def api_metal_orders(_sec=Depends(require_pull_secret)):
    """Бот забирает накопленные заявки и очищает очередь."""
    global _metal_orders
    out = _metal_orders[:]
    _metal_orders = []
    return {"orders": out}


# ── 🤝 КООПЕРАЦИЯ — Mini App биржи заказов ───────────────────────────────────
#   Чтение (лента / мои заказы / мои отклики) — напрямую из Sheets (кэш 120с).
#   Запись (размещение / отклик) — в очередь _coop_actions; бот опрашивает
#   GET /api/coop/actions (~6с) и исполняет со ВСЕЙ логикой (пуш исполнителям,
#   рейтинг, контакт-стена, подписка — живут в users.json на стороне бота),
#   затем шлёт подтверждение/ошибку заказчику в чат. server.py НЕ пишет в Sheets.
_COOP_STATUS_OPEN = "Открыт"
_coop_actions: list[dict] = []

def _coop_who(init_data: str, body: dict | None = None) -> dict:
    # АНТИ-СПУФИНГ (аудит 2026-06-17): при заданном TELEGRAM_TOKEN доверяем ТОЛЬКО
    # валидной подписи Telegram. Иначе подделанный initData (любой id) выдавал бы
    # себя за автора заказа (чужие ценовые офферы) или чужого исполнителя.
    u = _verify_init_data(init_data)
    if u is not None:
        return u                       # подпись валидна — доверяем
    if _BOT_TOKEN:
        # токен задан, но подпись не прошла → подделка/чужой initData → НЕ доверяем
        return {}
    # TELEGRAM_TOKEN не задан (Render не настроен) → деградируем к прежнему
    # поведению (неподписанный user), чтобы не сломать текущий прод до настройки токена
    u = _user_from_init_unverified(init_data)
    if not u and isinstance((body or {}).get("user"), dict):
        u = body["user"]
    return u or {}

def _coop_order_pub(o: dict, myid: str = "", replied=frozenset()) -> dict:
    """Безопасное представление заказа для ленты (БЕЗ контактов заказчика)."""
    oid = _s(o.get("ID"))
    ops = [x.strip() for x in _s(o.get("Операции")).split(",") if x.strip()]
    return {
        "id": oid, "date": _s(o.get("Дата")),
        "title": _s(o.get("Наименование")), "material": _s(o.get("Материал")),
        "ops": ops, "qty": _s(o.get("Количество")), "ddl": _s(o.get("Срок")),
        "city": _s(o.get("Город")), "budget": _s(o.get("Бюджет")),
        "replies": _s(o.get("Откликов")) or "0",
        "is_mine": bool(myid) and _s(o.get("Заказчик_uid")) == myid,
        "replied": oid in replied,
        "has_files": bool(_s(o.get("Файлы"))),
    }

@app.get("/api/coop/feed")
def api_coop_feed(x_telegram_init_data: str = Header(default="")):
    me = _coop_who(x_telegram_init_data)
    myid = str(me.get("id") or "")
    orders = [o for o in _sheet_rows("Заказы_Кооп") if _s(o.get("Статус")) == _COOP_STATUS_OPEN]
    replied = set()
    if myid:
        for r in _sheet_rows("Отклики_Кооп"):
            if _s(r.get("Исполнитель_uid")) == myid:
                replied.add(_s(r.get("Заказ_ID")))
    out = [_coop_order_pub(o, myid, replied) for o in orders]
    out.sort(key=lambda x: x.get("date", ""), reverse=True)
    return {"orders": out, "uid": myid}

@app.get("/api/coop/my")
def api_coop_my(x_telegram_init_data: str = Header(default="")):
    me = _coop_who(x_telegram_init_data)
    myid = str(me.get("id") or "")
    if not myid:
        return {"orders": [], "responses": [], "uid": ""}
    my_orders = [_coop_order_pub(o, myid) for o in _sheet_rows("Заказы_Кооп")
                 if _s(o.get("Заказчик_uid")) == myid]
    my_orders.sort(key=lambda x: x.get("date", ""), reverse=True)
    resp = []
    for r in _sheet_rows("Отклики_Кооп"):
        if _s(r.get("Исполнитель_uid")) == myid:
            resp.append({"id": _s(r.get("ID")), "oid": _s(r.get("Заказ_ID")),
                         "date": _s(r.get("Дата")), "price": _s(r.get("Цена")),
                         "srok": _s(r.get("Срок")), "status": _s(r.get("Статус")),
                         "comment": _s(r.get("Комментарий"))})
    resp.sort(key=lambda x: x.get("date", ""), reverse=True)
    return {"orders": my_orders, "responses": resp, "uid": myid}

@app.post("/api/coop/place")
async def api_coop_place(request: Request, x_telegram_init_data: str = Header(default="")):
    try:
        body = await request.json()
    except Exception:
        body = {}
    me = _coop_who(x_telegram_init_data, body)
    uid = me.get("id")
    if not uid:
        return JSONResponse({"ok": False, "reason": "no_user"}, status_code=401)
    title = (body.get("title") or "").strip()
    if not title:
        return JSONResponse({"ok": False, "reason": "no_title"}, status_code=400)
    _coop_actions.append({
        "action": "place", "uid": uid,
        "title": title[:140], "material": str(body.get("material") or "")[:90],
        "ops": [str(x)[:40] for x in (body.get("ops") or [])][:12],
        "qty": str(body.get("qty") or "")[:40], "ddl": str(body.get("ddl") or "")[:60],
        "city": str(body.get("city") or "")[:60], "budget": str(body.get("budget") or "")[:40],
        "ts": time.time(),
    })
    del _coop_actions[:-300]
    return {"ok": True, "queued": True}

@app.post("/api/coop/respond")
async def api_coop_respond(request: Request, x_telegram_init_data: str = Header(default="")):
    try:
        body = await request.json()
    except Exception:
        body = {}
    me = _coop_who(x_telegram_init_data, body)
    uid = me.get("id")
    if not uid:
        return JSONResponse({"ok": False, "reason": "no_user"}, status_code=401)
    oid = (body.get("oid") or "").strip()
    price = int("".join(ch for ch in str(body.get("price") or "") if ch.isdigit()) or 0)
    if not oid or price <= 0:
        return JSONResponse({"ok": False, "reason": "bad"}, status_code=400)
    _coop_actions.append({
        "action": "respond", "uid": uid, "oid": oid, "price": price,
        "srok": str(body.get("srok") or "")[:40], "comment": str(body.get("comment") or "")[:300],
        "ts": time.time(),
    })
    del _coop_actions[:-300]
    return {"ok": True, "queued": True}

def _coop_trust_map() -> dict:
    """uid → строка профиля доверия из листа «Доверие_Кооп» (бот зеркалит туда users.json)."""
    m = {}
    for r in _sheet_rows("Доверие_Кооп"):
        u = _s(r.get("uid"))
        if u:
            m[u] = r
    return m

def _coop_trust_badge_s(r: dict) -> str:
    if not r:
        return ""
    p = []
    if _s(r.get("Проверена")) == "1":
        p.append("✅ Проверена")
    elif _s(r.get("ИНН_ок")) == "1":
        p.append("🛡 ИНН")
    d = _s(r.get("Сделок"))
    if d and d != "0":
        p.append("🤝 " + d)
    return " · ".join(p)

@app.get("/api/coop/responses")
def api_coop_responses(oid: str = "", x_telegram_init_data: str = Header(default="")):
    """Отклики по заказу — ТОЛЬКО автору заказа. Контакты исполнителей НЕ отдаём
    (контакт-стена): открытие контакта/выбор идут через бота (гейтинг в users.json)."""
    me = _coop_who(x_telegram_init_data)
    myid = str(me.get("id") or "")
    if not oid or not myid:
        return {"ok": False, "reason": "bad", "responses": []}
    order = next((o for o in _sheet_rows("Заказы_Кооп") if _s(o.get("ID")) == oid), None)
    if not order:
        return {"ok": False, "reason": "no_order", "responses": []}
    if _s(order.get("Заказчик_uid")) != myid:
        return {"ok": False, "reason": "not_author", "responses": []}
    tmap = _coop_trust_map()
    reps = []
    for r in _sheet_rows("Отклики_Кооп"):
        if _s(r.get("Заказ_ID")) != oid:
            continue
        tr = tmap.get(_s(r.get("Исполнитель_uid"))) or {}
        reps.append({"id": _s(r.get("ID")), "name": _s(r.get("Исполнитель_имя")),
                     "card": _s(r.get("Исполнитель_карточка")), "price": _s(r.get("Цена")),
                     "srok": _s(r.get("Срок")), "comment": _s(r.get("Комментарий")),
                     "status": _s(r.get("Статус")),
                     "chosen": _s(r.get("Статус")) == "Выбран",
                     "trust": _coop_trust_badge_s(tr),
                     "promo": _s(tr.get("Промо")) == "1"})
    # реклама: продвигаемые исполнители выше, внутри групп — по цене
    reps.sort(key=lambda x: (0 if x["promo"] else 1,
                             int("".join(ch for ch in x["price"] if ch.isdigit()) or 10**12)))
    return {"ok": True, "title": _s(order.get("Наименование")),
            "status": _s(order.get("Статус")), "responses": reps}

def _coop_enqueue_rid(action: str, init_data: str, body: dict):
    me = _coop_who(init_data, body)
    uid = me.get("id")
    rid = (body.get("rid") or "").strip()
    if not uid:
        return JSONResponse({"ok": False, "reason": "no_user"}, status_code=401)
    if not rid:
        return JSONResponse({"ok": False, "reason": "bad"}, status_code=400)
    _coop_actions.append({"action": action, "uid": uid, "rid": rid, "ts": time.time()})
    del _coop_actions[:-300]
    return {"ok": True, "queued": True}

@app.post("/api/coop/open")
async def api_coop_open(request: Request, x_telegram_init_data: str = Header(default="")):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return _coop_enqueue_rid("open", x_telegram_init_data, body)

@app.post("/api/coop/choose")
async def api_coop_choose(request: Request, x_telegram_init_data: str = Header(default="")):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return _coop_enqueue_rid("choose", x_telegram_init_data, body)

@app.get("/api/coop/trust")
def api_coop_trust(x_telegram_init_data: str = Header(default="")):
    """Профиль доверия текущего пользователя для экрана «Доверие» в мини-аппе."""
    me = _coop_who(x_telegram_init_data)
    myid = str(me.get("id") or "")
    r = (_coop_trust_map().get(myid) or {}) if myid else {}
    return {"uid": myid, "inn": _s(r.get("ИНН")), "inn_ok": _s(r.get("ИНН_ок")) == "1",
            "verified": _s(r.get("Проверена")) == "1", "org": _s(r.get("Организация")),
            "deals": _s(r.get("Сделок")) or "0", "promo": _s(r.get("Промо")) == "1"}

@app.post("/api/coop/inn")
async def api_coop_inn(request: Request, x_telegram_init_data: str = Header(default="")):
    try:
        body = await request.json()
    except Exception:
        body = {}
    me = _coop_who(x_telegram_init_data, body)
    uid = me.get("id")
    if not uid:
        return JSONResponse({"ok": False, "reason": "no_user"}, status_code=401)
    inn = "".join(ch for ch in str(body.get("inn") or "") if ch.isdigit())
    if len(inn) not in (10, 12):
        return JSONResponse({"ok": False, "reason": "bad"}, status_code=400)
    _coop_actions.append({"action": "inn", "uid": uid, "inn": inn, "ts": time.time()})
    del _coop_actions[:-300]
    return {"ok": True, "queued": True}

@app.post("/api/coop/promoreq")
async def api_coop_promoreq(request: Request, x_telegram_init_data: str = Header(default="")):
    try:
        body = await request.json()
    except Exception:
        body = {}
    me = _coop_who(x_telegram_init_data, body)
    uid = me.get("id")
    if not uid:
        return JSONResponse({"ok": False, "reason": "no_user"}, status_code=401)
    _coop_actions.append({"action": "promoreq", "uid": uid, "ts": time.time()})
    del _coop_actions[:-300]
    return {"ok": True, "queued": True}

@app.get("/api/coop/profiles")
def api_coop_profiles(x_telegram_init_data: str = Header(default="")):
    """Мои активные профили подписки (для умного пуша заказов)."""
    me = _coop_who(x_telegram_init_data)
    myid = str(me.get("id") or "")
    if not myid:
        return {"profiles": []}
    out = []
    for r in _sheet_rows("Профили_подписки"):
        if _s(r.get("Исполнитель_uid")) != myid:
            continue
        if _s(r.get("Активен")).lower() not in ("да", "1", "true", ""):
            continue
        out.append({"id": _s(r.get("ID")),
                    "ops": [x.strip() for x in _s(r.get("Операции")).split(",") if x.strip()],
                    "mats": [x.strip() for x in _s(r.get("Материалы")).split(",") if x.strip()],
                    "city": _s(r.get("Город")), "budget": _s(r.get("Бюджет_от"))})
    return {"profiles": out}

@app.post("/api/coop/profile")
async def api_coop_profile(request: Request, x_telegram_init_data: str = Header(default="")):
    try:
        body = await request.json()
    except Exception:
        body = {}
    me = _coop_who(x_telegram_init_data, body)
    uid = me.get("id")
    if not uid:
        return JSONResponse({"ok": False, "reason": "no_user"}, status_code=401)
    ops = [str(x)[:40] for x in (body.get("ops") or []) if str(x).strip()][:12]
    if not ops:
        return JSONResponse({"ok": False, "reason": "no_ops"}, status_code=400)
    sid = (body.get("sid") or "").strip()          # есть sid → редактирование, нет → создание
    item = {"action": "profile_edit" if sid else "profile_add", "uid": uid, "ops": ops,
            "mats": [str(x)[:40] for x in (body.get("mats") or [])][:12],
            "city": str(body.get("city") or "")[:60],
            "budget": str(body.get("budget") or "")[:40], "ts": time.time()}
    if sid:
        item["sid"] = sid
    _coop_actions.append(item)
    del _coop_actions[:-300]
    return {"ok": True, "queued": True}

@app.post("/api/coop/profile_del")
async def api_coop_profile_del(request: Request, x_telegram_init_data: str = Header(default="")):
    try:
        body = await request.json()
    except Exception:
        body = {}
    me = _coop_who(x_telegram_init_data, body)
    uid = me.get("id")
    sid = (body.get("sid") or "").strip()
    if not uid:
        return JSONResponse({"ok": False, "reason": "no_user"}, status_code=401)
    if not sid:
        return JSONResponse({"ok": False, "reason": "bad"}, status_code=400)
    _coop_actions.append({"action": "profile_del", "uid": uid, "sid": sid, "ts": time.time()})
    del _coop_actions[:-300]
    return {"ok": True, "queued": True}

@app.get("/api/coop/actions")
def api_coop_actions(_sec=Depends(require_pull_secret)):
    """Бот забирает накопленные действия Кооперации (размещение/отклик/открыть/выбрать/инн/промо/профиль)."""
    global _coop_actions
    out = _coop_actions[:]
    _coop_actions = []
    return {"actions": out}


# ── 🏭 Парк оборудования клиента (мини-апп self-service) → очередь _coop_actions ──
@app.post("/api/equipment")
async def api_equipment_save(request: Request, x_telegram_init_data: str = Header(default="")):
    """Клиент сохраняет СВОЙ парк станков из мини-аппа. uid из подписи Telegram;
    действие equip_set → бот применяет в users.json (фото существующих станков сохраняет)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    me  = _coop_who(x_telegram_init_data, body)
    uid = me.get("id")
    if not uid:
        return JSONResponse({"ok": False, "reason": "no_user"}, status_code=401)
    park, clean, seen = body.get("park") or [], [], set()
    for it in park[:30]:
        if isinstance(it, dict) and it.get("key"):
            k = str(it["key"])[:24]
            if k in seen:
                continue
            seen.add(k)
            clean.append({"key": k, "specs": str(it.get("specs") or "")[:80]})
    _coop_actions.append({"action": "equip_set", "uid": uid, "park": clean, "ts": time.time()})
    del _coop_actions[:-300]
    return {"ok": True, "queued": True, "count": len(clean)}


@app.post("/api/equipment/photo")
async def api_equipment_photo_upload(request: Request, x_telegram_init_data: str = Header(default="")):
    """Клиент загружает ФОТО станка из мини-аппа (сжатый base64) → очередь equip_photo;
    бот декодирует и грузит в Telegram (получает file_id). Работает без токена на Render."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    me  = _coop_who(x_telegram_init_data, body)
    uid = me.get("id")
    if not uid:
        return JSONResponse({"ok": False, "reason": "no_user"}, status_code=401)
    key = str(body.get("key") or "")[:24]
    img = str(body.get("image_b64") or "")
    if not key or not img or len(img) > 700000:        # ~520 КБ base64
        return JSONResponse({"ok": False, "reason": "bad"}, status_code=400)
    _coop_actions.append({"action": "equip_photo", "uid": uid, "key": key,
                          "image_b64": img, "ts": time.time()})
    del _coop_actions[:-300]
    return {"ok": True, "queued": True}


@app.get("/api/equipment/photo")
async def api_equipment_photo_get(fid: str):
    """Прокси Telegram-фото станка по file_id (через TELEGRAM_TOKEN). Без токена → 404
    (фото хранятся в Telegram; показ в окне требует токен на Render)."""
    import httpx
    from fastapi.responses import StreamingResponse
    if not _BOT_TOKEN or not fid or len(fid) > 250:
        raise HTTPException(404, "Фото недоступно")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r1 = await client.get(f"https://api.telegram.org/bot{_BOT_TOKEN}/getFile",
                                  params={"file_id": fid})
            j = r1.json()
            if not j.get("ok"):
                raise HTTPException(404, "Фото не найдено")
            fpath = j["result"]["file_path"]
            r2 = await client.get(f"https://api.telegram.org/file/bot{_BOT_TOKEN}/{fpath}")
            if r2.status_code != 200:
                raise HTTPException(502, "Ошибка загрузки фото")
            return StreamingResponse(iter([r2.content]), media_type="image/jpeg",
                                     headers={"Cache-Control": "public, max-age=86400"})
    except httpx.RequestError:
        raise HTTPException(502, "Ошибка сети")


# ── Прайсы-файлы компаний (хранятся в Telegram через бота; в листе только file_id) ──
_company_files_q: list[dict] = []   # очередь: attach (бот попросит файл) / get (бот пришлёт файл)

@app.post("/api/company-file")
async def api_company_file(request: Request, x_telegram_init_data: str = Header(default="")):
    """Действие с прайсом компании: attach (прикрепить — бот попросит файл в чат)
    или get (получить — бот пришлёт файл). Кладём в очередь, бот забирает."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    action  = body.get("action")
    company = (body.get("company") or "").strip()
    if action not in ("attach", "get") or not company:
        return JSONResponse({"ok": False, "reason": "bad_request"}, status_code=400)
    user = _verify_init_data(x_telegram_init_data)
    if not user:
        user = _user_from_init_unverified(x_telegram_init_data)
    if not user and isinstance(body.get("user"), dict):
        user = body.get("user")
    user = user or {}
    uid = user.get("id")
    if not uid:
        return JSONResponse({"ok": False, "reason": "no_user"}, status_code=400)
    _company_files_q.append({
        "action":  action,
        "company": company,
        "file_id": body.get("file_id", ""),
        "fname":   body.get("fname", ""),
        "uid":     uid,
        "ts":      time.time(),
    })
    if len(_company_files_q) > 200:
        del _company_files_q[:-200]
    return {"ok": True, "queued": True}

@app.get("/api/company-file-queue")
def api_company_file_queue(_sec=Depends(require_pull_secret)):
    """Бот забирает очередь действий с прайсами и очищает."""
    global _company_files_q
    out = _company_files_q[:]
    _company_files_q = []
    return {"actions": out}

@app.get("/api/company-files/{company:path}")
def api_company_files(company: str, _auth: dict = Depends(require_manager)):
    """Список прикреплённых прайсов компании (для показа в карточке)."""
    out = []
    for r in _sheet_rows("Прайсы_Файлы"):
        if _norm(_s(r.get("Компания"))) == _norm(company):
            out.append({
                "name":    _s(r.get("Файл")),
                "date":    _s(r.get("Дата")),
                "by":      _s(r.get("Кто")),
                "file_id": _s(r.get("File_ID")),
                "size":    _s(r.get("Размер")),
            })
    out.reverse()   # свежие сверху
    return {"files": out}


# ── МОДУЛЬ КП (перенесён из v2.1) — заявка → подбор из прайса × наценка → клиенту ──
# Лист "Прайс": Наименование | Марка | Размер | Единица | Цена_закуп | Поставщик | Наличие
def _dec(raw) -> Decimal:
    """Деньги в Decimal: '1 234,56 ₽'/NBSP/'1,234.56' → Decimal (float не считаем). Отдельно от float _money."""
    s = _MONEY_CLEAN.sub("", _s(raw).replace("\xa0", "").replace(" ", ""))
    if not s or s in ("-", ".", ","):
        return Decimal("0")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
        if s.count(".") > 1:
            s = s.replace(".", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal("0")

def _price_rows() -> list[dict]:
    out = []
    for r in _sheet_rows(PRICE_SHEET):
        name = _s(r.get("Наименование"))
        cost = _dec(r.get("Цена_закуп") or r.get("Цена"))
        if not name or cost <= 0:
            continue
        out.append({"name": name, "mark": _s(r.get("Марка")), "size": _s(r.get("Размер")),
                    "unit": _s(r.get("Единица")) or "шт", "cost": cost,
                    "supplier": _s(r.get("Поставщик")), "stock": _s(r.get("Наличие"))})
    return out

def _match_price(query: str, rows: list[dict]) -> tuple[Optional[dict], bool]:
    """Лучшее совпадение по словам запроса. (строка, точное_ли)."""
    words = [w for w in re.split(r"[\s,;]+", query.lower().strip()) if w]
    if not words:
        return None, False
    best, best_score = None, 0
    for r in rows:
        hay = f'{r["name"]} {r["mark"]} {r["size"]}'.lower()
        score = sum(1 for w in words if w in hay)
        if score > best_score:
            best, best_score = r, score
    if best is None or best_score == 0:
        return None, False
    return best, best_score == len(words)

class QuoteItem(BaseModel):
    query: str
    qty: float = 1

class QuoteRequest(BaseModel):
    client: str = ""
    items: list[QuoteItem]
    markup: Optional[float] = None
    delivery_cost: float = 0

def _fmt_rub(d: Decimal) -> str:
    return f"{d:,.2f}".replace(",", " ").replace(".", ",") + " ₽"

def _format_quote_client(client, lines, subtotal, delivery, total, vat) -> str:
    """Текст КП для клиента: НИКАКИХ закупочных цен, поставщиков и маржи."""
    out = ["📋 <b>КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ</b>"]
    if client:
        out.append(f"Для: <b>{client}</b>")
    out.append(f"Дата: {datetime.now().strftime('%d.%m.%Y')}")
    out.append("")
    for i, l in enumerate(lines, 1):
        title = " ".join(x for x in (l["name"], l["mark"], l["size"]) if x)
        out.append(f"{i}. {title}")
        out.append(f"   {l['qty']} {l['unit']} × {_fmt_rub(l['price'])} = <b>{_fmt_rub(l['sum'])}</b>")
    out.append("")
    if delivery > 0:
        out.append(f"Товары: {_fmt_rub(subtotal)}")
        out.append(f"Доставка: {_fmt_rub(delivery)}")
    out.append(f"💰 <b>ИТОГО: {_fmt_rub(total)}</b>")
    out.append(f"в т.ч. НДС 20%: {_fmt_rub(vat)}")
    out.append("")
    out.append("Предложение действительно 3 рабочих дня.")
    return "\n".join(out)

@app.get("/api/price")
def api_price(q: str = "", _auth: dict = Depends(require_manager)):
    """Прайс с закупочными ценами — ТОЛЬКО менеджер/админ (коммерческая тайна)."""
    rows = _price_rows()
    if q:
        n = q.lower().strip()
        rows = [r for r in rows if n in f'{r["name"]} {r["mark"]} {r["size"]} {r["supplier"]}'.lower()]
    return {"items": [{**r, "cost": float(r["cost"])} for r in rows], "total": len(rows)}

@app.post("/api/quote")
def api_quote(body: QuoteRequest, _auth: dict = Depends(require_manager)):
    """Расчёт КП: позиции из прайса × (1 + наценка). client_text — без закупа/поставщиков/маржи."""
    if not body.items:
        raise HTTPException(400, "Нет позиций")
    rows = _price_rows()
    if not rows:
        raise HTTPException(503, f"Лист '{PRICE_SHEET}' пуст или не создан — добавьте прайс в Google Sheets")
    markup = Decimal(str(body.markup)) if body.markup is not None else QUOTE_MARKUP
    if not (Decimal("0") <= markup <= Decimal("5")):
        raise HTTPException(400, "Наценка вне диапазона 0–500%")
    lines, missing = [], []
    cost_total = Decimal("0")
    for it in body.items:
        row, exact = _match_price(it.query, rows)
        if row is None:
            missing.append(it.query); continue
        qty = Decimal(str(it.qty))
        price = (row["cost"] * (Decimal("1") + markup)).quantize(Decimal("0.01"))
        line_sum = (price * qty).quantize(Decimal("0.01"))
        cost_total += (row["cost"] * qty)
        lines.append({"query": it.query, "name": row["name"], "mark": row["mark"],
                      "size": row["size"], "unit": row["unit"], "qty": float(qty),
                      "price": price, "sum": line_sum, "supplier": row["supplier"],
                      "exact_match": exact})
    if not lines:
        raise HTTPException(404, f"Ничего не найдено в прайсе: {', '.join(missing)}")
    subtotal = sum((l["sum"] for l in lines), Decimal("0"))
    delivery = Decimal(str(body.delivery_cost)).quantize(Decimal("0.01"))
    total = subtotal + delivery
    vat = (total * Decimal("20") / Decimal("120")).quantize(Decimal("0.01"))
    margin = (subtotal - cost_total).quantize(Decimal("0.01"))
    client_text = _format_quote_client(body.client, lines, subtotal, delivery, total, vat)
    logger.info("[quote] %s: %d поз., итог %s, маржа %s", body.client or "—", len(lines), total, margin)
    return {"ok": True, "client": body.client,
            "lines": [{**l, "price": float(l["price"]), "sum": float(l["sum"])} for l in lines],
            "missing": missing, "subtotal": float(subtotal), "delivery": float(delivery),
            "total": float(total), "vat_included": float(vat), "markup_used": float(markup),
            "margin": float(margin),                 # ТОЛЬКО менеджеру, клиенту не отправлять
            "client_text": client_text}


# ── Static ────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return FileResponse(
        WEBAPP_DIR / "contact.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
    )

@app.get("/contact.html")
def contact_html():
    return FileResponse(
        WEBAPP_DIR / "contact.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
    )

@app.get("/docs.html")
def docs_html():
    return FileResponse(
        WEBAPP_DIR / "docs.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
    )

@app.get("/calc.html")
def calc_html():
    return FileResponse(
        WEBAPP_DIR / "calc.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
    )

@app.get("/calc_hud.html")
def calc_hud_html():
    return FileResponse(
        WEBAPP_DIR / "calc_hud.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
    )

@app.get("/tolerances.html")
def tolerances_html():
    return FileResponse(
        WEBAPP_DIR / "tolerances.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
    )

@app.get("/coop.html")
def coop_html():
    return FileResponse(
        WEBAPP_DIR / "coop.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
    )

@app.get("/equipment.html")
def equipment_html():
    return FileResponse(
        WEBAPP_DIR / "equipment.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
    )

@app.get("/nav.js")
def nav_js():
    # Общая кнопка «Назад» для всех модулей мини-аппа
    return FileResponse(
        WEBAPP_DIR / "nav.js",
        media_type="text/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
    )

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    logger.info(f"METALLBOT Mini App → http://127.0.0.1:{PORT}")
    logger.info(f"API docs          → http://127.0.0.1:{PORT}/docs")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
