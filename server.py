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
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI(title="METALLBOT Mini App")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

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
    return str(v).strip() if v else ""

def _row_to_contact(row: dict, kind: str) -> dict:
    name = _s(row.get("Название") or row.get("Компания") or row.get("Поставщик"))
    return {
        "id":             name,
        "kind":           kind,
        "name":           name,
        "city":           _s(row.get("Город")),
        "phone":          _s(row.get("Телефон")),
        "email":          _s(row.get("Email")),
        "contact":        _s(row.get("Контакт") or row.get("Менеджер")),
        "specialization": _s(row.get("Специализация") or row.get("Вид металла/услуги")),
        "service":        _s(row.get("Виды_работ") or row.get("Вид услуги") or row.get("Вид покрытия")),
        "equipment":      _s(row.get("Оборудование")),
        "materials":      _s(row.get("Материалы")),
        "status":         _s(row.get("Статус")),
        "rating":         _s(row.get("Рейтинг")),
        "price_level":    _s(row.get("Цена_уровень")),
        "notes":          _s(row.get("Заметки") or row.get("Примечание")),
        "media":          _s(row.get("Медиафайлы")),
        "added":          _s(row.get("Добавлено") or row.get("Дата")),
        "added_by":       _s(row.get("Кто_добавил")),
        "raw":            {k: _s(v) for k, v in row.items()},
    }

def _load_contacts() -> list[dict]:
    contacts = []
    for sheet, kind in [
        ("Кооперация", "coop"),
        ("Поставщики",  "supplier"),
    ]:
        for r in _sheet_rows(sheet):
            c = _row_to_contact(r, kind)
            if c["name"]:
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

# ── Static ────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return FileResponse(WEBAPP_DIR / "contact.html")

@app.get("/contact.html")
def contact_html():
    return FileResponse(WEBAPP_DIR / "contact.html")

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    logger.info(f"METALLBOT Mini App → http://127.0.0.1:{PORT}")
    logger.info(f"API docs          → http://127.0.0.1:{PORT}/docs")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
