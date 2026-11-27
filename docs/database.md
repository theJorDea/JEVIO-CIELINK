# Локальная база PostgreSQL

Из корня репозитория, с запущенным Docker Engine и Docker Compose v2 с поддержкой `--wait`:

```sh
docker compose up -d --wait db
docker compose exec db psql -U cielink -d cielink -c '\dt public.*'
```

В списке должны быть три таблицы: `sessions` (канал и необязательный номер), `events` (порядок внутри сессии, смещение в миллисекундах, тип, необязательная ревизия и данные) и `operations` (ID операции, сессия, ключ идемпотентности и статус). Подключение с хоста: `postgresql://cielink:cielink_local@127.0.0.1:5432/cielink`. Пароль учебный; порт доступен только через loopback.

## Проверка идемпотентности

Откройте `docker compose exec db psql -U cielink -d cielink` и выполните в учебной базе:

```sql
BEGIN;
INSERT INTO sessions (session_id, channel) VALUES ('bootstrap-demo', 'web');
INSERT INTO operations (operation_id, session_id, idempotency_key, status)
VALUES ('demo-1', 'bootstrap-demo', 'bootstrap-retry', 'demo');
INSERT INTO operations (operation_id, session_id, idempotency_key, status)
VALUES ('demo-2', 'bootstrap-demo', 'bootstrap-retry', 'demo')
ON CONFLICT (idempotency_key) DO NOTHING;
SELECT operation_id FROM operations WHERE idempotency_key = 'bootstrap-retry';
ROLLBACK;
```

Ожидается только `demo-1`: другой ID операции не позволяет повторно записать тот же ключ. `ROLLBACK` убирает проверочные записи. Для выхода из psql введите `\q`.

## Повторный запуск

`docker compose down` останавливает контейнер, сохраняя данные в томе `pgdata`. Повторите `docker compose up -d --wait db` для запуска с теми же данными. PostgreSQL выполняет SQL-файлы верхнего уровня `migrations/` **только при первом старте с пустым томом**. На существующем томе примените миграции CRM без удаления данных:

```sh
docker compose exec db psql -U cielink -d cielink -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/002_operations.sql -f /docker-entrypoint-initdb.d/003_spoken_reference.sql
```

Существующим квитанциям присваиваются номера для поиска; UUID и условия не меняются. Миграцию CRM можно запускать повторно.

Чтобы проверить инициализацию с нуля, выполните команды ниже **только если локальные данные больше не нужны**:

```sh
docker compose down -v
docker compose up -d --wait db
```
