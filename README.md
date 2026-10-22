# local-sync

Hermes-плагин: синхронизация профиля между машинами в локальной сети. Устройства
находят друг друга UDP-биконом, знакомятся по 6-значному коду, дальше возят файлы
по HTTP с HMAC-подписью и трёхточечным merge. Без облака, без git, без сторонних
зависимостей — только stdlib Python 3.9+.

```
local-sync/
├── plugin.yaml          # манифест: 6 tools, config_schema
├── __init__.py          # register(ctx): tools + вложенный скилл
├── schemas.py           # схемы инструментов для модели
├── tools.py             # обработчики: JSON-строка, автозапуск демона, прокси в /ctl
├── local_sync/          # движок
│   ├── core.py          # discovery, pairing, manifest, план, apply, HTTP, CLI
│   └── __main__.py      # python -m local_sync run
├── bin/local-sync       # CLI: run, peers, pair, accept, plan, sync, mcp
├── skills/local-sync/   # вложенный скилл: когда и как пользоваться
├── deploy/              # systemd --user unit + launchd plist
└── tests/               # 12 тестов на loopback
```

## Что синхронизируется

| Слой | Путь | Да |
|---|---|---|
| Свои скиллы | `skills/**` (bundled отсекаются по `.bundled_manifest`) | ✅ |
| Память | `memories/*.md` — union-merge по секциям `§` | ✅ |
| Душа/правила | `SOUL.md` | ✅ |
| Хуки | `hooks/**` | ✅ |
| Desktop-плагины | `desktop-plugins/**` | ✅ |
| Cron | `cron/**` (без `executions.db`) | ✅ |
| `config.yaml` | — | ❌ машиноспецифичен |
| `.env`, `auth.json` | — | ❌ секреты |
| `state.db`, сессии | — | ❌ пока нет |
| `cache/`, `logs/`, `backups/` | — | ❌ |

## Установка плагина

```bash
hermes plugins install file:///Users/erkin/algo/local-sync --ref <40-hex-sha>
hermes plugins enable local-sync
hermes gateway restart          # подхватится в новой сессии
```

На обеих машинах одинаково. Настройки — Desktop → Settings → Plugins → local-sync
(`sync_interval`, `notify`, `auto_start`, `roots`); они же в
`plugins.entries.local-sync.settings` в config.yaml.

Опубликовать: `git tag v0.1.0 && git push --tags`, раздавать
`hermes plugins install owner/local-sync --ref <sha>`.

## Знакомство и синхронизация

Демон поднимается сам при первом вызове инструмента (`auto_start`). Дальше — через
интерфейс, агента или CLI. Два способа знакомства, оба требуют человека с обеих сторон:

```bash
# 1) код на экране
bin/local-sync peers
bin/local-sync pair ubuntu-pc            # печатает код, ждёт
bin/local-sync accept mac --code 489665  # на второй машине

# 2) общий пароль (рукопожатие по паролю)
bin/local-sync pair ubuntu-pc --password 'общий-пароль'
bin/local-sync accept mac --password 'общий-пароль'

bin/local-sync sync ubuntu-pc --dry-run
bin/local-sync sync ubuntu-pc
bin/local-sync status
```

## Интерфейс (HITL)

```bash
bin/local-sync ui            # поднимет демон, откроет http://127.0.0.1:45873/?t=…
bin/local-sync ui --no-open --ui-port 45900
```

Локальная страница (только loopback, токен в URL, каждый запрос — осознанный клик):

- **Поиск устройств** — выключатель. Выключил — тебя перестают находить в сети
  (широковещательные биконы останавливаются), но уже спаренные машины продолжают
  синхронизироваться: им биконы идут юникастом на сохранённый адрес.
- **Устройства** — кто виден: имя, профиль, адрес, статус `найден` / `спарен`.
  У найденного — «Подключить» и поле пароля, у спаренного — «План» и
  «Синхронизировать» с подтверждением в диалоге.
- **Запросы на подключение** — входящие. Для пароля: поле ввода и «ОК»; для кода:
  сам код и «Подтвердить»; всегда есть «Отклонить». Пока человек не нажал —
  ничего не происходит: ключ пары хранится только в памяти инициатора до подтверждения.
- **Результат** — pretty-JSON плана или применения (сколько pulled/pushed,
  список конфликтов с путями сайдкаров).

Рукопожатие по паролю: обе машины вводят один и тот же пароль → ключ
`PBKDF2-SHA256(пароль, pair_id, 200k)`; стороны обмениваются HMAC-тегами
(`confirm` / `ack`) и показывают одинаковый 6-значный код как SAS — его видно
на обоих экранах, так что опечатку в пароле замечаешь сразу. Пароль нигде не
сохраняется; на диске оказывается только выведенный ключ пары.

Инструменты для агента: `local_sync_status`, `local_sync_peers`,
`local_sync_pair`, `local_sync_accept`, `local_sync_plan`, `local_sync_apply`.

## Демон постоянно

Плагин поднимает его по требованию; чтобы синк шёл без захода в Hermes — сервис:

```bash
# Ubuntu
mkdir -p ~/.config/systemd/user
cp deploy/local-sync.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now local-sync
loginctl enable-linger $USER
journalctl --user -u local-sync -f

# macOS
cp deploy/com.local.sync.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.local.sync.plist
tail -f /tmp/local-sync.log
```

`bin/local-sync init` создаёт `~/.local-sync/config.json`: `name`, `profile`,
`profile_dir`, `roots`, `sync_interval` (300 с), `notify`, `auto_sync`.
Профиль берётся из `HERMES_HOME`, иначе `~/.hermes/profiles/algo`.

## Конфликты

Три точки: локальный файл, файл пира, хеш с прошлой синхронизации.

- изменилась одна сторона → едет она;
- обе по-разному: `memories/*.md` сливаются по `§`; остальное — локальная версия
  побеждает, версия пира ложится рядом `<имя>.conflict-<peer>-<ts>`, следующий
  цикл досылает локальную, и пара сходится;
- первый контакт без базы → побеждает более свежий mtime.

Перед перезаписью — бэкап в `~/.local-sync/backups/<ts>/`.

## Безопасность

- Знакомство по коду, который видно на обоих экранах; после — HMAC-SHA256 на
  каждый запрос, окно 5 минут.
- `/ctl` принимается только с loopback и с токеном из `~/.local-sync/state.json` (0600).
- Трафик в LAN не шифруется: подпись защищает от подмены, не от прослушивания.
- Без ключа пары API отдаёт только `/hello`, остальное — 401. Пути нормализуются,
  выход за пределы профиля запрещён.

## Диагностика

Если пиры не видят друг друга: broadcast режут гостевые и корпоративные Wi-Fi.
После знакомства демон шлёт биконы ещё и юникастом на сохранённый адрес пира, так
что связь переживает пропажу broadcast. Первый контакт в такой сети — вручную:
`bin/local-sync --beacon-target 192.168.1.42:45870 run` на одной стороне.

## Разработка

```bash
python3 -m unittest tests.test_sync     # 12 тестов, ~9 c
hermes plugins doctor . --ci            # discovery + import + register
hermes plugins validate .               # гейт каталога: 15 проверок
```

Правило одно: один дискаверер на машину. CLI, UI и MCP-сервер проксируются в уже
запущенный демон через `/ctl`; второй бикон на том же хосте даст пиру два
одинаковых имени.

Интерфейс живёт отдельным процессом (`local_sync/ui.py`), тоже только на
127.0.0.1 и с токеном в URL — это защита от CSRF, а не от локального
пользователя.
