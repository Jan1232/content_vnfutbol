# OpenClaw

Gateway общий (`127.0.0.1:18789`). Агенты разделены, чтобы проекты не делили лимиты и runtime.

| Кто | Агент API | Бэкенд | Runtime |
|---|---|---|---|
| yt-bot, calorie-bot (`/var/calorie-bot`) | `openclaw/default` | `openai/gpt-5.6-sol` (из `~/.openclaw`) | Codex |
| max-repost перевод / SEO | `openclaw/default` + `OPENCLAW_BACKEND_MODEL=openai/gpt-5.6-sol` | Codex |
| editorial | **не ходит в OpenClaw** | Platform API `EDITORIAL_TEXT_MODEL` / `EDITORIAL_SEARCH_MODEL` через `OPENAI_API_KEY` |

`openclaw/default` не менять: на нём сидят yt-bot, calorie-bot, перевод и SEO.

## Статус
- Gateway: `systemctl --user status openclaw-gateway`
- HTTP API: `http://127.0.0.1:18789/v1`
- Прокси egress: `http://127.0.0.1:10809` (xray)
- Агенты: `openclaw agents list`

## Вход в ChatGPT
Нужен SSH с TTY:

```bash
ssh -t root@SERVER 'bash /var/max-repost/scripts/openclaw-login-chatgpt.sh'
```

Проверка: `bash /var/max-repost/scripts/openclaw-status.sh`

## Переменные max-repost `.env`
- `OPENCLAW_MODEL` / `OPENCLAW_BACKEND_MODEL` — перевод и SEO (default/Codex)
- Editorial: `EDITORIAL_LLM_TRANSPORT=openai`, `EDITORIAL_TEXT_MODEL`, `EDITORIAL_SEARCH_MODEL` — Platform API, не OpenClaw
- `GROQ_*` — не в горячем пути editorial (`EDITORIAL_ALLOW_GROQ_FALLBACK=false`)
- `*_HTTP_PROXY` / xray `10809`

## Рестарт
Gateway трогать только если менялся `~/.openclaw/openclaw.json` и изменения не применились сами.

```bash
systemctl restart max-repost-editorial
# перевод/SEO/yt-bot/calorie-bot рестартовать не нужно
```
