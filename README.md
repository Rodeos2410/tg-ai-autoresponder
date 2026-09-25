# Telegram AI Autoresponder

Актуальный Telegram AI-автоответчик. Код находится в `bot.py`.

## Возможности

- управление через Telegram-кнопки;
- смена API, модели и системного промпта;
- зашифрованное хранение API-ключа;
- SQLite для настроек и лимитов диалогов;
- проверка онлайн-статуса владельца через Telethon;
- OpenAI-compatible API.

## Запуск

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 bot.py
```

Заполни `.env` перед запуском. Секреты, сессии и SQLite-база намеренно не входят в репозиторий.
