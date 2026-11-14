---
name: llm-usage
description: >-
  Статус квот LLM-провайдеров (Codex/Kimi/Qwen/DeepSeek и др.) на любой управляемой
  машине или в контейнере: читай центральный снапшот ~/.local/state/llm-usage/snapshot.json.
  Триггеры: «сколько квоты осталось», «статус квот», «LLM usage», «лимиты провайдеров»,
  «можно ли запускать тяжёлые задачи». Не ходи в API провайдеров за квотами сам.
metadata:
  version: "1.0.0"
---

# LLM Usage snapshot (универсальный контракт)

Центральный дашборд `herdr-quota` (Herdr App на Windows) каждые ~5 минут публикует
нормализованный снапшот квот во все управляемые машины и контейнеры. Агенты внутри
машин читают его локально — без ключей, без обращений к провайдерам.

## Канонический путь чтения

```
~/.local/state/llm-usage/snapshot.json
```

Один и тот же путь для SSH-машин (dev → /home/hermes, 1c → /home/vscode) и для
локальных docker-контейнеров (home пользователя контейнера). Если файла нет или
он старше `max_age_seconds` — статус **unknown**, не выдумывать цифры и не считать
это аварией провайдеров.

## Формат (schema_version 1)

```json
{
  "schema_version": 1,
  "generated_at": 1794649525.9,
  "source": "central-herdr-quota",
  "max_age_seconds": 900,
  "providers": [
    {"name": "KIMI", "status": "ok", "plan": "...", "updated_at": 1794649500.0,
     "details": ["..."],
     "windows": [
       {"label": "monthly", "used_percent": 63, "remaining_percent": 37,
        "resets_at": 1796428800, "reset_text": "", "duration_minutes": null}
     ]}
  ]
}
```

- `status`: `ok` | `depleted` | `stale` | `error` | `unsupported` | `unconfigured` | `loading` | `empty`.
- `windows.label`: `weekly` | `monthly` | `5 hour` | `5 day`; `used_percent` 0–100.
- `resets_at` — unix seconds (может быть null); `duration_minutes` может быть null
  (месячные окна без явной длительности — центр считает pace по billing-месяцу от reset).
- В снапшоте **нет** ключей/токенов/сырых ошибок коллекторов — только статусы и окна.

## Как читать (агенту)

```bash
python3 - <<'EOF'
import json, time
from pathlib import Path
p = Path.home() / '.local/state/llm-usage/snapshot.json'
try:
    d = json.loads(p.read_text())
    age = time.time() - d['generated_at']
    if age > d.get('max_age_seconds', 900):
        print('LLM usage: stale/unknown (age %.0fs)' % age)
    else:
        for pr in d['providers']:
            wins = ', '.join('%s=%d%%' % (w['label'], w['used_percent']) for w in pr['windows'])
            print('%-12s %-10s %s' % (pr['name'], pr['status'], wins))
except FileNotFoundError:
    print('LLM usage: unknown (no snapshot on this machine)')
EOF
```

Правила:
- `depleted` или `used_percent >= 100` по авторитетному окну → не запускать тяжёлые
  задачи через этого провайдера, предложить альтернативу.
- Месячные лимиты (KIMI, QWEN): ориентируйся на `monthly`, не на `weekly`.
- `stale`/`error`/нет файла → unknown; сообщить пользователю, что центральный
  экспорт не доехал, а не что провайдер упал.

## Доставка (для оператора/центрального агента)

- Конфиг таргетов: `<appHome>/plugins/config/herdr.quota/export-targets.json`
  (Windows: `C:/Users/steel/.config/herdr-app/plugins/config/herdr.quota/`).
- Формы таргета:
  - `{"machine": "dev"}` → `hc exec -i --on dev -- python3 -c <writer>` (SSH-машина,
    home SSH-пользователя);
  - `{"container": "c", "user": "u"}` → локальный `docker exec -i -u u c python3`;
  - `{"machine": "m", "container": "c", "user": "u"}` → контейнер на машине.
- Статус доставки: `export-status.json` рядом с таргетами; локальная копия снапшота:
  `usage.json` там же.
- Публикация происходит в цикле дашборда (`usage_export.publish`), ручной запуск:
  `python -c "import usage_export, dashboard; usage_export.publish(dashboard.Store().snapshot())"`
  из `plugins/herdr-quota/bin` (даст текущие статусы после прогона коллекторов).

## Установка навыка на машину/агенту

Скопировать каталог `skills/llm-usage` в skills-каталог агента (например
`~/.dsh/skills/`, `~/.claude/skills/`, профиль Hermes). Навык самодостаточен:
нужен только python3 для чтения.
