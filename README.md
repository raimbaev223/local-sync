# local-sync

Синхронизация профилей Hermes между машинами в локальной сети. Два демона находят
друг друга UDP-биконом, знакомятся по 6-значному коду и дальше возят файлы по HTTP
с HMAC-подписью. Без облака, без git, без сторонних зависимостей — только stdlib
Python 3.9+.

## Что синхронизируется

| Слой | Путь | Да |
|---|---|---|
| Свои скиллы | `skills/**` (кроме bundled из `.bundled_manifest`) | ✅ |
| Память | `memories/*.md` — union-merge по секциям `§` | ✅ |
| Душа/правила | `SOUL.md` | ✅ |
| Хуки | `hooks/**` | ✅ |
| Desktop-плагины | `desktop-plugins/**` | ✅ |
| Cron-определения | `cron/**` (без `executions.db`) | ✅ |
| `config.yaml` | — | ❌ машиноспецифичен, синкать ключи вручную |
| `.env`, `auth.json` | — | ❌ секреты остаются на машине |
| `state.db`, сессии | — | ❌ пока нет (см. Roadmap) |
| `cache/`, `logs/`, `backups/` | — | ❌ |

## Установка (обе машины)

```bash
git clone <this repo> ~/algo/local-sync     # или scp -r
chmod +x ~/algo/local-sync/local-sync
~/algo/local-sync/local-sync init          # создаст ~/.local-sync/config.json
```

`config.json` можно поправить: `name`, `profile`, `profile_dir`, `roots`,
`sync_interval` (сек, по умолчанию 300), `notify`, `auto_sync`.

Профиль по умолчанию берётся из `HERMES_HOME`, иначе `~/.hermes/profiles/algo`.

### Ubuntu: systemd --user

```bash
mkdir -p ~/.config/systemd/user
cp ~/algo/local-sync/deploy/local-sync.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now local-sync
loginctl enable-linger $USER      # чтобы жил без логина
journalctl --user -u local-sync -f
```

### macOS: launchd

```bash
cp ~/algo/local-sync/deploy/com.local.sync.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.local.sync.plist
tail -f /tmp/local-sync.log
```

Без сервиса: `local-sync run` в отдельном терминале.

## Знакомство

1. На машине A: `local-sync peers` — в списке `peers` появится устройство из сети.
2. `local-sync pair pc` → печатает код (например `925436`); на машине B прилетает
   уведомление с тем же кодом.
3. На B: `local-sync accept mac --code 925436` (без `--code` тоже можно — но сверь глазами).
4. Дальше всё делает автопин: `sync_interval` секунд, только при видимом пире.

Проверить, что именно поедет, до применения:

```bash
local-sync sync pc --dry-run     # pull/push/conflict по каждому файлу
local-sync sync pc               # применить
```

## Как агент этим пользуется (MCP)

```bash
hermes mcp add local-sync --command ~/algo/local-sync/local-sync --args mcp
hermes mcp test local-sync
```

Инструменты: `sync_status`, `peers_list`, `pair_begin`, `pair_accept`, `sync_plan`,
`sync_apply`. Если демон уже запущен — MCP-сервер к нему проксируется, второго
дискаверера на машине не появляется.

## Конфликты

Три точки сравнения: локальный файл, файл пира и хеш с прошлой синхронизации.

- изменилась одна сторона → едет она;
- обе, но по-разному → `memories/*.md` сливаются по секциям, остальные файлы:
  локальная версия остаётся, версия пира кладётся рядом как
  `<файл>.conflict-<peer>-<ts>`; следующий цикл досылает локальную, пара сходится;
- первый контакт без базы → побеждает более свежий mtime.

Перед перезаписью старые версии складываются в `~/.local-sync/backups/<ts>/`.

## Безопасность

- Знакомство — по коду, который видно на обоих экранах (SAS).
- После знакомства каждый запрос подписан HMAC-SHA256 с ключом пары; окно 5 минут.
- Локальное управление (`/ctl`) принимается только с 127.0.0.1 и с токеном из
  `~/.local-sync/state.json` (0600).
- Трафик по LAN **не шифруется**: подпись защищает от подмены, не от прослушивания.
  Для публичных Wi-Fi это осознанный компромисс; для домашней/офисной сети — норма.
- Порт API слушает 0.0.0.0, но без ключа пары отдаёт только `/hello` и 401 на всё
  остальное; пути нормализуются, выход за пределы профиля запрещён.

## Диагностика

```bash
local-sync status          # кто я, с кем спарен
local-sync peers           # кто виден в сети + висящие запросы
local-sync init            # пересоздать/посмотреть конфиг
```

Если пиры не видят друг друга:broadcast часто режут гостевые/корпоративные Wi-Fi.
После первого знакомства демон шлёт биконы ещё и юникастом на сохранённый адрес
пира, так что связь переживает пропажу broadcast. Первое знакомство в такой сети —
вручную: `local-sync --beacon-target 192.168.1.42:45870 run` на одной стороне.

## Roadmap

- [x] discovery, pairing, per-file merge, memory union, MCP, notifications, autosync
- [ ] снапшот `state.db` как read-only зеркало: `session_search` по сессиям второй машины
- [ ] панель в desktop-приложении: найденные пиры, дрейф файлов, кнопка синхронизации
      (нужен Python-плагин `~/.hermes/plugins/local-sync/` с `plugin_api.py`,
      который проксирует `/ctl` демона)
- [ ] `local-sync pair` по QR/коду без ввода команды
