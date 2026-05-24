# METALLBOT Mini App — установка

Этот пакет добавляет к боту **Mini App** — карточки контрагентов прямо внутри Telegram.

---

## 📁 Что куда копировать

Структура папки бота **до** установки:
```
metallbot/
├── bot.py
├── config.py
├── state.py
├── handlers/
│   ├── start.py
│   ├── commands_data.py
│   └── ...
├── db/
└── ...
```

**После** установки должно быть так:
```
metallbot/
├── bot.py                          ← добавить 1 строку (см. ниже)
├── handlers/
│   ├── webapp.py                   ← НОВЫЙ файл (из handler_webapp.py)
│   └── ...
├── webapp/                         ← НОВАЯ папка
│   ├── launcher.py
│   ├── server.py
│   ├── contact.html
│   ├── requirements.txt
│   ├── start_metallbot.bat        ← теперь запускаешь это
│   ├── cloudflared.exe            ← скачать (см. ниже)
│   └── README.md
└── ...
```

### Шаги:

1. **Скопируй папку `webapp/`** из этого проекта в корень `metallbot/`
   (рядом с `bot.py`).
2. **Переименуй** `webapp/handler_webapp.py` → `handlers/webapp.py` (т.е. перенеси
   в существующую папку `handlers/`).
3. **Открой `bot.py`** и в блок импортов хендлеров добавь одну строку:
   ```python
   import handlers.webapp  # noqa
   ```
   (рядом с `import handlers.start`)
4. **Скачай cloudflared.exe**:
   <https://github.com/cloudflare/cloudflared/releases/latest>
   → положи `cloudflared.exe` в папку `webapp/` (или добавь в PATH).
5. **Запускай бот через** `webapp\start_metallbot.bat` (вместо старого .bat).

Готово — в боте появятся команды `/карточка` и `/webapp`.

---

## ⚙️ Как это работает

`start_metallbot.bat` → `launcher.py`:
1. Ставит `fastapi` + `uvicorn` (если ещё не стоят) — один раз.
2. Поднимает `cloudflared tunnel` → получает HTTPS-URL вида `https://xxx.trycloudflare.com`.
3. Запускает FastAPI-сервер на `127.0.0.1:8765` — отдаёт `contact.html`
   и API `/api/contacts`, `/api/contact/<id>`.
4. Записывает URL в `webapp/.webapp_url` (хендлер бота его читает).
5. Стартует сам бот (твой `bot.py` без изменений в логике).

При выключении (Ctrl+C) останавливается всё разом.

---

## 🛠 Что если cloudflared не установлен

Бот всё равно запустится. Mini App будет доступен только по
`http://127.0.0.1:8765` — открыть можно из браузера **на этом компе**.
Кнопка в Telegram не сработает, потому что Telegram WebApp требует HTTPS.

---

## 🔄 Откат

Если что-то пошло не так — просто запусти старый `.bat` (или `python bot.py`).
Установка ничего не ломает: всё новое лежит в `webapp/` и в одном файле
`handlers/webapp.py`. Удали папку и строку импорта — будет как было.
