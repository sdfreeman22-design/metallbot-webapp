"""
METALLBOT Mini App — локальный HTTP-сервер.
Автономный: читает Google Sheets напрямую через .env родительской папки.
Запуск: python server.py  (из папки webapp/)
"""
import json
import logging
import os
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
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="METALLBOT Mini App")

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

# ── Авторизация Mini App (Telegram initData, HMAC-SHA256 от токена бота) ───────
import hashlib as _hashlib
import hmac as _hmac
from urllib.parse import parse_qsl as _parse_qsl
from fastapi import Header, Depends

_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

def _load_allowed_ids() -> set:
    """ID Telegram, которым разрешены изменения (админы + менеджеры из users.json)."""
    ids: set = set()
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
        user = pairs.get("user")
        return json.loads(user) if user else {}
    except Exception as e:
        logger.warning("[auth] verify: %s", e)
        return None

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
        "status":         _s(row.get("Статус")),
        "rating":         _s(row.get("Рейтинг")),
        "price_level":    _s(row.get("Цена_уровень")),
        "notes":          _s(row.get("Заметки") or row.get("Примечание")),
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

def _parsed_data() -> dict[str, dict]:
    """Читает лист 'Парсинг' → dict {normalized_name: data}."""
    try:
        rows = _sheet_rows("Парсинг")
        result = {}
        for row in rows:
            name = _s(row.get("Компания", ""))
            if name:
                result[_norm(name)] = row
        return result
    except Exception:
        return {}


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

    return contacts

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
        rat = -float(c.get("rating") or 0)
    except Exception:
        rat = 0
    return (tier, rat)

@app.get("/api/contacts")
def api_contacts(q: str = "", kind: str = ""):
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
    return {"items": items, "total": len(items)}

@app.get("/api/contact/{contact_id:path}")
def api_contact(contact_id: str):
    items = _load_contacts()
    target = next(
        (x for x in items
         if x["id"].lower() == contact_id.lower()
         or x["name"].lower() == contact_id.lower()),
        None,
    )
    if not target:
        raise HTTPException(404, f"Не найден: {contact_id}")

    # Добавляем парсинговые данные из листа "Парсинг"
    parsed = _parsed_data()
    p = parsed.get(_norm(target["name"]), {})
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

# Маппинг поля → возможные названия колонок в листах
_FIELD_COLS: dict[str, list[str]] = {
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
def api_cache_bust(sheets: list[str] = None):
    """Явный сброс кэша (для тестов)."""
    for s in (sheets or ["Кооперация"]):
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


@app.get("/api/operations")
def api_operations():
    """
    Возвращает топ операций по базе кооператоров для фильтр-чипов в Mini App.
    Парсит поля Виды_работ, Услуги_парс, Специализация, Оборудование.
    """
    import re as _re
    # Ключевые операции — в том же порядке что в боте (_OP_KW)
    OP_KEYS = [
        ("Токарная",         ["токарн", "токарк", "lathe"]),
        ("Фрезерная",        ["фрезер", "фрезеровк", "фрезерн", "milling"]),
        ("Шлифовальная",     ["шлифов", "шлифовк", "grinding"]),
        ("Термообработка",   ["термо", "закалк", "отжиг", "улучшение", "heat"]),
        ("Покрытие",         ["покрыт", "гальван", "цинков", "хромиров", "никелир", "анодир"]),
        ("Сварка",           ["сварк", "сварочн", "welding"]),
        ("Лазерная резка",   ["лазерн", "laser"]),
        ("Штамповка",        ["штамп", "прессов", "stamp"]),
        ("Литьё",            ["литьё", "литья", "литейн", "casting"]),
        ("Слесарная",        ["слесарн", "слесарк"]),
        ("Электроэрозионная",["эрозионн", "электроэроз", "wire", "edm"]),
    ]
    contacts = _load_contacts()
    coops = [c for c in contacts if c["kind"] in ("coop", "both")]
    counts: dict[str, int] = {}
    for op_label, kws in OP_KEYS:
        cnt = 0
        for c in coops:
            text = " ".join([
                c.get("service", ""), c.get("equipment", ""),
                c.get("specialization", ""), c.get("materials", ""), c.get("notes", "")
            ]).lower()
            if any(kw in text for kw in kws):
                cnt += 1
        if cnt > 0:
            counts[op_label] = cnt
    # Сортируем по убыванию
    result = [{"op": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]
    return {"operations": result}


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
def api_cache_clear():
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
    # SSRF-защита: запрет приватных/локальных адресов
    if not _is_public_host(parsed.hostname or ""):
        raise HTTPException(400, "Недопустимый адрес источника")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "image/*,*/*;q=0.8",
        "Referer": f"{parsed.scheme}://{parsed.netloc}/",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15,
                                     verify=False) as client:
            async with client.stream("GET", url, headers=headers) as r:
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

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    logger.info(f"METALLBOT Mini App → http://127.0.0.1:{PORT}")
    logger.info(f"API docs          → http://127.0.0.1:{PORT}/docs")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
