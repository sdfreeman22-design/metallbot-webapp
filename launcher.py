"""
METALLBOT — единый запуск.

Что делает:
1. Стартует cloudflared tunnel → получает публичный HTTPS-URL для Mini App
2. Записывает URL в webapp/.webapp_url (бот его прочитает)
3. Запускает FastAPI-сервер на 127.0.0.1:8765 в отдельном потоке
4. Запускает бот (импортирует bot.main)

Если cloudflared не установлен — выводит инструкцию и работает в локальном режиме
(Mini App доступен только по http://localhost:8765, без интеграции в Telegram).

Запуск: python webapp/launcher.py
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

WEBAPP_DIR = Path(__file__).parent
URL_FILE = WEBAPP_DIR / ".webapp_url"


def find_bot_dir() -> Path:
    """Ищем папку с bot.py — поддержка двух раскладок:
       1) webapp/ лежит ВНУТРИ metallbot/  (рядом с bot.py)
       2) webapp/ лежит РЯДОМ с metallbot/  (на уровень выше)"""
    candidates = [
        WEBAPP_DIR.parent,                     # ../
        WEBAPP_DIR.parent / "metallbot",       # ../metallbot/
        WEBAPP_DIR.parent.parent / "metallbot",
    ]
    for c in candidates:
        if (c / "bot.py").exists():
            return c
    raise RuntimeError(
        f"Не найден bot.py. Проверены: {[str(c) for c in candidates]}\n"
        f"Положи папку webapp/ рядом с bot.py."
    )


BOT_DIR = find_bot_dir()
ROOT = BOT_DIR
sys.path.insert(0, str(BOT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("launcher")


PORT = 8765


# ──────────────────────────────────────────────────────────────
# 1. FastAPI в отдельном потоке
# ──────────────────────────────────────────────────────────────
def _run_server():
    import uvicorn
    # Импортируем server как локальный модуль (он рядом с launcher)
    sys.path.insert(0, str(WEBAPP_DIR))
    from server import app  # type: ignore
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


def start_server():
    t = threading.Thread(target=_run_server, daemon=True, name="fastapi")
    t.start()
    # Дожидаемся, что порт открылся
    import socket
    for _ in range(40):  # 4 сек макс
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.1)
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                log.info(f"✅ FastAPI поднят на http://127.0.0.1:{PORT}")
                return
        time.sleep(0.1)
    log.warning("⚠️  FastAPI не отозвался за 4 сек — продолжаю всё равно")


# ──────────────────────────────────────────────────────────────
# 2. Cloudflared tunnel
# ──────────────────────────────────────────────────────────────
CLOUDFLARED_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def find_cloudflared() -> str | None:
    """Ищем cloudflared в PATH, рядом с launcher.py или в родительской папке."""
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    for parent in (WEBAPP_DIR, WEBAPP_DIR.parent, BOT_DIR):
        for name in ("cloudflared.exe", "cloudflared"):
            local = parent / name
            if local.exists():
                return str(local)
    return None


def start_cloudflared() -> str | None:
    """Запускаем туннель, парсим URL из stdout/stderr. Возвращаем URL или None."""
    exe = find_cloudflared()
    if not exe:
        log.warning("⚠️  cloudflared не найден")
        log.warning("    Скачай: https://github.com/cloudflare/cloudflared/releases")
        log.warning("    → положи cloudflared.exe в папку webapp/ или добавь в PATH")
        log.warning("    Без cloudflared Mini App работает только в браузере на этом компе.")
        return None

    log.info(f"🌐 Запускаю cloudflared: {exe}")
    proc = subprocess.Popen(
        [exe, "tunnel", "--url", f"http://127.0.0.1:{PORT}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )

    url: str | None = None
    start = time.time()

    def _drain():
        """Читаем stdout туннеля в фоне, чтобы не блокировался."""
        for line in proc.stdout:  # type: ignore[union-attr]
            if not line:
                continue
            line = line.rstrip()
            if "trycloudflare.com" in line and "INF" not in line[:30]:
                log.info(f"[cf] {line}")

    # Ищем URL в первые 30 сек
    while time.time() - start < 30:
        if proc.poll() is not None:
            log.error("cloudflared упал при старте")
            return None
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            time.sleep(0.2)
            continue
        m = CLOUDFLARED_URL_RE.search(line)
        if m:
            url = m.group(0)
            log.info(f"✅ Туннель готов: {url}")
            break

    if not url:
        log.warning("⚠️  Не удалось получить URL туннеля за 30 сек")
        return None

    # Дренируем остаток вывода в фоне, чтобы pipe не забивался
    t = threading.Thread(target=_drain, daemon=True, name="cf-drain")
    t.start()
    return url


# ──────────────────────────────────────────────────────────────
# 3. Сохраняем URL и запускаем бот
# ──────────────────────────────────────────────────────────────
def save_url(url: str | None):
    if url:
        URL_FILE.write_text(url, encoding="utf-8")
        os.environ["WEBAPP_URL"] = url
        log.info(f"📝 URL сохранён: {URL_FILE}")
    else:
        # пишем заглушку — http-локалка для теста с компа
        local = f"http://127.0.0.1:{PORT}"
        URL_FILE.write_text(local, encoding="utf-8")
        os.environ["WEBAPP_URL"] = local
        log.info(f"📝 Локальный URL: {local} (Telegram WebApp в боте работать НЕ будет)")


def start_bot():
    log.info("🤖 Стартую бота...")
    # Импортируем как модуль — config / state / handlers сами поднимутся
    from bot import main as bot_main
    asyncio.run(bot_main())


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("METALLBOT — единый запуск (бот + Mini App)")
    log.info("=" * 60)

    start_server()
    url = start_cloudflared()
    save_url(url)

    if url:
        log.info("=" * 60)
        log.info(f"🎯 Mini App: {url}")
        log.info(f"🤖 Команда в боте: /карточка")
        log.info("=" * 60)

    start_bot()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")
