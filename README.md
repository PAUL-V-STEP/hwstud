# hwstud

Сайт учебного курса hwstud.ru: лекции, глоссарий, литература, домашние задания с проверкой ИИ через ProxyAPI, объявления, переписка и админка преподавателя.

Основное приложение находится в каталоге [`hwstud-claude/`](hwstud-claude/). Репозиторий предназначен для приватного хранения: материалы курса, рабочие данные и секреты не должны публиковаться открыто.

## Локальный запуск на Windows

Нужен Python 3.12.

```powershell
cd hwstud-claude
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m app.tools.init_local
.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Открыть: <http://127.0.0.1:8000>.

Файл `.env` и каталог `data/` не входят в репозиторий. `init_local` создаёт локальную конфигурацию при первом запуске и применяет миграции.

## Проверка

```powershell
cd hwstud-claude
.venv\Scripts\python.exe -m pytest
```

Тесты используют заглушки ProxyAPI и SMTP и не выполняют внешние сетевые запросы.

## Документация

- [README проекта](hwstud-claude/README.md)
- [Требования](hwstud-claude/REQUIREMENTS.md)
- [Архитектура](hwstud-claude/docs/03-ARCHITECTURE.md)
- [Установка на REG.RU](hwstud-claude/DEPLOY.md)
- [Инструкция REG.RU в DOCX](hwstud-claude/REG-RU-INSTALL.docx)
- [Результаты проверок](hwstud-claude/TESTING.md)

## Рекламные материалы

Буклеты «ИИ Методист» лежат в каталоге [`marketing/`](marketing/):

- [Буклет на 1 страницу, крупный шрифт](marketing/hwstud-buklet-na-1-stranitsu-krupnyi.pdf)
- [Рекламный буклет на 1 странице](marketing/hwstud-reklamnyi-buklet-1-str.pdf)
- [Рекламный буклет на 2 страницах](marketing/hwstud-reklamnyi-buklet-2-str.pdf)
