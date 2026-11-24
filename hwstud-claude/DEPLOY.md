# Установка hwstud.ru на VPS REG.RU

Документ предназначен для выполнения вручную после отдельного решения о размещении. Разработка и локальные тесты не выполняют этих команд на сервере автоматически.

## 1. Исходные условия

- Ubuntu 24.04 LTS на уже заказанном VPS REG.RU.
- Домен `hwstud.ru` должен указывать на сервер:
  - `A hwstud.ru → 89.108.98.92`
  - `CNAME www → hwstud.ru`
- Нужен SSH-доступ с правами sudo.
- Нужны собственные значения: ключ ProxyAPI, пароль преподавателя, параметры и пароль приложения SMTP.
- Секреты не передавать в чат, не включать в Git и не записывать в код.

Приложение использует один процесс uvicorn, SQLite и внутреннюю очередь. Docker, Nginx, внешняя база, Redis, cron и отдельные воркеры не нужны.

## 2. Системные пакеты и Caddy

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv sqlite3 ufw curl
```

Проверить версию Python:

```bash
python3 --version
```

Нужна ветка 3.12. Если она отличается, остановиться и выбрать совместимый пакет Python, не меняя код произвольно.

Caddy устанавливается из официального репозитория Caddy для Debian/Ubuntu. Использовать актуальную официальную инструкцию: <https://caddyserver.com/docs/install#debian-ubuntu-raspbian>. После установки:

```bash
caddy version
```

## 3. Пользователь и каталоги

```bash
sudo useradd --system --home /opt/hwstud --shell /usr/sbin/nologin hwstud 2>/dev/null || true
sudo install -d -o hwstud -g hwstud -m 750 /opt/hwstud
sudo install -d -o hwstud -g hwstud -m 750 /opt/hwstud/app
sudo install -d -o hwstud -g hwstud -m 750 /opt/hwstud/data
```

`data/` содержит базу, лекции, работы, копии и архивы. Не размещать её в публичном каталоге Caddy.

## 4. Загрузка кода

Вариант через приватный Git:

```bash
sudo -u hwstud git clone <адрес-приватного-репозитория> /opt/hwstud/app
```

Если репозиторий уже существует, обновление описано ниже. Не использовать публичный репозиторий с материалами курса или секретами.

Вариант через локальный архив, выполняемый на своём компьютере:

```powershell
tar --exclude=.venv --exclude=.env --exclude=data --exclude=.git --exclude=__pycache__ -czf hwstud-code.tar.gz .
scp hwstud-code.tar.gz <пользователь>@89.108.98.92:/tmp/
```

На сервере:

```bash
sudo tar -xzf /tmp/hwstud-code.tar.gz -C /opt/hwstud/app
sudo chown -R hwstud:hwstud /opt/hwstud/app
```

Проверить, что присутствуют `app/`, `static/`, `migrations/`, `requirements.txt` и `deploy/`.

## 5. Python-окружение

```bash
sudo -u hwstud python3 -m venv /opt/hwstud/venv
sudo -u hwstud /opt/hwstud/venv/bin/pip install -r /opt/hwstud/app/requirements.txt
```

Все зависимости приложения закреплены в `requirements.txt`. Новых сервисов добавлять не нужно.

## 6. Файл секретов `.env`

Создать пустой файл с правильными правами:

```bash
sudo install -o hwstud -g hwstud -m 600 /dev/null /opt/hwstud/.env
sudo -u hwstud cp /opt/hwstud/app/deploy/hwstud.env.example /opt/hwstud/.env
sudo chmod 600 /opt/hwstud/.env
```

Отредактировать файл локально на сервере:

```bash
sudo -u hwstud nano /opt/hwstud/.env
```

Если nano отсутствует, использовать другой редактор без вывода содержимого файла в общий журнал.

Обязательные значения:

```ini
APP_BASE_URL=https://hwstud.ru
SESSION_SECRET=<сгенерированная-случайная-строка>
ADMIN_PASSWORD_HASH=<хеш-scrypt; можно оставить пустым для первого входа>
PROXYAPI_KEY=<ключ-владельца>
SMTP_HOST=<SMTP-сервер>
SMTP_PORT=465
SMTP_SECURITY=ssl
SMTP_USER=<почтовый-ящик>
SMTP_PASSWORD=<пароль-приложения>
MAIL_FROM=<адрес-отправителя>
MAIL_TO=<адрес-получателя>
DATA_DIR=/opt/hwstud/data
TZ_DISPLAY=Europe/Moscow
```

Сгенерировать секрет сессии можно командой, не записывая его в историю как значение:

```bash
openssl rand -base64 48
```

Для хеша пароля:

```bash
cd /opt/hwstud/app
sudo -u hwstud /opt/hwstud/venv/bin/python -m app.tools.hash_password
```

Пароль вводится в интерактивном запросе и не остаётся в истории команды. В `.env` копируется только напечатанный хеш. Сам пароль в чат не передаётся.

Если собственный хеш не задан, первый вход преподавателя выполняется с начальным паролем `admin`. Сайт сразу требует установить новый пароль через «Настройки → Смена пароля» и блокирует остальную админку до смены. Новый пароль хранится в SQLite как scrypt-хеш. Не оставлять сайт доступным другим пользователям с начальным паролем: выполнить смену при первом же входе.

При использовании STARTTLS вместо SSL:

```ini
SMTP_PORT=587
SMTP_SECURITY=starttls
```

Порт 25 не использовать.

## 7. Проверка перед первым запуском

- В кабинете ProxyAPI у ключа сайта включить разрешение «Запрос баланса».
- При необходимости задать бюджет ключа на месяц курса.
- Преподавателю сверить модели и цены с каталогом ProxyAPI.
- Добавить адрес отправителя в контакты или белый список почтового ящика.
- Убедиться, что `.env` имеет владельца `hwstud` и режим 600.
- Не публиковать исходные лекции или тестовые данные.

Проверка моделей может быть платной. Выполнять её только после отдельного решения:

```bash
cd /opt/hwstud/app
sudo -u hwstud /opt/hwstud/venv/bin/python -m app.tools.check_models --confirm-paid
```

Команда проверяет обе модели с текстом и изображением 1600 × 1600, записывает результат в `TESTING.md`, обновляет `image_tokens` и разрешает прикладные GPT-задачи только после успеха.

## 8. systemd

```bash
sudo cp /opt/hwstud/app/deploy/hwstud.service /etc/systemd/system/hwstud.service
sudo systemctl daemon-reload
sudo systemctl enable hwstud
sudo systemctl start hwstud
sudo systemctl status hwstud --no-pager
```

Unit ограничивает процесс `MemoryMax=1200M`, запускает один worker и использует `/opt/hwstud/.env`.

Проверить журнал:

```bash
sudo journalctl -u hwstud -n 100 --no-pager
```

Миграции SQLite применяются при старте. Отдельный процесс для очереди не запускается.

## 9. Caddy и HTTPS

```bash
sudo cp /opt/hwstud/app/deploy/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy сам получает сертификат Let's Encrypt после корректной настройки DNS. Порт 8000 остаётся на `127.0.0.1`.

Проверить:

```bash
curl -I https://hwstud.ru
curl https://hwstud.ru/health
sudo ss -ltn | grep 8000
```

Первая команда должна вернуть 200 или 302, вторая — JSON со статусом `ok`. Порт 8000 не должен быть доступен снаружи.

## 10. Брандмауэр

Сначала убедиться, что SSH-доступ работает и правило для порта 22 добавлено:

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

Не блокировать текущий SSH-сеанс без проверки правил.

## 11. Swap и журналы

Если файл подкачки ещё не создан, настроить 1 ГБ вручную:

```bash
sudo fallocate -l 1G /swapfile-hwstud
sudo chmod 600 /swapfile-hwstud
sudo mkswap /swapfile-hwstud
sudo swapon /swapfile-hwstud
```

Для сохранения после перезагрузки добавить в `/etc/fstab`:

```text
/swapfile-hwstud none swap sw 0 0
```

Для ограничения journald добавить `SystemMaxUse=200M` в `/etc/systemd/journald.conf` и перезапустить `systemd-journald`.

## 12. Проверка после установки

1. Войти студентом по тестовому нику и проверить карту курса.
2. Войти преподавателем, создать тестовую лекцию, сохранить скрытой, открыть тестовой группе.
3. Проверить режим студента и защищённый WebP.
4. Загрузить тестовую работу, включая DOCX с несколькими скриншотами.
5. Проверить одобрение преподавателем и CSV.
6. В «Настройки → ProxyAPI» выполнить проверку баланса и при необходимости явный пробный запрос к модели.
7. В настройках отправить пробное письмо и убедиться в доставке.
8. Проверить объявление, вопрос, ответ и почтовое уведомление.
9. Проверить экзамен для одной группы и доступность заданий/справки.
10. Создать и скачать копию сайта, проверить архив.
11. Один раз выполнить восстановление на чистом каталоге по разделу ниже.
12. В браузере на вкладке «Сеть» не должно быть внешних ресурсов страниц.

### Память под нагрузкой

Перед началом курса провести отдельный нагрузочный тест:

- импорт лекции;
- одновременный разбор двух DOCX, в каждом пять изображений с предельно допустимым числом пикселей;
- две проверки работ.

```bash
free -m
systemctl show hwstud -p MemoryCurrent -p MemoryMax
```

Процесс не должен превышать `MemoryMax=1200M`, система должна сохранять свободно больше 300 МБ. Если предел не соблюдён, не запускать курс до уменьшения пиковой нагрузки и повторной проверки. Результаты записать в `TESTING.md`.

## 13. Обновление сайта

```bash
sudo systemctl stop hwstud
cd /opt/hwstud/app
```

Создать копию базы через админку до остановки либо использовать SQLite API в безопасном сценарии. Затем обновить код:

```bash
sudo -u hwstud git -C /opt/hwstud/app pull
sudo -u hwstud /opt/hwstud/venv/bin/pip install -r /opt/hwstud/app/requirements.txt
sudo systemctl start hwstud
```

Для обновления из архива распаковать новый код по той же процедуре, сохраняя `/opt/hwstud/.env` и `/opt/hwstud/data`.

После старта миграции применяются автоматически. Проверить `/health`, вход, лекцию в режиме студента и журнал systemd.

## 14. Восстановление из архива «база + лекции»

Работы студентов в этот архив не входят. Их ZIP преподаватель хранит отдельно после скачивания.

Остановить приложение:

```bash
sudo systemctl stop hwstud
```

Сохранить текущее повреждённое состояние вне `/opt/hwstud/data`, если оно требуется для анализа.

Распаковать архив так, чтобы каталоги `data/` оказались в `/opt/hwstud`:

```bash
sudo tar -xzf /путь/к/site-YYYYMMDD.tar.gz -C /opt/hwstud
```

Проверить базу:

```bash
sudo sqlite3 /opt/hwstud/data/hwstud.db "PRAGMA integrity_check;"
```

Ожидается `ok`. Вернуть владельца:

```bash
sudo chown -R hwstud:hwstud /opt/hwstud/data
sudo systemctl start hwstud
```

Войти в админку и открыть лекцию в режиме студента. Проверить группы, задания и справку.

Для восстановления только файла базы скопировать резервную `.db` как `/opt/hwstud/data/hwstud.db`, проверить `integrity_check`, назначить владельца и запустить сервис.

## 15. Архив работ и удаление после курса

- «Скачать архив работ» собирает ZIP в фоне и показывает его на странице «Данные курса».
- На сервере остаётся не более одного временного архива; он удаляется через час после создания, даже если не скачан.
- Еженедельные архивы работ не содержат.
- Удаление данных студентов требует двух подтверждений.
- Сначала создаётся и проверяется новая cleanup-копия без студенческих данных, затем удаляются прежние копии, работы и переписка.
- Лекции, глоссарий, литература, задания и настройки остаются.
- После перезапуска удаление продолжается с последнего записанного шага.

## 16. Что установка не делает

- Не меняет DNS или тариф REG.RU.
- Не оплачивает и не заказывает услуги.
- Не включает автопополнение ProxyAPI.
- Не публикует материалы лекций отдельно от правил сайта.
- Не отправляет секреты во внешние системы, кроме явно настроенных ProxyAPI и SMTP.
