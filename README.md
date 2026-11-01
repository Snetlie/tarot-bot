# Ответы на ваши вопросы таро — Telegram-бот с мини-аппом

Python без внешних зависимостей. Бот (long polling) и мини-апп работают в одном процессе.

## Переменные окружения
| Имя | Зачем |
|---|---|
| `BOT_TOKEN` | токен бота от @BotFather |
| `LLM_PROVIDER` | `openai` (любой OpenAI-совместимый API) или `anthropic` |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL` | для Groq: `https://api.groq.com/openai/v1`, `openai/gpt-oss-120b` |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | если `LLM_PROVIDER=anthropic` |
| `WEBAPP_URL` | публичный HTTPS-адрес сервиса (на Railway/Render подставляется сам) |
| `DATA_DIR` | куда класть `data.db` (на хостинге — путь тома, например `/data`) |
| `CORS_ORIGIN` | только если страница на GitHub Pages: адрес Pages без пути, например `https://имя.github.io` |
| `ADMIN_ID` | необязательно: Telegram ID админа. Без него админом станет первый, кто отправит секретную команду |

## Локальный запуск
`start.bat` — поднимает туннель Cloudflare, прописывает адрес в `.env` и запускает бота.

## Постоянная работа (Railway)
1. Railway → New Project → Deploy from GitHub repo → выберите этот репозиторий (соберётся по `Dockerfile`).
2. Variables: добавьте переменные из таблицы выше.
3. Settings → Networking → Generate Domain.
4. Volumes → добавьте том с путём `/data` (иначе статистика пропадёт при обновлении).
5. Перед этим остановите локального бота: один токен нельзя слушать из двух мест.

## Страница на GitHub Pages
Интерфейс лежит в `docs/`. Сервер с ботом всё равно нужен (токен и ключ нейронки нельзя держать в странице).
1. Repo → Settings → Pages → Deploy from a branch → `main` / `/docs`.
2. В `docs/config.js` впишите адрес сервера: `window.API_BASE = "https://ваш-сервер";`
3. На сервере задайте `CORS_ORIGIN=https://имя.github.io` и `WEBAPP_URL=https://имя.github.io/имя-репозитория`.
4. Админ-панель: `<адрес страницы>/admin.html`, открывается командой бота.
