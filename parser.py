"""
METALLBOT — Парсер сайтов партнёров.
Извлекает текст, структурированные данные и фотографии с сайта компании.
Использует Claude AI для интеллектуального анализа контента.

Использование:
    from parser import SiteParser
    result = await SiteParser.parse("https://example.com", company_name="Компания")
"""

import asyncio
import hashlib
import io
import logging
import mimetypes
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import aiohttp
import anthropic
import gspread
import json as _json
from bs4 import BeautifulSoup, Comment
from google.oauth2.service_account import Credentials

logger = logging.getLogger("metallbot.parser")

# ── Настройки ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
IMAGES_DIR = Path(__file__).parent / "static" / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

MAX_TEXT_CHARS   = 40_000   # лимит текста для Claude
MAX_IMAGES       = 12       # максимум фото с сайта
MIN_IMAGE_SIZE   = 15_000   # байт — меньше скорее всего иконка
MAX_IMAGE_SIZE   = 8_000_000  # 8 МБ
ALLOWED_MIME     = {"image/jpeg", "image/png", "image/webp"}
REQUEST_TIMEOUT  = aiohttp.ClientTimeout(total=30, connect=10)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}


# ── Структуры данных ──────────────────────────────────────────────────────────
@dataclass
class ParsedImage:
    url: str
    local_path: str        # относительный путь для раздачи через FastAPI
    alt: str = ""
    width: int = 0
    height: int = 0
    size_bytes: int = 0


@dataclass
class ParseResult:
    url: str
    company_name: str
    ok: bool = False
    error: str = ""

    # Извлечённые данные
    description: str = ""
    services: list[str] = field(default_factory=list)
    equipment: list[str] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)
    certificates: list[str] = field(default_factory=list)
    contacts: dict = field(default_factory=dict)      # phone, email, address
    work_hours: str = ""
    founded_year: str = ""
    employees: str = ""
    area_sqm: str = ""
    extra_facts: list[str] = field(default_factory=list)

    images: list[ParsedImage] = field(default_factory=list)
    raw_text: str = ""
    parse_duration_sec: float = 0.0


# ── Утилиты ───────────────────────────────────────────────────────────────────
def _slugify(text: str, max_len: int = 60) -> str:
    """Превращает произвольный текст в безопасное имя папки."""
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:max_len] or "company"


def _extract_text(soup: BeautifulSoup) -> str:
    """Извлекает чистый текст страницы: убирает скрипты, стили, комментарии."""
    # Удаляем шумные теги
    for tag in soup(["script", "style", "noscript", "iframe",
                     "nav", "footer", "header", "aside", "form",
                     "button", "input", "select", "textarea"]):
        tag.decompose()
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    lines = []
    for el in soup.find_all(
        ["h1", "h2", "h3", "h4", "h5", "p", "li", "td", "th", "span", "div"]
    ):
        text = el.get_text(separator=" ", strip=True)
        # Пропускаем слишком короткие / пустые строки
        if len(text) < 15:
            continue
        # Убираем дубли пробелов
        text = re.sub(r"\s+", " ", text).strip()
        lines.append(text)

    # Дедупликация соседних одинаковых строк
    seen = set()
    deduped = []
    for line in lines:
        key = line[:80]
        if key not in seen:
            seen.add(key)
            deduped.append(line)

    return "\n".join(deduped)


def _collect_image_urls(soup: BeautifulSoup, base_url: str) -> list[tuple[str, str]]:
    """
    Собирает все кандидаты на фото: (url, alt).
    Приоритет: <img>, srcset, og:image, background-image в style.
    """
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(url: str, alt: str = "") -> None:
        if not url:
            return
        url = url.strip()
        if url.startswith("data:"):
            return
        abs_url = urljoin(base_url, url)
        # Чистим query-параметры типа ?resize=300x200 но сохраняем путь
        parsed = urlparse(abs_url)
        clean = parsed._replace(query="", fragment="").geturl()
        if clean not in seen:
            seen.add(clean)
            candidates.append((clean, alt))

    # og:image — обычно самый репрезентативный
    for meta in soup.find_all("meta", property=lambda p: p and "image" in p.lower()):
        _add(meta.get("content", ""), "og:image")

    # Все <img>
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        srcset = img.get("srcset", "")
        alt = img.get("alt", "")

        # Из srcset берём наибольшее разрешение
        if srcset:
            parts = [p.strip().split() for p in srcset.split(",") if p.strip()]
            # Сортируем по числу (ширина) по убыванию
            parts.sort(key=lambda p: float(p[1].rstrip("wx")) if len(p) > 1 else 0, reverse=True)
            if parts:
                _add(parts[0][0], alt)
        if src:
            _add(src, alt)

    # CSS background-image
    for el in soup.find_all(style=True):
        matches = re.findall(r'url\(["\']?([^"\'()]+)["\']?\)', el["style"])
        for m in matches:
            _add(m)

    return candidates


def _is_likely_photo(url: str) -> bool:
    """Эвристика: не иконка и не логотип."""
    low = url.lower()
    skip_keywords = [
        "logo", "icon", "favicon", "banner", "sprite", "pixel",
        "arrow", "button", "bg_", "_bg", "background", "placeholder",
        "thumb_small", "1x1", "blank", "loading",
    ]
    # Проверяем расширение
    path = urlparse(low).path
    ext = Path(path).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        return False
    return not any(kw in low for kw in skip_keywords)


# ── Основной класс ────────────────────────────────────────────────────────────
class SiteParser:

    @staticmethod
    async def parse(url: str, company_name: str = "", progress_cb=None) -> ParseResult:
        """
        Полный цикл парсинга.
        progress_cb(step: str) — вызывается при каждом этапе для UI.
        """
        t0 = time.monotonic()
        result = ParseResult(url=url, company_name=company_name or urlparse(url).netloc)

        async def _progress(msg: str):
            logger.info("[parser] %s", msg)
            if progress_cb:
                try:
                    await progress_cb(msg)
                except Exception:
                    pass

        # ── 1. Загрузка HTML ───────────────────────────────────────────────────
        await _progress("🌐 Загружаю страницу...")
        try:
            html, final_url = await SiteParser._fetch_html(url)
            result.url = final_url  # после редиректов
        except aiohttp.ClientConnectorError as e:
            result.error = f"Не могу подключиться к сайту: {e}"
            return result
        except aiohttp.ClientResponseError as e:
            result.error = f"Сайт вернул ошибку {e.status}: {e.message}"
            return result
        except asyncio.TimeoutError:
            result.error = "Сайт не отвечает (timeout 30s)"
            return result
        except aiohttp.TooManyRedirects:
            result.error = "Слишком много редиректов"
            return result
        except Exception as e:
            result.error = f"Ошибка загрузки: {type(e).__name__}: {e}"
            return result

        # ── 2. Парсинг HTML ────────────────────────────────────────────────────
        await _progress("🔍 Анализирую структуру страницы...")
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception as e:
            result.error = f"Ошибка разбора HTML: {e}"
            return result

        raw_text = _extract_text(soup)
        result.raw_text = raw_text[:MAX_TEXT_CHARS]

        if len(raw_text) < 100:
            result.error = "Страница почти пустая — возможно требует JavaScript (JS-рендеринг)"
            # Не прерываемся — пробуем хотя бы фото собрать

        # ── 3. Claude AI — извлечение структурированных данных ────────────────
        if raw_text and ANTHROPIC_API_KEY:
            await _progress("🤖 Claude AI анализирует контент...")
            try:
                ai_data = await SiteParser._extract_with_claude(
                    result.raw_text, result.company_name, result.url
                )
                result.description    = ai_data.get("description", "")
                result.services       = ai_data.get("services", [])
                result.equipment      = ai_data.get("equipment", [])
                result.materials      = ai_data.get("materials", [])
                result.certificates   = ai_data.get("certificates", [])
                result.contacts       = ai_data.get("contacts", {})
                result.work_hours     = ai_data.get("work_hours", "")
                result.founded_year   = ai_data.get("founded_year", "")
                result.employees      = ai_data.get("employees", "")
                result.area_sqm       = ai_data.get("area_sqm", "")
                result.extra_facts    = ai_data.get("extra_facts", [])
            except anthropic.APIConnectionError:
                logger.warning("[parser] Claude API недоступен — пропускаем AI-анализ")
            except anthropic.RateLimitError:
                logger.warning("[parser] Claude rate limit — пропускаем AI-анализ")
            except anthropic.APIStatusError as e:
                logger.warning("[parser] Claude API error %s — пропускаем", e.status_code)
            except Exception as e:
                logger.warning("[parser] Claude error: %s", e)
        elif not ANTHROPIC_API_KEY:
            logger.warning("[parser] ANTHROPIC_API_KEY не задан — AI-анализ отключён")

        # ── 4. Сбор и скачивание фото ─────────────────────────────────────────
        await _progress("🖼 Собираю изображения...")
        img_candidates = _collect_image_urls(soup, result.url)
        # Фильтруем явно не-фото
        img_candidates = [(u, a) for u, a in img_candidates if _is_likely_photo(u)]

        if img_candidates:
            await _progress(f"📥 Скачиваю фото ({min(len(img_candidates), MAX_IMAGES)} шт.)...")
            downloaded = await SiteParser._download_images(
                img_candidates, result.company_name
            )
            result.images = downloaded
            await _progress(f"✅ Сохранено {len(downloaded)} фото")
        else:
            await _progress("ℹ️ Фотографий на главной странице не найдено")

        result.ok = True
        result.parse_duration_sec = round(time.monotonic() - t0, 1)
        return result

    # ── HTTP-загрузка ──────────────────────────────────────────────────────────
    @staticmethod
    async def _fetch_html(url: str) -> tuple[str, str]:
        """Загружает HTML с поддержкой редиректов, gzip, SSL."""
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        # Используем Google DNS (8.8.8.8) — обходит блокировки VPN/корп. DNS
        try:
            resolver = aiohttp.AsyncResolver(nameservers=["8.8.8.8", "8.8.4.4", "1.1.1.1"])
            connector = aiohttp.TCPConnector(ssl=False, resolver=resolver)
        except Exception:
            # Если aiodns недоступен — стандартный коннектор
            connector = aiohttp.TCPConnector(ssl=False)

        async with aiohttp.ClientSession(
            connector=connector,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        ) as session:
            async with session.get(url, allow_redirects=True, max_redirects=10) as resp:
                resp.raise_for_status()
                # Определяем кодировку
                content_type = resp.headers.get("Content-Type", "")
                charset = None
                if "charset=" in content_type:
                    charset = content_type.split("charset=")[-1].split(";")[0].strip()

                raw = await resp.read()
                # Пробуем определить кодировку из мета-тега
                if not charset:
                    sniff = raw[:2048].decode("ascii", errors="ignore")
                    m = re.search(r'charset=["\']?([a-zA-Z0-9_-]+)', sniff, re.I)
                    charset = m.group(1) if m else "utf-8"

                try:
                    html = raw.decode(charset, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    html = raw.decode("utf-8", errors="replace")

                return html, str(resp.url)

    # ── Claude AI ─────────────────────────────────────────────────────────────
    @staticmethod
    async def _extract_with_claude(text: str, company: str, url: str) -> dict:
        """Отправляет текст в Claude и получает структурированный JSON."""
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

        prompt = f"""Ты помощник для анализа сайтов промышленных компаний.
Проанализируй текст сайта компании «{company}» ({url}) и извлеки информацию в JSON.

ТЕКСТ САЙТА:
{text[:MAX_TEXT_CHARS]}

Верни ТОЛЬКО валидный JSON без markdown-обёртки, в точно таком формате:
{{
  "description": "краткое описание компании 2-4 предложения",
  "services": ["услуга 1", "услуга 2"],
  "equipment": ["станок/оборудование 1", "станок 2"],
  "materials": ["материал 1", "материал 2"],
  "certificates": ["ISO 9001", "ГОСТ ..."],
  "contacts": {{
    "phone": "+7...",
    "email": "...",
    "address": "..."
  }},
  "work_hours": "Пн-Пт 9:00-18:00",
  "founded_year": "2005",
  "employees": "50-100 человек",
  "area_sqm": "2000 кв.м",
  "extra_facts": ["интересный факт 1", "факт 2"]
}}

Если какое-то поле отсутствует на сайте — оставь пустую строку или пустой массив.
Отвечай только JSON, без пояснений."""

        msg = await client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = msg.content[0].text.strip()
        # Вырезаем JSON если Claude всё же добавил обёртку
        m = re.search(r"\{[\s\S]+\}", raw)
        if not m:
            raise ValueError("Claude не вернул JSON")
        import json
        return json.loads(m.group(0))

    # ── Скачивание фото ───────────────────────────────────────────────────────
    @staticmethod
    async def _download_images(
        candidates: list[tuple[str, str]],
        company_name: str,
    ) -> list[ParsedImage]:
        """Скачивает до MAX_IMAGES фото, сохраняет на диск."""
        folder_name = _slugify(company_name)
        save_dir = IMAGES_DIR / folder_name
        save_dir.mkdir(parents=True, exist_ok=True)

        results: list[ParsedImage] = []
        try:
            resolver = aiohttp.AsyncResolver(nameservers=["8.8.8.8", "8.8.4.4", "1.1.1.1"])
            connector = aiohttp.TCPConnector(ssl=False, limit=5, resolver=resolver)
        except Exception:
            connector = aiohttp.TCPConnector(ssl=False, limit=5)

        async with aiohttp.ClientSession(
            connector=connector, headers=HEADERS, timeout=REQUEST_TIMEOUT
        ) as session:
            tasks = [
                SiteParser._download_one(session, url, alt, save_dir, folder_name)
                for url, alt in candidates[:MAX_IMAGES * 2]  # берём с запасом
            ]
            for coro in asyncio.as_completed(tasks):
                try:
                    img = await coro
                    if img:
                        results.append(img)
                        if len(results) >= MAX_IMAGES:
                            break
                except Exception as e:
                    logger.debug("[parser] Фото пропущено: %s", e)

        return results

    @staticmethod
    async def _download_one(
        session: aiohttp.ClientSession,
        url: str,
        alt: str,
        save_dir: Path,
        folder_name: str,
    ) -> Optional[ParsedImage]:
        """Скачивает одно изображение, проверяет размер и тип."""
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None

                # Проверяем Content-Type
                ct = resp.headers.get("Content-Type", "")
                mime = ct.split(";")[0].strip().lower()

                # Если MIME неизвестен — угадываем по URL
                if mime not in ALLOWED_MIME:
                    guessed, _ = mimetypes.guess_type(url)
                    if guessed in ALLOWED_MIME:
                        mime = guessed
                    else:
                        return None

                # Читаем с ограничением размера
                chunks = []
                total = 0
                async for chunk in resp.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > MAX_IMAGE_SIZE:
                        logger.debug("[parser] Фото слишком большое: %s", url)
                        return None
                    chunks.append(chunk)

                data = b"".join(chunks)
                if len(data) < MIN_IMAGE_SIZE:
                    return None  # иконка

                # Определяем расширение
                ext_map = {
                    "image/jpeg": ".jpg",
                    "image/png":  ".png",
                    "image/webp": ".webp",
                }
                ext = ext_map.get(mime, ".jpg")

                # Имя файла = хэш URL (избегаем дублей)
                fname = hashlib.md5(url.encode()).hexdigest()[:12] + ext
                fpath = save_dir / fname

                fpath.write_bytes(data)

                local_path = f"/static/images/{folder_name}/{fname}"
                return ParsedImage(
                    url=url,
                    local_path=local_path,
                    alt=alt,
                    size_bytes=len(data),
                )

        except asyncio.TimeoutError:
            logger.debug("[parser] Timeout для фото: %s", url)
            return None
        except Exception as e:
            logger.debug("[parser] Ошибка фото %s: %s", url, e)
            return None


# ── Сохранение в Google Sheets ────────────────────────────────────────────────
GOOGLE_SHEET_ID   = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON", "")
SCOPES = ["https://spreadsheets.google.com/feeds",
          "https://www.googleapis.com/auth/drive"]

# Колонки которые добавляем/обновляем при парсинге
PARSED_COLUMNS = [
    "Сайт", "Описание_парс", "Услуги_парс", "Оборудование_парс",
    "Материалы_парс", "Сертификаты_парс", "Телефон_парс", "Email_парс",
    "Адрес_парс", "Режим_работы", "Год_основания", "Сотрудников",
    "Площадь", "Факты_парс", "Фото_URLs", "Парсинг_дата",
]


def _get_sheet_client():
    if GOOGLE_CREDS_JSON and GOOGLE_CREDS_JSON.strip().startswith("{"):
        creds = Credentials.from_service_account_info(
            _json.loads(GOOGLE_CREDS_JSON), scopes=SCOPES
        )
    else:
        root = Path(__file__).parent.parent
        creds = Credentials.from_service_account_file(
            str(root / "google_creds.json"), scopes=SCOPES
        )
    return gspread.authorize(creds)


def _ensure_columns(ws: gspread.Worksheet) -> dict[str, int]:
    """Добавляет недостающие колонки в лист, возвращает {name: col_index}."""
    headers = ws.row_values(1)
    col_map: dict[str, int] = {h: i + 1 for i, h in enumerate(headers)}
    new_cols = [c for c in PARSED_COLUMNS if c not in col_map]
    if new_cols:
        start = len(headers) + 1
        for i, col_name in enumerate(new_cols):
            col_idx = start + i
            ws.update_cell(1, col_idx, col_name)
            col_map[col_name] = col_idx
    return col_map


def save_to_sheets(result: "ParseResult", sheet_name: str) -> bool:
    """
    Находит строку компании в листе Google Sheets и обновляет парсинговые колонки.
    Возвращает True если строка найдена и обновлена.
    """
    try:
        gc = _get_sheet_client()
        ss = gc.open_by_key(GOOGLE_SHEET_ID)
        ws = ss.worksheet(sheet_name)

        col_map = _ensure_columns(ws)

        # Ищем строку по названию компании (колонка "Название" или "Компания")
        all_vals = ws.get_all_values()
        if not all_vals:
            return False

        headers = all_vals[0]
        name_col_idx = None
        for candidate in ("Название", "Компания", "Поставщик", "Имя"):
            if candidate in headers:
                name_col_idx = headers.index(candidate)
                break

        if name_col_idx is None:
            logger.warning("[sheets] Не найдена колонка с названием компании")
            return False

        # Нечёткий поиск строки
        target_name = result.company_name.lower().strip()
        row_idx = None
        for i, row in enumerate(all_vals[1:], start=2):
            cell = row[name_col_idx].lower().strip() if name_col_idx < len(row) else ""
            if cell and (cell in target_name or target_name in cell):
                row_idx = i
                break

        if row_idx is None:
            logger.warning("[sheets] Компания '%s' не найдена в листе '%s'",
                           result.company_name, sheet_name)
            return False

        # Формируем данные для обновления
        import datetime
        photo_urls = " | ".join(img.url for img in result.images[:8])
        updates = {
            "Сайт":           result.url,
            "Описание_парс":  result.description,
            "Услуги_парс":    " | ".join(result.services),
            "Оборудование_парс": " | ".join(result.equipment),
            "Материалы_парс": " | ".join(result.materials),
            "Сертификаты_парс": " | ".join(result.certificates),
            "Телефон_парс":   result.contacts.get("phone", ""),
            "Email_парс":     result.contacts.get("email", ""),
            "Адрес_парс":     result.contacts.get("address", ""),
            "Режим_работы":   result.work_hours,
            "Год_основания":  result.founded_year,
            "Сотрудников":    result.employees,
            "Площадь":        result.area_sqm,
            "Факты_парс":     " | ".join(result.extra_facts),
            "Фото_URLs":      photo_urls,
            "Парсинг_дата":   datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
        }

        cells = []
        for col_name, value in updates.items():
            if col_name in col_map and value:
                cells.append(gspread.Cell(row_idx, col_map[col_name], value))

        if cells:
            ws.update_cells(cells, value_input_option="USER_ENTERED")
            logger.info("[sheets] Обновлено %d ячеек для '%s' в '%s'",
                        len(cells), result.company_name, sheet_name)
        return True

    except gspread.exceptions.WorksheetNotFound:
        logger.warning("[sheets] Лист '%s' не найден", sheet_name)
        return False
    except Exception as e:
        logger.error("[sheets] Ошибка сохранения: %s", e)
        return False


# ── Форматирование результата для Telegram ────────────────────────────────────
def format_result_for_telegram(r: ParseResult) -> str:
    """Возвращает красивый текст для отправки в Telegram."""
    if not r.ok:
        return f"❌ <b>Ошибка парсинга</b>\n{r.error}"

    lines = [f"✅ <b>{r.company_name}</b>", f"🌐 {r.url}", ""]

    if r.description:
        lines += [f"📝 <b>Описание:</b>\n{r.description}", ""]

    if r.services:
        lines.append("🔧 <b>Услуги:</b>")
        lines += [f"  • {s}" for s in r.services[:10]]
        lines.append("")

    if r.equipment:
        lines.append("⚙️ <b>Оборудование:</b>")
        lines += [f"  • {e}" for e in r.equipment[:8]]
        lines.append("")

    if r.materials:
        lines.append("🏗 <b>Материалы:</b>")
        lines += [f"  • {m}" for m in r.materials[:6]]
        lines.append("")

    if r.certificates:
        lines.append("📜 <b>Сертификаты:</b> " + ", ".join(r.certificates))
        lines.append("")

    contacts = r.contacts
    contact_parts = []
    if contacts.get("phone"):
        contact_parts.append(f"📞 {contacts['phone']}")
    if contacts.get("email"):
        contact_parts.append(f"📧 {contacts['email']}")
    if contacts.get("address"):
        contact_parts.append(f"📍 {contacts['address']}")
    if contact_parts:
        lines.append("📋 <b>Контакты:</b>")
        lines += contact_parts
        lines.append("")

    facts = []
    if r.founded_year:
        facts.append(f"📅 Основана: {r.founded_year}")
    if r.employees:
        facts.append(f"👥 Сотрудников: {r.employees}")
    if r.area_sqm:
        facts.append(f"🏭 Площадь: {r.area_sqm}")
    if r.work_hours:
        facts.append(f"🕐 Режим: {r.work_hours}")
    if facts:
        lines += facts
        lines.append("")

    if r.extra_facts:
        lines.append("💡 <b>Факты:</b>")
        lines += [f"  • {f}" for f in r.extra_facts[:5]]
        lines.append("")

    if r.images:
        lines.append(f"🖼 Фото: {len(r.images)} шт. сохранено")

    lines.append(f"\n⏱ Время парсинга: {r.parse_duration_sec}с")
    return "\n".join(lines)
