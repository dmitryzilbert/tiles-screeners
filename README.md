# WallWatch

Мониторинг стенок в стакане через T-Invest gRPC (T-Bank / T-Investments). Работает только с рыночными данными — без торговли.

## Быстрый старт

1. Создайте виртуальное окружение.
2. Установите зависимости.
3. Создайте `.env` и `config.yaml`.
4. Запустите мониторинг.
5. Проверьте работу (лог heartbeat / Telegram `/ping`).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
cp config.example.yaml config.yaml
wallwatch run --symbols SBER,GAZP --depth 20 --config config.yaml
```

## Требования

- Python 3.11+
- `venv` (рекомендуется использовать изолированное окружение)
- Доступ к T-Invest gRPC (зависимость устанавливается через пакет `t-tech-investments`)

## Установка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Использование

```bash
cp .env.example .env
# отредактируйте .env
wallwatch run --symbols SBER,GAZP --depth 20 --config config.yaml
```

Пример `.env`:

```dotenv
tinvest_token="..."
```

`.env` загружается автоматически из текущей директории (или родительского корня проекта).
Токен читается из `tinvest_token` (или устаревшего `invest_token`). Варианты в верхнем регистре считаются устаревшими, но пока поддерживаются; установите `warn_deprecated_env=1`, чтобы получить одноразовое предупреждение при старте.

## Переменные окружения

- `tinvest_token` (ОБЯЗАТЕЛЬНО): gRPC токен для T-Invest.
- `invest_token` (НЕОБЯЗАТЕЛЬНО): устаревшее имя токена (используйте только если `tinvest_token` не задан).
- `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` (НЕОБЯЗАТЕЛЬНО, рекомендуется для деплоя): путь к PEM-encoded CA bundle для gRPC TLS.
- `tinvest_ca_bundle_path` (НЕОБЯЗАТЕЛЬНО): путь к PEM-encoded CA bundle для gRPC TLS.
- `tinvest_ca_bundle_b64` (НЕОБЯЗАТЕЛЬНО): base64-encoded PEM bundle для gRPC TLS.
- `wallwatch_retry_backoff_initial_seconds` (НЕОБЯЗАТЕЛЬНО, по умолчанию `1.0`): начальный backoff для повторных подключений.
- `wallwatch_retry_backoff_max_seconds` (НЕОБЯЗАТЕЛЬНО, по умолчанию `30.0`): максимальный backoff для повторных подключений.
- `wallwatch_stream_idle_sleep_seconds` (НЕОБЯЗАТЕЛЬНО, по умолчанию `3600.0`): idle sleep между keep-alive.
- `wallwatch_instrument_status` (НЕОБЯЗАТЕЛЬНО, по умолчанию `BASE`): статус инструментов для поиска (`BASE` или `ALL`).
- `tg_bot_token` (ОБЯЗАТЕЛЬНО для Telegram-режима): токен Telegram-бота.
- `tg_chat_id` (ОБЯЗАТЕЛЬНО для Telegram-режима): chat id для алертов (через запятую для нескольких).
- `tg_allowed_user_ids` (НЕОБЯЗАТЕЛЬНО): список user id, которым разрешены команды (через запятую).
- `tg_polling` (НЕОБЯЗАТЕЛЬНО, по умолчанию `true`): включить polling.
- `tg_parse_mode` (НЕОБЯЗАТЕЛЬНО, по умолчанию `HTML`): режим форматирования (`HTML` или `MarkdownV2`).
- `warn_deprecated_env` (НЕОБЯЗАТЕЛЬНО, по умолчанию `false`): одноразовое предупреждение, если используются ключи в верхнем регистре.

> Важно: ключи в `.env` должны быть в нижнем регистре, как указано выше.

## Конфигурация

Используйте YAML-конфиг, чтобы настраивать пороги детектора без изменения кода. Секреты храните в `.env`.

Пример `config.yaml` (см. `config.example.yaml`):

```yaml
logging:
  level: INFO

marketdata:
  depth: 20

walls:
  top_n_levels: 5
  candidate_ratio_to_median: 12.0
  candidate_max_distance_ticks: 2
  confirm_dwell_seconds: 3.0
  confirm_max_distance_ticks: 1
  consume_window_seconds: 3.0
  consume_drop_pct: 25.0
  teleport_reset: true

debug:
  walls_enabled: false
  walls_interval_seconds: 1.0

telegram:
  enabled: false
  polling: true
  poll_interval_seconds: 1.0
  startup_message: false
  send_events:
    - wall_confirmed
    - wall_consuming
    - wall_lost
  cooldown_seconds:
    wall_candidate: 60
    wall_confirmed: 0
    wall_consuming: 0
    wall_lost: 0
  disable_web_preview: true
  commands_enabled: true
```

### Приоритет настроек

Порядок приоритета для перекрывающихся настроек:

1. CLI-флаги (явно переданы)
2. YAML-конфиг (`--config`)
3. Переменные окружения / значения по умолчанию

`marketdata.depth` применяется, если не задан `--depth`. `logging.level` применяется, если не задан `--log-level`.

## Рекомендации по калибровке

- `k_ratio`: начните с 8–15. Более высокие значения уменьшают ложные срабатывания, но могут пропускать средние стенки.
- `dwell_seconds`: 20–60 секунд. Длинная выдержка снижает шанс спуфинга, но откладывает алерт.
- `Emin`: минимальный исполненный объем на цене стенки для подтверждения.
- `cancel_share_max`: около 0.6–0.8. Более низкие значения требуют больше подтверждений фактом исполнения.
- `consuming_drop_pct`: 15–30%. Увеличьте, чтобы уменьшить частоту алертов “consuming”.
- `distance_ticks`: 1–10. Меньшие значения фокусируют на стенках ближе к рынку.

## Архитектура

- `api/client.py`: gRPC клиент, разрешение инструментов, подписки.
- `detector/wall_detector.py`: логика детектора стенок (без I/O).
- `state/models.py`: датаклассы для событий и состояния.
- `notify/notifier.py`: интерфейс алертов + консольная реализация.
- `app/main.py`: CLI + оркестрация рантайма.

## Тестирование

```bash
pytest
```

## Режимы запуска

- **Обычный режим**: `wallwatch run ...`
- **Debug стенок**: `--debug-walls` и `--debug-walls-interval`
- **С конфигом**: `--config config.yaml`

Примеры:

```bash
# обычный режим
wallwatch run --symbols SBER,GAZP --depth 20 --config config.yaml

# включить debug стенок
wallwatch run --symbols SBER,GAZP --config config.yaml --debug-walls --debug-walls-interval 1.0

# переопределить уровень логов
wallwatch run --symbols SBER,GAZP --config config.yaml --log-level DEBUG
```

## Примеры для платформ

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
copy .env.example .env
copy config.example.yaml config.yaml
.\.venv\Scripts\wallwatch.exe run --symbols SBER,GAZP --depth 20 --config config.yaml
```

**Linux/macOS**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
cp config.example.yaml config.yaml
wallwatch run --symbols SBER,GAZP --depth 20 --config config.yaml
```

## Проверка окружения (doctor)

```bash
wallwatch doctor
wallwatch doctor --symbols SBER,GAZP
```

`doctor` проверяет обязательные переменные окружения, CA bundle конфигурацию и корректность инструментов. В обычном режиме флаг `--symbols` обязателен, в `doctor` — опционален.

## Telegram интерфейс

Примеры:

```bash
# установить Telegram extra
pip install -e ".[telegram]"

# только CLI мониторинг
wallwatch run --symbols SBER,GAZP --depth 20 --config config.yaml

# Telegram интерфейс (команды + мониторинг)
wallwatch telegram --symbols SBER,GAZP --config config.yaml

# Telegram интерфейс + алерты (тот же режим; алерты уходят в tg_chat_id)
wallwatch telegram --symbols SBER,GAZP --config config.yaml
```

Как включить Telegram режим:

1. Установите extra зависимости: `pip install -e ".[telegram]"`.
2. В `config.yaml` выставьте `telegram.enabled: true`.
3. В `.env` задайте `tg_bot_token` и `tg_chat_id` (и при необходимости `tg_allowed_user_ids`).
4. Запустите `wallwatch telegram ...`.

Проверка токена и отправки сообщений (кратко):

```bash
# проверка токена
curl "https://api.telegram.org/bot<YOUR_TOKEN>/getMe"

# отправка тестового сообщения
curl -X POST "https://api.telegram.org/bot<YOUR_TOKEN>/sendMessage" \
  -d chat_id=<CHAT_ID> \
  -d text="wallwatch ping"
```

Команды бота:

- `/start` — справка
- `/help` — список команд
- `/ping` — проверка здоровья
- `/status` — статус стрима
- `/watch <symbols>` — задать список (до 10)
- `/unwatch <symbols>` — убрать символы
- `/list` — текущие символы

## Troubleshooting

- **Проблемы с CA bundle / TLS**: установите системные сертификаты или задайте `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH`, `tinvest_ca_bundle_path`, `tinvest_ca_bundle_b64`.
- **Windows: Ctrl+C не завершает процесс**: запустите в новой консоли PowerShell и используйте `Stop-Process -Id <PID>` либо закрывайте окно терминала.
- **"wallwatch из глобального python"**: убедитесь, что активировано `venv` и используется `pip install -e .` именно внутри окружения.
- **`rx=0` когда биржа закрыта**: это ожидаемо — поток ордербуков может быть пустым вне торговых сессий.

## Deployment notes

Для контейнеров с минимальным набором пакетов:

- Установите системные сертификаты (рекомендуется):
  ```bash
  apt-get update && apt-get install -y ca-certificates
  ```
- Или предоставьте кастомный CA bundle (рекомендуется использовать gRPC env var напрямую):
  ```bash
  export GRPC_DEFAULT_SSL_ROOTS_FILE_PATH=/run/secrets/ca.pem
  # или используйте обертку wallwatch:
  export tinvest_ca_bundle_path=/run/secrets/ca.pem
  # или
  export tinvest_ca_bundle_b64="$(base64 -w0 /run/secrets/ca.pem)"
  ```

## Безопасность

- Не коммитьте `.env` в репозиторий.
- Храните токены в `env`/`.env`, а не в коде или конфиге.
- При утечке Telegram токена немедленно перевыпустите его у BotFather.
