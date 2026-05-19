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
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

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
    coop_list = [_row_to_contact(r, "coop")
                 for r in _sheet_rows("Кооперация") if _row_to_contact(r, "coop")["name"]]
    supp_list = [_row_to_contact(r, "supplier")
                 for r in _sheet_rows("Поставщики") if _row_to_contact(r, "supplier")["name"]]

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
                 or n in x["equipment"].lower()]
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
    "status":         ["Статус"],
    "rating":         ["Рейтинг"],
    "notes":          ["Заметки", "Примечание"],
}

@app.put("/api/contact/{contact_id:path}")
def api_contact_update(contact_id: str, body: ContactUpdate):
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

        except Exception as e:
            logger.warning("[update] %s: %s", sheet_name, e)

    if not updated_sheets:
        raise HTTPException(404, f"Компания не найдена: {contact_id}")

    return {"ok": True, "updated": updated_sheets}


@app.delete("/api/contact/{contact_id:path}")
def api_contact_delete(contact_id: str):
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

@app.get("/api/img")
async def api_img_proxy(url: str):
    """Проксирует изображение с сайта партнёра — обходит CORS и hotlink-защиту."""
    import httpx
    from fastapi.responses import StreamingResponse
    from urllib.parse import urlparse

    # Базовая защита — только картинки
    allowed_ext = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    path = urlparse(url).path.lower()
    if not any(path.endswith(e) for e in allowed_ext):
        raise HTTPException(400, "Только изображения")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "image/*,*/*;q=0.8",
        "Referer": f"{urlparse(url).scheme}://{urlparse(url).netloc}/",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15,
                                     verify=False) as client:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                raise HTTPException(502, f"Источник вернул {r.status_code}")
            ct = r.headers.get("content-type", "image/jpeg")
            return StreamingResponse(
                iter([r.content]),
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

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    logger.info(f"METALLBOT Mini App → http://127.0.0.1:{PORT}")
    logger.info(f"API docs          → http://127.0.0.1:{PORT}/docs")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
