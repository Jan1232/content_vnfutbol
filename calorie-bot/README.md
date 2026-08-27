# Calorie Bot — учёт КБЖУ

Telegram-бот для группы: считает калории и БЖУ за день, читает этикетки с фото, обновляет расход по весу.

## Возможности

- Принимает сообщения **только** из заданной группы
- Текст с КБЖУ (как у «Чикен Премьер»)
- Фото этикетки → OCR/vision → запись в дневник (с уточнением порции)
- Вес: `Вес 139.3 кг` → пересчёт BMR/TDEE (Mifflin–St Jeor)
- Итоги дня автоматически в **00:00** (`Asia/Yekaterinburg`)

## Профиль

- Имя: Ян
- Дата рождения: 19.08.2002
- Рост: 174 см
- Активность: ×1.2 (сидячий) — меняется в `.env` (`ACTIVITY_FACTOR`)

## Запуск

```bash
cd /var/max-repost/calorie-bot
.venv/bin/python -m bot.main
```

Или через systemd:

```bash
sudo systemctl enable --now calorie-bot
sudo systemctl status calorie-bot
```

## Команды в группе

| Команда | Действие |
|---------|----------|
| `/day` | Итог за сегодня |
| `/undo` | Удалить последнюю запись |
| `/cancel` | Отменить ожидание порции после фото |
| `/help` | Справка |

## Важно

Сейчас у бота Privacy Mode **включён** (`can_read_all_group_messages: false`).  
В [@BotFather](https://t.me/BotFather):

1. `/mybots` → `@heallhy_yan_bot`
2. **Bot Settings** → **Group Privacy** → **Turn off**

Иначе бот в группе не увидит сообщения без `@упоминания`.
