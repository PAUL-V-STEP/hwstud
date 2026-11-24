from __future__ import annotations

import secrets
from pathlib import Path

from ..config import PROJECT_ROOT, load_settings
from ..db import migrate


def main() -> None:
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        print(".env уже существует и не изменён")
    else:
        lines = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        values = []
        for line in lines:
            if line.startswith("APP_BASE_URL="):
                line = "APP_BASE_URL=http://127.0.0.1:8000"
            elif line.startswith("SESSION_SECRET="):
                line = "SESSION_SECRET=" + secrets.token_urlsafe(48)
            elif line.startswith("DATA_DIR="):
                line = "DATA_DIR=./data"
            values.append(line)
        env_path.write_text("\n".join(values) + "\n", encoding="utf-8")
        print("Создан локальный .env; секрет сессии сгенерирован, но не выводится")
    settings = load_settings(
        base_dir=PROJECT_ROOT,
        overrides={"APP_BASE_URL": "http://127.0.0.1:8000"},
    )
    settings.ensure_directories()
    applied = migrate(settings)
    print(f"База: {settings.db_path}")
    print(f"Новые миграции: {applied or 'нет'}")
    print("Далее: .venv\\Scripts\\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000")


if __name__ == "__main__":
    main()
