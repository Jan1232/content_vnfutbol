# Мем-источники (editorial)

Добавление нового мем-канала — **только YAML** в `editorial/channels/*.yaml`, без правок Python.

## Обязательные поля

```yaml
feeds:
  - name: my_meme_channel          # уникальное имя (для per-source лимита)
    kind: telegram                 # сейчас поддерживается только telegram
    handle: somechannel            # без @
    take_only: [video, meme_image] # что брать из постов
    rewrite_text: true             # переписать текст (meme_text)
    profanity_gate: strict         # мат → чистка / held
    max_per_day: 5                 # дневной лимит этого фида (0 = глобальный)
    wrap_template: false           # false = готовое медиа без PNG-шаблона
```

Для `kind: telegram` дефолты уже `rewrite_text=true` и `profanity_gate=strict`, если поля не указаны.

## Как работает отбор

- Пост берётся **по факту медиа** (видео или картинка), не по текст-классификатору.
- `event_type` принудительно `meme`.
- Глобальный потолок: `meme_source_max_per_day` в settings (дефолт 5); у фида `max_per_day` имеет приоритет.

## Точки расширения (не telegram)

В `editorial/sources.py` реестр:

```python
MEME_SOURCE_PARSERS = {
    "telegram": parse_telegram_meme_feed,
    # позже: "vk": ..., "instagram_export": ..., "rss_meme": ...
}
```

Новый `kind` = новая функция-парсер + запись в реестре. YAML-формат полей тот же, где применимо (`url`/`endpoint` вместо `handle`).

`kind: yt_bot` — отдельный пайплайн тем, не мем-источник.
