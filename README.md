# MAX Repost

Бот наполняет каналы/группы MAX контентом из источников (Telegram / VK / RSS).
Админка на IP хоста (порт 8790). Защита: логин/пароль.

## Доступ к админке

```bash
ssh -L 8790:127.0.0.1:8790 root@ВАШ_СЕРВЕР
```

Открыть в браузере: http://127.0.0.1:8790

Логин и пароль — в `/var/max-repost/.env` (`ADMIN_LOGIN`, `ADMIN_PASSWORD`).

## Сервисы

```bash
systemctl status max-repost-admin max-repost-worker
systemctl restart max-repost-admin max-repost-worker
journalctl -u max-repost-worker -f
```

## Логика

1. Воркер синхронизирует каналы бота (`GET /chats` + updates `bot_added`).
2. В админке: канал → **Источники** → добавить URL.
3. При добавлении источника история **не** публикуется — ставится watermark.
4. Новые посты фильтруются (рекламные слова + любые ссылки в тексте) и уходят в привязанный MAX-канал.

## VK

Для стабильного парсинга VK добавьте сервисный ключ приложения в `.env`:

```
VK_ACCESS_TOKEN=...
```

Документация MAX API: https://dev.max.ru/docs-api  
Выбор сервисов: https://dev.max.ru/docs/maxbusiness/selectionservices

## Автоперевод (Groq)

1. Ключ: https://console.groq.com/keys
2. В `/var/max-repost/.env`: `GROQ_API_KEY=gsk_...`
3. `systemctl restart max-repost-admin max-repost-worker`
4. В админке при добавлении источника включите «Автоперевод на русский».
