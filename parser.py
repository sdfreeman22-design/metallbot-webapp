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

# Проверка SSL-сертификатов при парсинге сайтов. По умолчанию выключена —
# у части мелких сайтов кооператоров просроченные/самоподписанные сертификаты,
# и включение проверки уронит их парсинг. Чтобы включить (безопаснее против
# MITM) — задать переменную окружения PARSER_VERIFY_SSL=1.
_PARSER_SSL = os.getenv("PARSER_VERIFY_SSL", "").strip().lower() in ("1", "true", "yes")

def _normalize_phone(p: str) -> str:
    """Нормализует телефон до +7 (XXX) XXX-XX-XX, извлекая только цифры."""
    if not p:
        return p
    digits = re.sub(r'\D', '', p)
    if len(digits) == 11 and digits[0] in ('7', '8'):
        digits = '7' + digits[1:]
    elif len(digits) == 10:
        digits = '7' + digits
    else:
        return re.sub(r'\s+', ' ', p).strip()
    return f'+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}'


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
    _extracted_company_name: str = ""  # имя компании извлечённое с сайта

    company_type: str = "coop"   # "coop" | "supplier" | "both"
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


def _extract_contacts_direct(soup: BeautifulSoup, raw_html: str = "") -> dict:
    """
    Извлекает контакты напрямую из HTML — до удаления footer/header.
    Многослойный поиск: tel:/mailto:, data-*, JSON-LD, itemprop, regex.
    Возвращает {'phone': '...', 'email': '...', 'address': '...'}.
    """
    import json as _json_local

    result = {"phone": "", "email": "", "address": ""}

    def _valid_phone(p: str) -> bool:
        """Минимум 10 цифр в строке."""
        return len(re.sub(r'\D', '', p)) >= 10

    # ── 1. tel:/mailto: ссылки ────────────────────────────────────────────────
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("tel:") and not result["phone"]:
            phone = re.sub(r"[^\d+\-\(\)\s]", "", href[4:]).strip()
            if phone and _valid_phone(phone):
                result["phone"] = phone
        elif href.startswith("mailto:") and not result["email"]:
            email = href[7:].split("?")[0].strip()
            if email and "@" in email:
                result["email"] = email

    # ── 2. JSON-LD / Schema.org (SEO-разметка — очень надёжный источник) ─────
    if not result["phone"] or not result["email"]:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                raw_json = script.string or ""
                if not raw_json.strip():
                    continue
                data = _json_local.loads(raw_json)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if not result["phone"]:
                        tel = item.get("telephone") or item.get("phone") or ""
                        if tel and _valid_phone(str(tel)):
                            result["phone"] = str(tel).strip()
                    if not result["email"]:
                        em = item.get("email") or ""
                        if em and "@" in str(em):
                            result["email"] = str(em).strip()
                    if not result["address"]:
                        addr = item.get("address", {})
                        if isinstance(addr, dict):
                            parts = [
                                addr.get("streetAddress", ""),
                                addr.get("addressLocality", ""),
                                addr.get("addressRegion", ""),
                            ]
                            addr_str = ", ".join(p for p in parts if p)
                            if addr_str:
                                result["address"] = addr_str
                        elif isinstance(addr, str) and len(addr) > 5:
                            result["address"] = addr
            except Exception:
                pass

    # ── 3. itemprop микроразметка ─────────────────────────────────────────────
    if not result["phone"]:
        for el in soup.find_all(attrs={"itemprop": "telephone"}):
            val = (el.get("content") or el.get_text(strip=True) or "").strip()
            if val and _valid_phone(val):
                result["phone"] = val
                break

    if not result["email"]:
        for el in soup.find_all(attrs={"itemprop": "email"}):
            val = (el.get("content") or el.get_text(strip=True) or "").strip()
            if val and "@" in val:
                result["email"] = val
                break

    if not result["address"]:
        for el in soup.find_all(attrs={"itemprop": "address"}):
            val = (el.get("content") or el.get_text(" ", strip=True) or "").strip()
            if val and len(val) > 5:
                result["address"] = re.sub(r"\s+", " ", val)[:200]
                break

    # ── 4. data-* атрибуты (антиспам: номер прячут в data-phone) ─────────────
    if not result["phone"]:
        for el in soup.find_all(True):
            for attr_name, attr_val in el.attrs.items():
                if isinstance(attr_val, str) and ("phone" in attr_name.lower() or "tel" in attr_name.lower()):
                    if _valid_phone(attr_val):
                        result["phone"] = attr_val.strip()
                        break
            if result["phone"]:
                break

    # ── 5. Regex по тексту страницы + raw HTML ───────────────────────────────
    if not result["phone"]:
        # Ищем в нескольких источниках последовательно
        search_sources = []
        # Сначала — видимый текст страницы
        search_sources.append(soup.get_text(" ", strip=True))
        # Затем — raw HTML (ловим телефоны в JS, data-атрибутах, комментариях)
        if raw_html:
            # Убираем теги но оставляем содержимое атрибутов и скриптов
            stripped = re.sub(r'<[^>]+>', ' ', raw_html)
            search_sources.append(stripped)

        phone_patterns = [
            # Стандартный российский с разделителями
            r'(?:\+7|8)[\s\-]?\(?\d{3,4}\)?[\s\-\.\u00a0]?\d{2,3}[\s\-\.\u00a0]?\d{2}[\s\-\.\u00a0]?\d{2}',
            # Сплошные 11 цифр начиная с 7 или 8
            r'(?:\+7|8)\d{10}',
            # 10 цифр без кода страны
            r'9\d{9}',
            # Со скобками без кода: (499) 123-45-67
            r'\(\d{3,4}\)[\s\-]?\d{2,3}[\s\-\.]?\d{2}[\s\-\.]?\d{2}',
        ]
        for source in search_sources:
            if result["phone"]:
                break
            for pat in phone_patterns:
                matches = re.findall(pat, source)
                for candidate in matches:
                    candidate = candidate.strip()
                    if _valid_phone(candidate):
                        result["phone"] = candidate
                        break
                if result["phone"]:
                    break

    # ── 5b. Нормализуем найденный телефон ─────────────────────────────────────
    if result["phone"]:
        digits = re.sub(r'\D', '', result["phone"])
        if len(digits) == 11 and digits[0] in ('7', '8'):
            digits = '7' + digits[1:]
        elif len(digits) == 10:
            digits = '7' + digits
        if len(digits) == 11:
            # Определяем длину кода города по первым цифрам
            # 4-значные коды: 8422(Ульяновск), 8332(Киров), 4722(Губкин) и т.д.
            _FOUR_DIGIT_CODES = {
                '8422','8332','4722','4832','4812','8482','4852','4872',
                '4932','3412','4742','4732','8442','8452','8462','8472',
            }
            _city = digits[1:5]
            if _city[:4] in _FOUR_DIGIT_CODES or (_city[:4].startswith('8') and _city[:3] not in ('800','812')):
                result["phone"] = f'+{digits[0]} ({digits[1:5]}) {digits[5:7]}-{digits[7:9]}-{digits[9:11]}'
            else:
                result["phone"] = f'+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}'


    # ── 6а. Joomla email антиспам-защита ─────────────────────────────────────
    # Joomla прячет email в JS-массив с ASCII-кодами: l[4]=' 107' и т.д.
    # Символы хранятся в обратном порядке.
    if not result["email"] and raw_html:
        try:
            import re as _re_j
            # Находим все блоки обфускации
            joomla_entries = _re_j.findall(r"l\[(\d+)\]=' (\d+)'", raw_html)
            if joomla_entries:
                num_map = {int(k): chr(int(v)) for k, v in joomla_entries
                           if 32 <= int(v) <= 126}
                if num_map:
                    max_idx = max(num_map.keys())
                    chars = [num_map.get(i, '') for i in range(max_idx + 1)]
                    decoded = ''.join(reversed(chars))
                    # \b гарантирует границу слова — TLD не «склеивается» с следующим словом
                    em_candidates = _re_j.findall(
                        r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,6}(?!\w)', decoded
                    )
                    skip_domains = ('sentry.io', 'example.com', 'test.com', 'wixpress.com')
                    for em in em_candidates:
                        if not any(d in em.lower() for d in skip_domains):
                            result["email"] = em.lower()
                            logger.info("[parser] Joomla email decoded: %s", em)
                            break
        except Exception:
            pass

    # ── 6б. Человеко-читаемая анти-спам обфускация email ────────────────────
    # Форматы: "info at domain dot ru", "info[at]domain[dot]ru",
    #          "info(at)domain(dot)ru", "info собака domain точка ru"
    _SKIP_EM = ('sentry.io', 'example.com', 'test.com', 'yourdomain',
                'email.com', 'domain.com', 'wixpress.com', 'googleapis')
    if not result["email"]:
        _obf_sources = [soup.get_text(' ', strip=True)]
        if raw_html:
            _obf_sources.append(re.sub(r'<[^>]+>', ' ', raw_html))
        _OBFUSC = [
            # info at domain dot ru  /  info [at] domain [dot] ru  /  (at) (dot)
            r'([a-zA-Z0-9_.+-]{2,30})\s*(?:\[at\]|\(at\)|(?<!\w)at(?!\w))\s*'
            r'([a-zA-Z0-9-]{2,50})\s*(?:\[dot\]|\(dot\)|(?<!\w)dot(?!\w))\s*'
            r'([a-zA-Z]{2,6})',
            # info собака / эт domain точка ru
            r'([a-zA-Z0-9_.+-]{2,30})\s*(?:собака|эт|ат)\s*'
            r'([a-zA-Z0-9-]{2,50})\s*(?:точка|dot)\s*'
            r'([a-zA-Z]{2,6})',
        ]
        for _src in _obf_sources:
            if result["email"]:
                break
            for _pat in _OBFUSC:
                _m = re.search(_pat, _src, re.IGNORECASE)
                if _m:
                    _cand = f"{_m.group(1)}@{_m.group(2)}.{_m.group(3)}".lower().strip()
                    if not any(d in _cand for d in _SKIP_EM) and len(_cand) < 80:
                        result["email"] = _cand
                        logger.info("[parser] Anti-spam email decoded: %s", _cand)
                        break

    # ── 6. Email: видимый текст + raw HTML ──────────────────────────────────
    if not result["email"]:
        email_pat = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,6}'

        def _good_email(e: str) -> bool:
            return '@' in e and not any(d in e.lower() for d in _SKIP_EM)

        sources = [soup.get_text(' ', strip=True)]
        if raw_html:
            sources.append(re.sub(r'<[^>]+>', ' ', raw_html))

        for source in sources:
            if result["email"]:
                break
            for m in re.finditer(email_pat, source):
                candidate = m.group(0).strip().lower()
                if _good_email(candidate):
                    result["email"] = candidate
                    break

    # ── 7. Адрес: footer/address теги ─────────────────────────────────────────
    if not result["address"]:
        for tag in soup.find_all(["address", "footer"]):
            text = tag.get_text(" ", strip=True)
            if text and len(text) > 10:
                result["address"] = re.sub(r"\s+", " ", text).strip()[:200]
                break

    logger.info("[parser] Контакты: phone=%r email=%r", result["phone"], result["email"])
    return result


def _extract_text(soup: BeautifulSoup) -> str:
    """Извлекает чистый текст страницы: убирает скрипты, стили, комментарии."""
    # Удаляем шумные теги (footer/header НЕ удаляем — там контакты!)
    for tag in soup(["script", "style", "noscript", "iframe",
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


def _extract_services_equipment_from_html(soup: BeautifulSoup) -> dict:
    """
    Резервный HTML-парсер услуг и оборудования.
    Ищет заголовки (h1-h4) с ключевыми словами и собирает <li>/<p> под ними.
    Возвращает {'services': [...], 'equipment': [...], 'materials': [...]}.
    """
    SERVICE_KW  = {'услуг', 'работ', 'сервис', 'направлени', 'технолог',
                   'возможност', 'специализац', 'выполняем', 'производим',
                   'изготовляем', 'обрабатываем', 'виды'}
    EQUIP_KW    = {'оборудован', 'станк', 'парк', 'техник', 'мощност', 'установк'}
    MATERIAL_KW = {'материал', 'металл', 'сплав', 'сырьё', 'сырье'}

    services, equipment, materials = [], [], []

    def _items_after_heading(heading_el) -> list[str]:
        """Собирает текстовые элементы <li> или <p> идущие после заголовка."""
        items = []
        # Ищем ближайший ul/ol/div-контейнер с <li>
        parent = heading_el.parent
        if parent:
            for el in parent.find_all(['li', 'p'], limit=30):
                txt = el.get_text(' ', strip=True)
                if 15 <= len(txt) <= 200:
                    clean = re.sub(r'\s+', ' ', txt).strip()
                    if clean not in items:
                        items.append(clean)
        # Если в родителе ничего — ищем следующий sibling-контейнер
        if not items:
            nxt = heading_el.find_next_sibling()
            while nxt and len(items) < 20:
                if nxt.name in ('ul', 'ol', 'div'):
                    for li in nxt.find_all('li', limit=20):
                        txt = li.get_text(' ', strip=True)
                        if 5 <= len(txt) <= 200:
                            clean = re.sub(r'\s+', ' ', txt).strip()
                            if clean not in items:
                                items.append(clean)
                    break
                nxt = nxt.find_next_sibling()
        return items[:20]

    for h in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5']):
        txt = h.get_text(' ', strip=True).lower()
        if not txt:
            continue
        if any(kw in txt for kw in SERVICE_KW) and not any(kw in txt for kw in EQUIP_KW):
            if len(services) < 20:
                services.extend(_items_after_heading(h))
        elif any(kw in txt for kw in EQUIP_KW):
            if len(equipment) < 20:
                equipment.extend(_items_after_heading(h))
        elif any(kw in txt for kw in MATERIAL_KW):
            if len(materials) < 15:
                materials.extend(_items_after_heading(h))

    # Дедупликация
    def _dedup(lst):
        seen, out = set(), []
        for x in lst:
            k = x[:60].lower()
            if k not in seen:
                seen.add(k); out.append(x)
        return out

    return {
        'services':  _dedup(services)[:20],
        'equipment': _dedup(equipment)[:20],
        'materials': _dedup(materials)[:15],
    }


def _get_section_context(el, depth: int = 4) -> str:
    """
    Возвращает текстовый контекст вокруг элемента — заголовок раздела.
    Поднимается по дереву до depth уровней, ищет h1-h4 или class/id с ключами.
    """
    node = el
    for _ in range(depth):
        node = getattr(node, "parent", None)
        if node is None:
            break
        # Ищем заголовок внутри этого блока
        for h in node.find_all(["h1", "h2", "h3", "h4"], limit=1):
            txt = h.get_text(" ", strip=True)
            if txt:
                return txt[:60]
        # Смотрим class/id блока
        cls = " ".join(node.get("class", []))
        nid = node.get("id", "")
        hint = (cls + " " + nid).lower()
        for kw in ("partner", "client", "клиент", "партнёр", "партнер",
                   "сертиф", "certif", "award", "наград",
                   "логотип", "logo", "галерея", "gallery", "отзыв", "review"):
            if kw in hint:
                return f"[{kw}]"
    return ""


# Ключевые слова разделов — изображения из этих разделов НЕ берём
_BAD_SECTION_KW = {
    "партнёр", "партнер", "partner", "клиент", "client", "покупател",
    "заказчик", "сертиф", "certif", "награда", "award", "лицензи",
    "отзыв", "review", "команда", "team", "сотрудник", "персонал",
    "вакансии", "career", "контакты", "contact",
}


def _collect_image_urls(soup: BeautifulSoup, base_url: str) -> list[tuple[str, str, str]]:
    """
    Собирает кандидаты на фото: (url, alt, section_context).
    Приоритет: og:image, <img>, data-original (Tilda/lazy), srcset, CSS background.
    """
    candidates: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def _add(url: str, alt: str = "", ctx: str = "") -> None:
        if not url:
            return
        url = url.strip()
        if url.startswith("data:"):
            return
        abs_url = urljoin(base_url, url)
        parsed = urlparse(abs_url)
        clean = parsed._replace(query="", fragment="").geturl()
        if clean not in seen:
            seen.add(clean)
            candidates.append((clean, alt, ctx))

    # og:image — обычно самый репрезентативный
    for meta in soup.find_all("meta", property=lambda p: p and "image" in p.lower()):
        _add(meta.get("content", ""), "og:image", "og:image")

    # Все <img>
    for img in soup.find_all("img"):
        src = (img.get("data-original") or img.get("src")
               or img.get("data-src") or img.get("data-lazy-src") or "")
        srcset = img.get("srcset", "")
        alt = img.get("alt", "")
        ctx = _get_section_context(img)

        if srcset:
            parts = [p.strip().split() for p in srcset.split(",") if p.strip()]
            parts.sort(key=lambda p: float(p[1].rstrip("wx")) if len(p) > 1 else 0, reverse=True)
            if parts:
                _add(parts[0][0], alt, ctx)
        if src:
            _add(src, alt, ctx)

    # data-original / data-bg на ЛЮБЫХ тегах (Tilda, WIX и др.)
    for el in soup.find_all(True):
        if el.name == "img":
            continue
        for attr in ("data-original", "data-bg", "data-image", "data-src"):
            val = el.get(attr, "")
            if val and not val.startswith("data:"):
                ctx = _get_section_context(el)
                _add(val, el.get("alt", "") or el.get("title", ""), ctx)

    # CSS background-image
    for el in soup.find_all(style=True):
        matches = re.findall(r'url\(["\']?([^"\'()]+)["\']?\)', el["style"])
        for m in matches:
            ctx = _get_section_context(el)
            _add(m, "", ctx)

    return candidates


# URL-паттерны, однозначно указывающие на «плохое» изображение
_BAD_URL_KW = [
    "logo", "icon", "favicon", "banner", "sprite", "pixel", "arrow",
    "button", "bg_", "_bg", "background", "placeholder", "thumb_small",
    "1x1", "blank", "loading",
    # Разделы сайта с партнёрами/клиентами
    "partner", "client", "клиент", "партнер", "сертиф", "award",
    "/team/", "/staff/", "/people/",
    # Tilda thumbs
    "/resize/20x/", "/resize/40x/", "/resize/80x/",
]


# CDN-хосты которые могут отдавать изображения без явного расширения в URL
_CDN_IMAGE_HOSTS = (
    "static.tildacdn.com", "img.tildacdn.com",
    "cdn.wixstatic.com", "images.squarespace-cdn.com",
    "media.bitrix24.ru",
)


def _is_likely_photo(url: str, ctx: str = "") -> bool:
    """Эвристика: не иконка, не логотип, не изображение из раздела партнёров."""
    low = url.lower()
    # Плохие URL-паттерны (проверяем до расширения — быстрее)
    if any(kw in low for kw in _BAD_URL_KW):
        return False
    # Плохой раздел страницы
    ctx_low = ctx.lower()
    if any(kw in ctx_low for kw in _BAD_SECTION_KW):
        return False
    # Расширение
    path = urlparse(low).path
    ext = Path(path).suffix.lower()
    # Для известных CDN-хостов разрешаем URL без расширения — MIME определим при скачивании
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        if not any(cdn in low for cdn in _CDN_IMAGE_HOSTS):
            return False
    return True


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

        # Сначала извлекаем контакты напрямую (tel:/mailto: ссылки, regex)
        # — до удаления тегов, пока footer/header ещё на месте
        direct_contacts = _extract_contacts_direct(soup, raw_html=html)
        logger.info("[parser] Прямые контакты: %s", direct_contacts)

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
                    result.raw_text, result.company_name, result.url,
                    hint_contacts=direct_contacts,
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
                # Название компании с сайта
                extracted = ai_data.get("company_name", "").strip()
                if extracted and len(extracted) > 3:
                    result._extracted_company_name = extracted
                # Тип компании
                ctype = ai_data.get("company_type", "coop").strip().lower()
                if ctype in ("coop", "supplier", "both"):
                    result.company_type = ctype
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


        # Если email не найден — пробуем страницу контактов
        if not result.contacts.get("email") and not direct_contacts.get("email"):
            await _progress("📇 Ищу страницу контактов...")
            try:
                extra = await SiteParser._fetch_contacts_page(result.url, soup)
                if extra.get("email"):
                    result.contacts["email"] = extra["email"]
                    logger.info("[parser] Email со страницы контактов: %s", extra["email"])
                if not result.contacts.get("phone") and extra.get("phone"):
                    direct_contacts["phone"] = extra["phone"]
            except Exception as _e:
                logger.debug("[parser] contacts page skip: %s", _e)

        # ── HTML-fallback для услуг / оборудования / материалов ─────────────────
        # Если Claude вернул пустые списки — пробуем извлечь из структуры HTML
        if not result.services or not result.equipment:
            html_data = _extract_services_equipment_from_html(soup)
            if not result.services and html_data['services']:
                result.services = html_data['services']
                logger.info("[parser] Services from HTML fallback: %d items", len(result.services))
            if not result.equipment and html_data['equipment']:
                result.equipment = html_data['equipment']
                logger.info("[parser] Equipment from HTML fallback: %d items", len(result.equipment))
            if not result.materials and html_data['materials']:
                result.materials = html_data['materials']
                logger.info("[parser] Materials from HTML fallback: %d items", len(result.materials))

        # Fallback: берём direct_contacts если Claude не нашёл или нашёл < 10 цифр
        import re as _re2
        claude_phone  = result.contacts.get("phone", "")
        claude_digits = len(_re2.sub(r"\D", "", claude_phone))
        direct_phone  = direct_contacts.get("phone", "")
        if direct_phone and claude_digits < 10:
            result.contacts["phone"] = direct_phone
            logger.info("[parser] Телефон из direct: %s", direct_phone)
        elif claude_phone and claude_digits >= 10:
            d = _re2.sub(r"\D", "", claude_phone)
            if len(d) == 11 and d[0] in ("7","8"): d = "7" + d[1:]
            elif len(d) == 10: d = "7" + d
            if len(d) == 11:
                result.contacts["phone"] = f"+{d[0]} ({d[1:4]}) {d[4:7]}-{d[7:9]}-{d[9:11]}"
        if not result.contacts.get("email") and direct_contacts.get("email"):
            result.contacts["email"] = direct_contacts["email"]
            logger.info("[parser] Email из direct: %s", direct_contacts["email"])
        if not result.contacts.get("address") and direct_contacts.get("address"):
            result.contacts["address"] = direct_contacts["address"]

        # ── 4. Сбор и скачивание фото ─────────────────────────────────────────
        await _progress("🖼 Собираю изображения...")
        # _collect_image_urls возвращает (url, alt, section_context)
        img_candidates_full = _collect_image_urls(soup, result.url)
        # Базовая фильтрация — убираем иконки, логотипы, изображения из плохих разделов
        img_candidates_full = [
            (u, a, ctx) for u, a, ctx in img_candidates_full
            if _is_likely_photo(u, ctx)
        ]
        # Для дальнейшей работы используем тройки (url, alt, ctx)
        img_candidates = img_candidates_full

        if img_candidates and ANTHROPIC_API_KEY:
            # Claude отбирает самые полезные фото (цех, оборудование, продукция)
            await _progress(f"🤖 Отбираю лучшие фото из {len(img_candidates)}...")
            try:
                img_candidates = await SiteParser._select_images_with_claude(img_candidates)
            except Exception as e:
                logger.warning("[parser] Claude image select error: %s", e)
                img_candidates = img_candidates[:MAX_IMAGES]

        if img_candidates:
            await _progress(f"📥 Скачиваю фото ({min(len(img_candidates), MAX_IMAGES)} шт.)...")
            # _download_images ожидает (url, alt) — берём первые два элемента
            downloaded = await SiteParser._download_images(
                [(u, a) for u, a, *_ in img_candidates],
                result.company_name,
                source_url=result.url,
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
            connector = aiohttp.TCPConnector(ssl=_PARSER_SSL, resolver=resolver)
        except Exception:
            # Если aiodns недоступен — стандартный коннектор
            connector = aiohttp.TCPConnector(ssl=_PARSER_SSL)

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

    # ── Поиск страницы контактов ──────────────────────────────────────────────
    @staticmethod
    async def _fetch_contacts_page(base_url: str, soup: BeautifulSoup) -> dict:
        """
        Если на главной не нашли email/phone — ищем страницу контактов и парсим её.
        Возвращает {'phone': '...', 'email': '...'}.
        """
        CONTACT_KEYWORDS = [
            "контакт", "contact", "kontakt", "kontakty",
            "связ", "svyaz", "о нас", "about", "реквизит",
        ]
        CONTACT_PATHS = [
            "/kontakty", "/contacts", "/contact", "/kontakt",
            "/about", "/o-nas", "/o-kompanii", "/svyaz",
            "/company/contacts", "/about/contacts",
        ]

        from urllib.parse import urlparse, urljoin

        base = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
        candidate_urls = []

        # 1. Ищем ссылки на странице с ключевыми словами
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            txt  = a.get_text(strip=True).lower()
            href_low = href.lower()
            if any(k in txt or k in href_low for k in CONTACT_KEYWORDS):
                full = urljoin(base_url, href)
                # Только ссылки на тот же домен
                if urlparse(full).netloc == urlparse(base_url).netloc and full not in candidate_urls:
                    candidate_urls.append(full)

        # 2. Добавляем стандартные пути
        for p in CONTACT_PATHS:
            u = base + p
            if u not in candidate_urls:
                candidate_urls.append(u)

        result = {"phone": "", "email": ""}
        EMAIL_PAT  = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,6}(?!\w)")
        # 3 или 4 цифры в коде города (8422 — Ульяновск и другие региональные)
        PHONE_PAT  = re.compile(r"(?:\+7|8)[\s\-]?\(?\d{3,4}\)?[\s\-\.]?\d{2,3}[\s\-\.]?\d{2}[\s\-\.]?\d{2}")
        SKIP_DOMAINS = ("sentry.io", "example.com", "test.com", "wixpress.com", "googleapis")

        try:
            connector = aiohttp.TCPConnector(ssl=_PARSER_SSL)
            async with aiohttp.ClientSession(
                connector=connector, headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                for url in candidate_urls[:5]:  # максимум 5 попыток
                    if result["email"]:
                        break
                    try:
                        async with session.get(url, allow_redirects=True) as resp:
                            if resp.status != 200:
                                continue
                            html2 = await resp.text(errors="replace")
                            # Ищем email обычным regex
                            for m in EMAIL_PAT.finditer(html2):
                                e = m.group(0).lower()
                                if not any(d in e for d in SKIP_DOMAINS):
                                    result["email"] = e
                                    logger.info("[parser] Email с контакт-страницы %s: %s", url, e)
                                    break
                            # Joomla email антиспам (ASCII-коды в JS-массиве)
                            if not result["email"]:
                                try:
                                    joomla_entries = re.findall(r"l\[(\d+)\]=' (\d+)'", html2)
                                    if joomla_entries:
                                        num_map = {int(k): chr(int(v)) for k, v in joomla_entries
                                                   if 32 <= int(v) <= 126}
                                        if num_map:
                                            max_idx = max(num_map.keys())
                                            chars = [num_map.get(i, '') for i in range(max_idx + 1)]
                                            decoded = ''.join(reversed(chars))
                                            for em in re.findall(
                                                r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,6}(?!\w)', decoded
                                            ):
                                                if not any(d in em.lower() for d in SKIP_DOMAINS):
                                                    result["email"] = em.lower()
                                                    logger.info("[parser] Joomla email (контакт-стр) %s: %s", url, em)
                                                    break
                                except Exception:
                                    pass
                            # Ищем телефон если ещё нет
                            if not result["phone"]:
                                pm = PHONE_PAT.search(html2)
                                if pm:
                                    result["phone"] = pm.group(0)
                    except Exception:
                        continue
        except Exception as e:
            logger.debug("[parser] contacts page error: %s", e)

        return result

    # ── Claude AI ─────────────────────────────────────────────────────────────
    @staticmethod
    async def _extract_with_claude(text: str, company: str, url: str,
                                    hint_contacts: dict | None = None) -> dict:
        """Отправляет текст в Claude и получает структурированный JSON."""
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

        # Подсказка для Claude если нашли контакты прямым парсингом
        hint_block = ""
        if hint_contacts and any(hint_contacts.values()):
            parts = []
            if hint_contacts.get("phone"):
                parts.append(f'Телефон: {hint_contacts["phone"]}')
            if hint_contacts.get("email"):
                parts.append(f'Email: {hint_contacts["email"]}')
            if hint_contacts.get("address"):
                parts.append(f'Адрес: {hint_contacts["address"][:100]}')
            if parts:
                hint_block = "\n\nКОНТАКТЫ НАЙДЕННЫЕ АВТОМАТИЧЕСКИ (используй их):\n" + "\n".join(parts)

        prompt = f"""Ты помощник для анализа сайтов промышленных компаний.
Проанализируй текст сайта ({url}) и извлеки информацию в JSON.{hint_block}

ТЕКСТ САЙТА:
{text[:MAX_TEXT_CHARS]}

ВАЖНЫЕ ПРАВИЛА ИЗВЛЕЧЕНИЯ:
— "services": перечисли ВСЕ виды работ/услуг/технологий которые компания выполняет.
  Ищи в разделах «Услуги», «Виды работ», «Технологии», «Возможности», «Направления», «Что мы делаем»,
  в маркированных списках (li), в заголовках разделов, в навигации, в тексте «Мы выполняем...».
  Каждая услуга — отдельная строка. Минимум 3-10 пунктов если сайт металлообрабатывающий.
— "equipment": конкретные станки и оборудование (марки, модели, типы).
  Ищи в разделах «Оборудование», «Парк оборудования», «Технический парк», «Производство».
— "materials": с какими материалами/металлами/сплавами работают.
— "contacts": email — ищи также в форматах "info at domain dot ru", "info[at]domain.ru", "info собака domain точка ru".

Верни ТОЛЬКО валидный JSON без markdown-обёртки:
{{
  "company_name": "Полное официальное название (ООО/АО/ИП + название)",
  "company_type": "coop|supplier|both — coop если выполняют работы на заказ, supplier если продают материалы/оборудование",
  "description": "краткое описание 2-4 предложения",
  "services": ["токарная обработка", "фрезерная обработка", "..."],
  "equipment": ["токарный ЧПУ Mazak", "фрезерный обрабатывающий центр", "..."],
  "materials": ["сталь", "нержавеющая сталь", "алюминий", "..."],
  "certificates": ["ISO 9001", "ГОСТ ..."],
  "contacts": {{"phone": "+7...", "email": "...", "address": "..."}},
  "work_hours": "Пн-Пт 9:00-18:00",
  "founded_year": "2005",
  "employees": "50-100 человек",
  "area_sqm": "2000 кв.м",
  "extra_facts": ["факт 1", "факт 2"]
}}

Если поле отсутствует — пустая строка или []. Только JSON, без пояснений."""

        msg = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=60,
        )

        raw = msg.content[0].text.strip()
        # Вырезаем JSON если Claude всё же добавил обёртку
        m = re.search(r"\{[\s\S]+\}", raw)
        if not m:
            raise ValueError("Claude не вернул JSON")
        import json
        return json.loads(m.group(0))

    # ── Claude: отбор полезных фото ──────────────────────────────────────────
    @staticmethod
    async def _select_images_with_claude(
        candidates: list[tuple]   # (url, alt) или (url, alt, ctx)
    ) -> list[tuple]:
        """
        Просит Claude Haiku выбрать из кандидатов наиболее полезные фото
        для карточки промышленной компании: цех, оборудование, продукция.
        Возвращает отфильтрованный список (не более MAX_IMAGES).
        """
        if not candidates:
            return candidates

        # Берём до 30 кандидатов для анализа
        pool = candidates[:30]

        lines = []
        for i, item in enumerate(pool):
            url = item[0]
            alt = item[1] if len(item) > 1 else ""
            ctx = item[2] if len(item) > 2 else ""
            fname = Path(urlparse(url).path).name[:40]
            alt_str = alt.strip()[:40] if alt.strip() else "—"
            ctx_str = ctx.strip()[:50] if ctx.strip() else "—"
            lines.append(f"{i}: {fname} | alt={alt_str} | раздел={ctx_str}")

        import json as _json
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""Отбираешь фото для карточки промышленного предприятия в B2B-системе.

ВЫБИРАЙ (5-8 штук) — изображения, которые скорее всего показывают:
✅ производственный цех, станки, оборудование
✅ готовую продукцию, детали, металлоизделия
✅ территорию завода, склад с металлом

СТРОГО ИСКЛЮЧАЙ — даже если имя файла выглядит как фото:
❌ логотипы ЛЮБЫХ компаний (своих, партнёров, клиентов, поставщиков)
❌ раздел "Наши партнёры" / "Клиенты" / "Работаем с..." — там чужие логотипы
❌ сертификаты, дипломы, награды
❌ иконки, баннеры с текстом, фоновые паттерны
❌ фото людей без производственного контекста

ПОДСКАЗКА: колонка "раздел" показывает заголовок блока на сайте, где найдено изображение.
Если раздел содержит слова "партнёр", "клиент", "сертификат" — почти наверняка плохой выбор.

СПИСОК (индекс: имя файла | alt | раздел):
{chr(10).join(lines)}

Верни ТОЛЬКО JSON: {{"selected": [0, 2, 5], "reason": "краткое пояснение"}}
Если хороших нет — верни {{"selected": [], "reason": "..."}}"""

        msg = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=20,
        )
        raw = msg.content[0].text.strip()
        m = re.search(r"\{[\s\S]+\}", raw)
        if not m:
            return pool[:MAX_IMAGES]

        data = _json.loads(m.group(0))
        indices = data.get("selected", [])
        reason  = data.get("reason", "")
        if reason:
            logger.info("[parser] Claude фото: %s", reason)

        if not indices:
            logger.info("[parser] Claude не нашёл подходящих фото — используем первые %d", MAX_IMAGES)
            return pool[:MAX_IMAGES]

        selected = [pool[i] for i in indices if 0 <= i < len(pool)]
        logger.info("[parser] Claude выбрал %d фото из %d кандидатов", len(selected), len(pool))
        return selected[:MAX_IMAGES]

    # ── Скачивание фото ───────────────────────────────────────────────────────
    @staticmethod
    async def _download_images(
        candidates: list[tuple[str, str]],
        company_name: str,
        source_url: str = "",
    ) -> list[ParsedImage]:
        """Скачивает до MAX_IMAGES фото, сохраняет на диск."""
        folder_name = _slugify(company_name)
        save_dir = IMAGES_DIR / folder_name
        save_dir.mkdir(parents=True, exist_ok=True)

        # Определяем Referer: корень сайта-источника
        referer = ""
        if source_url:
            p = urlparse(source_url)
            referer = f"{p.scheme}://{p.netloc}/"

        results: list[ParsedImage] = []
        try:
            resolver = aiohttp.AsyncResolver(nameservers=["8.8.8.8", "8.8.4.4", "1.1.1.1"])
            connector = aiohttp.TCPConnector(ssl=_PARSER_SSL, limit=5, resolver=resolver)
        except Exception:
            connector = aiohttp.TCPConnector(ssl=_PARSER_SSL, limit=5)

        async with aiohttp.ClientSession(
            connector=connector, headers=HEADERS, timeout=REQUEST_TIMEOUT
        ) as session:
            tasks = [
                SiteParser._download_one(session, url, alt, save_dir, folder_name, referer)
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
        referer: str = "",
    ) -> Optional[ParsedImage]:
        """Скачивает одно изображение, проверяет размер и тип.

        Для CDN-ресурсов (Tilda, WIX и др.) добавляет Referer заголовок.
        При таймауте делает одну повторную попытку с увеличенным timeout.
        """
        # CDN-домены, которые требуют Referer и иногда медленно отвечают
        _CDN_HOSTS = ("static.tildacdn.com", "tildacdn.com", "cdn.wixstatic.com",
                      "img.tildacdn.com")
        is_cdn = any(h in url for h in _CDN_HOSTS)
        per_image_timeout = 30 if is_cdn else 15

        # Заголовки запроса: добавляем Referer если он передан
        extra_headers: dict = {}
        if referer:
            extra_headers["Referer"] = referer
            extra_headers["Origin"] = referer.rstrip("/")

        async def _fetch(timeout_sec: int) -> Optional[ParsedImage]:
            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=timeout_sec),
                    headers=extra_headers or None,
                ) as resp:
                    if resp.status != 200:
                        logger.debug("[parser] HTTP %s для фото: %s", resp.status, url)
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
                return None
            except Exception as e:
                logger.debug("[parser] Ошибка фото %s: %s", url, e)
                return None

        # Первая попытка
        result = await _fetch(per_image_timeout)
        if result is not None:
            return result

        # Повторная попытка для CDN — с удвоенным таймаутом и без лишних заголовков
        if is_cdn:
            logger.debug("[parser] CDN retry: %s", url)
            result = await _fetch(per_image_timeout * 2)

        return result


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


PARSED_SHEET = "Парсинг"   # отдельный лист для всех парсинговых данных


def save_to_sheets(result: "ParseResult", sheet_name: str) -> bool:
    """
    Сохраняет данные парсинга в отдельный лист 'Парсинг'.
    Если лист не существует — создаёт его.
    Возвращает True при успехе.
    """
    try:
        gc = _get_sheet_client()
        ss = gc.open_by_key(GOOGLE_SHEET_ID)

        # Работаем с отдельным листом "Парсинг" — не трогаем основные листы
        try:
            ws = ss.worksheet(PARSED_SHEET)
        except gspread.exceptions.WorksheetNotFound:
            ws = ss.add_worksheet(title=PARSED_SHEET, rows=500, cols=len(PARSED_COLUMNS) + 2)
            ws.update([["Компания", "Лист"] + PARSED_COLUMNS])
            logger.info("[sheets] Создан лист '%s'", PARSED_SHEET)

        # В листе "Парсинг" первые колонки: Компания | Лист | <данные...>
        all_vals = ws.get_all_values()
        headers  = all_vals[0] if all_vals else []
        col_map  = {h: i + 1 for i, h in enumerate(headers)}

        # Добавляем недостающие колонки
        needed = ["Компания", "Лист"] + PARSED_COLUMNS
        for col_name in needed:
            if col_name not in col_map:
                new_idx = len(col_map) + 1
                ws.update_cell(1, new_idx, col_name)
                col_map[col_name] = new_idx

        # Ищем строку по имени компании (точное совпадение или добавляем новую)
        row_idx = None
        company_col = col_map.get("Компания", 1)
        for i, row in enumerate(all_vals[1:], start=2):
            cell = row[company_col - 1].strip() if len(row) >= company_col else ""
            if cell.lower() == result.company_name.lower():
                row_idx = i
                break

        if row_idx is None:
            # Добавляем новую строку
            row_idx = len(all_vals) + 1
            ws.update_cell(row_idx, company_col, result.company_name)
            ws.update_cell(row_idx, col_map.get("Лист", 2), sheet_name)
            logger.info("[sheets] Добавлена новая строка %d для '%s'", row_idx, result.company_name)

        # Записываем данные
        import datetime
        photo_urls = " | ".join(img.url for img in result.images[:8])
        updates = {
            "Компания":          result.company_name,
            "Лист":              sheet_name,
            "Сайт":              result.url,
            "Описание_парс":     result.description,
            "Услуги_парс":       " | ".join(result.services),
            "Оборудование_парс": " | ".join(result.equipment),
            "Материалы_парс":    " | ".join(result.materials),
            "Сертификаты_парс":  " | ".join(result.certificates),
            "Телефон_парс":      _normalize_phone(result.contacts.get("phone", "")),
            "Email_парс":        result.contacts.get("email", ""),
            "Адрес_парс":        result.contacts.get("address", ""),
            "Режим_работы":      result.work_hours,
            "Год_основания":     result.founded_year,
            "Сотрудников":       result.employees,
            "Площадь":           result.area_sqm,
            "Факты_парс":        " | ".join(result.extra_facts),
            "Фото_URLs":         photo_urls,
            "Парсинг_дата":      datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
        }

        cells = [
            gspread.Cell(row_idx, col_map[col], val)
            for col, val in updates.items()
            if col in col_map
        ]
        ws.update_cells(cells, value_input_option="RAW")
        logger.info("[sheets] Записано %d ячеек для '%s' в лист '%s'",
                    len(cells), result.company_name, PARSED_SHEET)
        return True

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
