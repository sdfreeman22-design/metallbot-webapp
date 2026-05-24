"""
Обработчики команд бота для Mini App.

Этот файл нужно положить в metallbot/handlers/webapp.py
и добавить одну строку импорта в bot.py:

    import handlers.webapp  # noqa

Команды:
    /карточка                    — открыть Mini App (список + поиск)
    /карточка <название>         — сразу открыть карточку конкретного контрагента
    /webapp                      — синоним
"""
import logging
import os
import urllib.parse
from pathlib import Path

from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from state import dp

logger = logging.getLogger("metallbot.webapp")


# Файл с публичным URL туннеля — пишет launcher.py
def _find_url_file() -> Path | None:
    """webapp/ может лежать рядом с metallbot/ ИЛИ внутри него — пробуем оба пути."""
    handlers_dir = Path(__file__).parent
    metallbot_dir = handlers_dir.parent
    candidates = [
        metallbot_dir / "webapp" / ".webapp_url",          # webapp ВНУТРИ metallbot/
        metallbot_dir.parent / "webapp" / ".webapp_url",   # webapp РЯДОМ с metallbot/
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def get_webapp_url() -> str | None:
    """Берём URL из env (launcher выставляет) или из файла."""
    url = os.environ.get("WEBAPP_URL", "").strip()
    if url:
        return url
    f = _find_url_file()
    if f:
        url = f.read_text(encoding="utf-8").strip()
        if url:
            return url
    return None


def is_https(url: str) -> bool:
    return url.lower().startswith("https://")


async def cmd_webapp(msg: Message):
    """Открыть Mini App (карточки контрагентов)."""
    url = get_webapp_url()
    if not url:
        await msg.answer(
            "⚠️ Mini App ещё не готов.\n\n"
            "Запусти бот через `start_metallbot.bat` — "
            "тогда cloudflared поднимет туннель и Mini App станет доступен."
        )
        return

    # Если команда с аргументом — открываем сразу карточку
    parts = msg.text.split(maxsplit=1)
    target_url = url
    if len(parts) > 1:
        contact = parts[1].strip()
        target_url = f"{url}/?contact={urllib.parse.quote(contact)}"

    if is_https(target_url):
        # Полноценная WebApp-кнопка — открывается прямо внутри Telegram
        kb = ReplyKeyboardMarkup(
            keyboard=[[
                KeyboardButton(
                    text="🗂 Открыть карточки",
                    web_app=WebAppInfo(url=target_url),
                )
            ]],
            resize_keyboard=True,
            one_time_keyboard=False,
        )
        await msg.answer(
            "🗂 Карточки контрагентов\n\n"
            "Нажми кнопку ниже — откроется Mini App с поиском, "
            "историей закупок и быстрыми действиями.",
            reply_markup=kb,
        )
    else:
        # Локальный http — даём обычную ссылку
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🌐 Открыть в браузере", url=target_url)
        ]])
        await msg.answer(
            "🗂 Mini App работает в локальном режиме (нет HTTPS-туннеля).\n\n"
            "Открыть можно только в браузере на этом компьютере:\n"
            f"`{target_url}`\n\n"
            "Чтобы работало с телефона — установи cloudflared "
            "(см. webapp/README.md)",
            reply_markup=kb,
            parse_mode="Markdown",
        )


async def cmd_hide_kb(msg: Message):
    """Спрятать клавиатуру Mini App."""
    from aiogram.types import ReplyKeyboardRemove
    await msg.answer("Клавиатура скрыта.", reply_markup=ReplyKeyboardRemove())


# ──────────────────────────────────────────────────────────────
# Регистрация
# ──────────────────────────────────────────────────────────────
dp.message.register(cmd_webapp, Command(commands=["карточка", "webapp", "miniapp"]))
dp.message.register(cmd_hide_kb, Command(commands=["скрыть_кнопку"]))
