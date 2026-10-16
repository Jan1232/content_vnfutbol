# content_vnfutbol

Две связанные системы на одном сервере (`/var/max-repost`):

1. **Создание контента** (`src/`) — доноры → факт → генерация → очередь в ЛС.
2. **Автопостинг MAX** — репост источников, свой контент (сериалы), опросы, предложка, SEO-матчи, дайджест.

Секреты только в `.env`, его в git нет. Юниты systemd лежат в `scripts/max-repost-*.service`.

## Как это устроено

```
Telegram-доноры
      ↓
src/ingest          фильтр мусора, extract факта, дедуп, медиа
      ↓
src/generate        Terra-генератор + guardrail
      ↓
очередь в SQLite    data/ingest.db
      ↓
live-бот в ЛС       ✅ / ❌ / ✏️ / смена категории / картинка
```

- `src/ingest` — Telethon читает источники, отсекает рекламу и мусор, достаёт факт, подбирает медиа (источник или Яндекс.Картинки).
- `src/generate` — пишет пост в тоне канала. Калибровочный бот (`moderator_bot`) отдельно: только оценка стиля, без очереди из доноров.
- `src/config.py` — модели и клиент API, отдельно от автопостинга.

Источники задаются в `src/ingest/sources.py`.

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-generate.txt
cp .env.example .env
```

В `.env` нужны как минимум `OPENAI_API_KEY`, `BOT_TOKEN_OBUCHENIE`, `OWNER_CHAT_ID` и Telethon-сессия.

Один процесс: сбор + бот очереди в ЛС:

```bash
python -m src.ingest.run
python -m src.ingest.run --warm 20
```

Разовый прогон истории за сутки:

```bash
python -m src.ingest.run_24h
python -m src.ingest.run_24h --hours 24 --collect-only
python -m src.ingest.run_24h --bot-only
```

Генерация одного поста из факта (без ingest):

```bash
python -m src.generate.pipeline --fact "..." --archetype transfer --veracity verified
```

Калибровка стиля (не публикация):

```bash
python -m src.generate.moderator_bot
```

## Переменные окружения

| Переменная | Зачем |
|---|---|
| `OPENAI_API_KEY` | генерация, extract, эмбеддинги |
| `OPENAI_BASE_URL` | совместимый endpoint |
| `OPENAI_HTTP_PROXY` / `SCRAPER_HTTP_PROXY` | прокси для API и Telethon |
| `BOT_TOKEN_OBUCHENIE` | бот очереди / калибровки |
| `OWNER_CHAT_ID` | кому слать очередь |
| `TG_API_ID` / `TG_API_HASH` | user-session Telethon |
| `INGEST_DB` | путь к SQLite, по умолчанию `data/ingest.db` |

Секреты только в `.env`, его в git нет.

## Автопостинг MAX

| Путь | Зачем |
|---|---|
| `app/` | админка, MAX/VK/TG API, фильтры, БД репоста |
| `workers/` | репост, контент tvoe.live, SEO, дайджест, опросы |
| `parsers/` | ленты Telegram / VK / RSS |
| `content/` | свой контент (сериалы → MAX) |
| `opros/` | анонимные опросы и квизы |
| `predlozka/` | бот предложки, ERID, охрана комментариев |
| `seo/` | матчи Wordstat → посты |
| `rules/` | YAML-правила каналов |
| `scripts/` | systemd + обёртки запуска |

Зависимости автопостинга: `pip install -r requirements.txt`. Генератор: `requirements-generate.txt`.

## Данные

`data/` и логи в репозиторий не входят. Там сессии Telethon, медиа и SQLite-очереди.
