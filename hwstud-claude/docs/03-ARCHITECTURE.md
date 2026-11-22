# Архитектура сайта hwstud.ru

Версия 1.1 от 22.11.2026. Как реализовать функции из `01-FUNCTIONS.md` минимальным набором инструментов.

## 1. Разрешённый стек

| Слой | Инструмент | Зачем |
|---|---|---|
| Язык | Python 3.12 (есть в Ubuntu 24.04) | Всё приложение |
| Веб | FastAPI + uvicorn | Маршруты, формы, загрузка файлов |
| Шаблоны | Jinja2 | Страницы собираются на сервере |
| Формы | python-multipart | Загрузка файлов |
| Сессии | itsdangerous (через `SessionMiddleware` Starlette) | Подписанная cookie |
| База | SQLite, модуль `sqlite3`, режим WAL | Одна база-файл, без ORM |
| HTTP-клиент | httpx | Запросы к ProxyAPI, проверка ссылок литературы |
| PPTX | python-pptx (вместе с lxml и Pillow) | Заметки, заголовки, разделы слайдов |
| PDF | PyMuPDF | Картинки слайдов, текст работ студентов, выгрузка PDF |
| DOCX | python-docx | Текст работ студентов, выгрузка DOCX |
| XLSX | openpyxl | Выгрузка глоссария в Excel |
| Почта | `smtplib` и `email` из стандартной библиотеки | Письма преподавателю |
| HTTPS | Caddy | Сертификат Let's Encrypt автоматически, раздача статики |
| Запуск | systemd | Автозапуск и перезапуск приложения |
| Тесты | pytest | Только для разработки |

Файл `requirements.txt` содержит только эти пакеты с закреплёнными версиями. Новые пакеты — только с разрешения пользователя.

Шрифты IBM Plex Sans и IBM Plex Mono (лицензия OFL) скачиваются один раз при разработке из официального репозитория IBM и кладутся в `static/fonts/` в формате WOFF2, только кириллица и латиница.

## 2. Структура проекта

```
hwstud/
  app/
    main.py            # создание приложения, middleware, запуск фоновых задач
    config.py          # чтение .env
    db.py              # подключение SQLite, применение миграций
    auth.py            # вход по нику, вход преподавателя, проверки доступа
    access.py          # кто что видит: открытие лекций, группы, экзамен
    routes/            # student_*.py, admin_*.py — по разделам
    services/
      balance.py         # баланс ProxyAPI, уровни, прогноз, письма
      lecture_import.py  # разбор PPTX + PDF
      glossary.py        # сокращения, объединение, конфликты
      literature.py      # источники, дубли, проверка ссылок
      grading.py         # агент проверки, ProxyAPI
      mailer.py          # SMTP
      exports.py         # CSV, XLSX, DOCX, PDF, Markdown
    worker.py          # фоновый цикл: очередь jobs, письма, ссылки, копии
    templates/         # Jinja2
  static/
    css/ js/ fonts/ icons/ brand/
  migrations/          # 001_init.sql, 002_....sql — применяются по порядку при старте
  data/                # НЕ в git: hwstud.db, lectures/, uploads/, backups/
  tests/
  .env.example
  requirements.txt
  README.md  DEPLOY.md  TESTING.md
  deploy/
    hwstud.service     # unit systemd
    Caddyfile
    backup.sh
```

## 3. Модель данных (SQLite)

Миграции — простые SQL-файлы. Таблица `schema_version` хранит номер последней применённой миграции.

| Таблица | Главные поля |
|---|---|
| `groups` | id, name, max_members (20), enroll_open |
| `students` | id, group_id, nick, created_at, blocked, login_token; ник уникален на всём сайте (без учёта регистра). ФИО и контакты не хранятся |
| `lectures` | id, slug, num, title, status (hidden/open), open_at, announce, header_lines, updated_at |
| `lecture_groups` | lecture_id, group_id — пусто означает «для всех групп» |
| `lecture_sections` | lecture_id, idx, title, first_slide |
| `slides` | lecture_id, n, title, notes_html, img, thumb, section_idx, missing_notes |
| `slide_views` | student_id, lecture_id, n, viewed_at |
| `terms` | id, term, expansion, english, status (draft/verified/hidden), unused |
| `term_refs` | term_id, lecture_id, slide_n |
| `term_conflicts` | term_id, lecture_id, slide_n, variant |
| `sources` | id, author, title, url, type, annotation, priority (main/extra), url_ok, checked_at |
| `source_refs` | source_id, lecture_id, slide_n, local_number |
| `assignments` | id (1–4), title, condition, reference, criteria, typical_errors, threshold (0,7), due_at, max_attempts (4) |
| `submissions` | id, student_id, assignment_id, attempt, file, mime, size, created_at, status, score, agent_json, confidence, teacher_comment, approved_at |
| `review_requests` | id, submission_id, created_at, status |
| `jobs` | id, kind, payload_json, status, tries, run_after, error, tokens_in, tokens_out |
| `announcements` | id, title, body, link, kind (normal/important/deadline), pinned, publish_at, unpublish_at |
| `announcement_groups` | announcement_id, group_id |
| `announcement_reads` | announcement_id, student_id, read_at |
| `messages` | id, student_id, author (student/teacher), body, ref_lecture, ref_slide, ref_assignment, created_at, teacher_read_at, student_read_at |
| `threads` | student_id, closed_at |
| `exam` | id, active, start_at, end_at, hide_glossary, hide_literature |
| `exam_groups` | exam_id, group_id |
| `settings` | key, value — общие настройки из админки, в том числе пороги баланса |
| `balance_log` | id, at, balance, budget_limit, budget_used, level, source (poll/manual/402) |
| `balance_alerts` | level, sent_at — когда последний раз отправлено письмо об уровне |
| `audit_log` | id, at, actor, action, object, details_json |

Статусы работы: `not_submitted`, `queued`, `checking`, `needs_review`, `revise`, `accepted`. У задачи в `jobs` есть отдельный статус `paused_no_funds`: студент при этом видит «На проверке». На экране — русские названия из `01-FUNCTIONS.md`, раздел 1.

## 4. Доступ: одна функция на всё

Все проверки «можно ли студенту видеть» собраны в `access.py`. Маршруты, поиск, глоссарий, литература и картинки слайдов вызывают только её.

```
lecture_visible(student, lecture, now) =
    not exam_closes(student.group, now, "lectures")
    and (lecture.status == "open" or (lecture.open_at and now >= lecture.open_at))
    and (lecture has no groups or student.group in lecture.groups)
```

- Анонс: лекция не видна, но если `announce` включён и задан `open_at`, в списке показываются только название и дата.
- Преподаватель видит всё. «Режим студента» — флаг в сессии преподавателя: страницы строятся тем же кодом, что у студента, но проверка `lecture_visible` пропускается только для выбранной лекции и только для сессии преподавателя.
- Термины и источники видны студенту, только если у них есть хотя бы одна ссылка на видимую ему лекцию. Номера слайдов показываются только из видимых лекций.
- Открытие по дате вычисляется при каждом запросе. Отдельный планировщик для этого не нужен. Факт открытия по дате записывается в `audit_log` фоновым циклом.

## 5. Вход

- Студент: форма «ник» → если ника нет, выбор группы. Других полей нет. Проверки: группа не заполнена (меньше 20), набор открыт, ник свободен на всём сайте, ник 3–24 символа: буквы, цифры, `_`, `-`.
- Сессия: подписанная cookie на 90 дней, `HttpOnly`, `Secure`, `SameSite=Lax`, внутри `student_id` и `login_token`. Блокировка ника или смена `login_token` в админке выходит из всех устройств.
- Вход по нику без пароля — осознанное решение пользователя. Защита от перебора: не больше 20 попыток входа с одного IP в час.
- Преподаватель: пароль сравнивается с хешем `scrypt` (модуль `hashlib`) из `.env`. Не больше 5 ошибок за 15 минут с одного IP.
- Все формы, меняющие данные, — POST со скрытым CSRF-токеном из сессии.

## 6. Загрузка лекции

1. Преподаватель загружает PPTX и PDF (до 100 МБ каждый).
2. `lecture_import.py`:
   - PDF → картинки страниц через PyMuPDF: WebP шириной 1600 px и миниатюра 320 px, качество 80;
   - PPTX → заголовок слайда, текст заметок, разделы из `p14:sectionLst` в `ppt/presentation.xml` (через lxml);
   - заметки: первая строка жирным — заголовок текста, дальше абзацы; строки «Сокращения: …» отделяются для глоссария; номера `[n]` и строки источников — для литературы;
   - повторяющиеся служебные строки (больше чем на половине слайдов, порог из настроек) переносятся в `header_lines`.
3. Проверки из `01-FUNCTIONS.md`, раздел 2.1. Результат — во временной папке `data/import/<id>/` и на экране предпросмотра.
4. «Сохранить на сайте» переносит данные в базу и `data/lectures/<slug>/`. Повторная загрузка заменяет слайды, но сохраняет статус, дату открытия и группы.
5. Сложные строки сокращений разбирает GPT (задача `jobs`, вид `parse_abbr`), результат попадает на экран подтверждения.

Картинки слайдов отдаёт приложение по маршруту `/l/<slug>/s/<n>.webp` после проверки доступа, с заголовком `Cache-Control: private, max-age=3600`. Прямого доступа к `data/` через Caddy нет. Файлы PPTX и PDF студентам не отдаются никогда.

## 7. Агент проверки

Очередь — таблица `jobs`. Фоновый цикл в том же процессе uvicorn берёт задачи, одновременно не больше 2 запросов к ProxyAPI.

**Подготовка работы:**

- PDF с текстом → текст через PyMuPDF; PDF-скан → до 5 страниц картинками;
- JPG, PNG → картинка, сжатая до 1600 px по длинной стороне;
- DOCX → текст через python-docx;
- TXT → текст в UTF-8 или cp1251.

Запрос: `POST {PROXYAPI_BASE_URL}/chat/completions`, заголовок `Authorization: Bearer {PROXYAPI_KEY}`, модель из `.env`, temperature 0,2. В системном сообщении — роль проверяющего, условие, эталон, критерии, типичные ошибки. В пользовательском — работа.

Ответ — строго JSON:

```json
{
  "verdict": "accept | revise",
  "score": 0,
  "max_score": 10,
  "errors": [{"criterion": "", "description": "", "severity": "low | medium | high"}],
  "classification": "",
  "confidence": 0.0,
  "comment_for_student": ""
}
```

- Если модель поддерживает `response_format` с `json_schema`, используется он. Иначе в промпте требование «только JSON» и проверка ответа в коде.
- Невалидный JSON → одна повторная попытка, потом статус `needs_review`.
- `confidence < threshold` → `needs_review`.
- Ошибка сети или код 5xx → до 3 повторов с паузой 30 с, 2 мин, 10 мин.
- Тайм-аут запроса 120 с.
- Комментарий студенту виден только после одобрения преподавателем.
- Расход токенов записывается в `jobs` и показывается в админке по месяцам.
- В каждом запросе задаётся `max_completion_tokens` (настройка, по умолчанию 1500). Это ограничивает предварительную оценку стоимости в ProxyAPI.
- Модель указывается в формате ProxyAPI `vendor/name`, например `openai/<модель>`; значение берётся из `.env`.
- Перед отправкой — проверка `funds_ok()` (раздел 9а). Ответ 402 — не ошибка работы: задача получает `paused_no_funds`, повторов не тратит.

## 8. Почта

- `smtplib.SMTP_SSL` на порт 465 или `SMTP` + `starttls()` на порт 587 — по настройке в `.env`. Порт 25 не используется.
- Письмо: тема «hwstud: вопрос от <ник>, <группа>», текст сообщения, ссылка на ветку в админке.
- Режим «сводка раз в час» — флаг в настройках: фоновый цикл собирает новые сообщения за час в одно письмо.
- Ошибка отправки не мешает сохранить сообщение: письмо ставится в `jobs` с повтором.

## 9. Фоновый цикл

Один цикл `asyncio`, запускается при старте приложения, шаг 10 с:

| Задача | Частота |
|---|---|
| Очередь `jobs`: проверка работ, разбор сокращений, аннотации источников, письма | Постоянно |
| Сводное письмо преподавателю | Раз в час, если включено |
| Запись в журнал открытия лекций по дате | Раз в минуту |
| Проверка ссылок литературы (HEAD, потом GET, тайм-аут 10 с) | Раз в неделю, ночью |
| Резервная копия базы (`sqlite3` API `backup`) | Раз в сутки, 03:30 МСК |
| Запрос баланса ProxyAPI | Каждые 30 мин, после каждых 10 запросов к GPT, каждые 5 мин при паузе `paused_no_funds` |

Поэтому uvicorn запускается строго с одним worker.

## 9а. Монитор баланса ProxyAPI

Модуль `services/balance.py`. Описание функции — `01-FUNCTIONS.md`, раздел 11.

**Запрос баланса:** `GET {PROXYAPI_BALANCE_URL}` с заголовком `Authorization: Bearer {PROXYAPI_KEY}`, тайм-аут 10 с. Ответ:

```json
{"balance": 1234.56}
{"balance": 37.66, "budget": {"limit": 50.0, "used": 12.34}}
```

Второй вариант — у ключа с бюджетом: `balance` — остаток бюджета ключа ([ProxyAPI, баланс](https://www.proxyapi.ru/docs/balance)). Каждый ответ записывается в `balance_log`.

**Ошибки запроса баланса:**

- 401 → уровень «ключ не принят», письмо преподавателю;
- 403 или иной отказ в доступе → флаг «запрос баланса не разрешён», монитор показывает подсказку; контроль идёт только по ответам 402;
- сеть или 5xx → значение не меняется, в мониторе «нет связи с ProxyAPI с <время>».

**Уровень:**

```
level = no_funds  если последний запрос к GPT получил 402 и баланс с тех пор не вырос
        no_funds  если balance < avg_cost_per_check * 1.5
        critical  если balance < settings.balance_critical   (по умолчанию 100)
        low       если balance < settings.balance_low        (по умолчанию 300)
        ok        иначе
```

`avg_cost_per_check` — среднее уменьшение баланса на одну проверку по последним 20 проверкам. Пока данных нет — значение из настроек (по умолчанию 5 ₽).

**`funds_ok()`** — вызывается перед каждым запросом к GPT. Возвращает `False` при уровне `no_funds`. Задача остаётся в очереди со статусом `paused_no_funds`.

**Выход из паузы:** при паузе баланс запрашивается каждые 5 минут. Как только уровень стал не ниже `critical`, задачи `paused_no_funds` возвращаются в `queued` по порядку создания. Преподаватель получает письмо «Баланс восстановлен, в очереди N работ».

**Расход и прогноз:**

- расход за период — сумма уменьшений баланса между соседними записями `balance_log`. Рост баланса считается пополнением и в расход не входит;
- прогноз в проверках — `balance / avg_cost_per_check`;
- прогноз в днях — `balance / средний расход в день за 7 дней`. Если данных меньше 3 дней, прогноз не показывается.

**Письма:** при переходе на уровень `low`, `critical`, `no_funds` и при восстановлении. Повтор того же уровня — не чаще раза в 12 часов (`balance_alerts`). Письмо уходит через тот же `mailer.py`. Тема — «hwstud: баланс ProxyAPI — <уровень>», в тексте баланс, прогноз, число работ в очереди, ссылка на монитор.

**Страница монитора** `/admin/settings/proxyapi`: данные из `balance_log` и `jobs`. График за 30 дней — встроенный SVG, который строит сервер в шаблоне, без библиотек. Кнопка «Пополнить баланс» — ссылка на `PROXYAPI_BILLING_URL` в новой вкладке.

## 10. Ограничения и лимиты в коде

| Что | Значение |
|---|---|
| Группы / ники в группе | 4 / 20 |
| Задания / попытки на задание | 4 / 4, новая попытка только после `revise` |
| Файл работы | PDF, JPG, PNG, TXT, DOCX; до 10 МБ; тип проверяется по содержимому, а не по расширению |
| Сообщение преподавателю | До 2000 знаков, не больше 10 от ника за календарные сутки по Москве |
| Страница без слайдов | До 300 КБ |
| Одновременных запросов к ProxyAPI | 2 |
| Длина ответа GPT (`max_completion_tokens`) | 1500, настраивается |
| Пороги баланса «мало» / «критично» | 300 ₽ / 100 ₽, настраиваются |

## 11. Файл `.env.example`

```
APP_BASE_URL=https://hwstud.ru
SESSION_SECRET=
ADMIN_PASSWORD_HASH=
PROXYAPI_BASE_URL=https://api.proxyapi.ru/v1
PROXYAPI_KEY=
PROXYAPI_MODEL=
PROXYAPI_BALANCE_URL=https://api.proxyapi.ru/proxyapi/balance
PROXYAPI_BILLING_URL=
SMTP_HOST=
SMTP_PORT=465
SMTP_SECURITY=ssl
SMTP_USER=
SMTP_PASSWORD=
MAIL_TO=
DATA_DIR=/opt/hwstud/data
TZ_DISPLAY=Europe/Moscow
```

Для `ADMIN_PASSWORD_HASH` в проекте есть команда `python -m app.tools.hash_password` — пароль вводится в консоли и не сохраняется в истории.
