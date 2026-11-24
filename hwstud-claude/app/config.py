from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta, timezone, tzinfo
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"Некорректная строка в {path}: {raw_line!r}")
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _as_bool(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def get_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Europe/Moscow":
            return timezone(timedelta(hours=3), name="Europe/Moscow")
        raise


@dataclass(frozen=True, slots=True)
class Settings:
    base_dir: Path
    data_dir: Path
    app_base_url: str
    session_secret: str
    admin_password_hash: str
    proxyapi_base_url: str
    proxyapi_balance_url: str
    proxyapi_key: str
    model_grading: str
    model_aux: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_user: str
    smtp_password: str
    mail_from: str
    mail_to: str
    tz_display: str
    enable_worker: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hwstud.db"

    @property
    def timezone(self) -> tzinfo:
        return get_timezone(self.tz_display)

    @property
    def cookie_secure(self) -> bool:
        return self.app_base_url.lower().startswith("https://")

    @property
    def smtp_configured(self) -> bool:
        return all(
            (
                self.smtp_host,
                self.smtp_user,
                self.smtp_password,
                self.mail_from,
                self.mail_to,
            )
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name in ("lectures", "uploads", "import", "backups", "archives", "tmp"):
            (self.data_dir / name).mkdir(exist_ok=True)


def load_settings(
    *,
    base_dir: Path | None = None,
    env_file: Path | None = None,
    overrides: Mapping[str, object] | None = None,
) -> Settings:
    root = (base_dir or PROJECT_ROOT).resolve()
    values: dict[str, object] = {
        "APP_BASE_URL": "http://127.0.0.1:8000",
        "SESSION_SECRET": "",
        "ADMIN_PASSWORD_HASH": "",
        "PROXYAPI_BASE_URL": "https://api.proxyapi.ru/v1",
        "PROXYAPI_BALANCE_URL": "https://api.proxyapi.ru/proxyapi/balance",
        "PROXYAPI_KEY": "",
        "MODEL_GRADING": "openai/gpt-6.1-terra",
        "MODEL_AUX": "openai/gpt-6-luna",
        "SMTP_HOST": "",
        "SMTP_PORT": 465,
        "SMTP_SECURITY": "ssl",
        "SMTP_USER": "",
        "SMTP_PASSWORD": "",
        "MAIL_FROM": "",
        "MAIL_TO": "",
        "DATA_DIR": str(root / "data"),
        "TZ_DISPLAY": "Europe/Moscow",
        "ENABLE_WORKER": True,
    }
    values.update(_read_env_file(env_file or root / ".env"))
    values.update(os.environ)
    if overrides:
        values.update(overrides)

    data_dir = Path(str(values["DATA_DIR"])).expanduser()
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    session_secret = str(values["SESSION_SECRET"])
    if not session_secret:
        raise RuntimeError(
            "SESSION_SECRET не задан. Для локального запуска выполните "
            "`python -m app.tools.init_local`."
        )
    smtp_security = str(values["SMTP_SECURITY"]).lower()
    if smtp_security not in {"ssl", "starttls"}:
        raise ValueError("SMTP_SECURITY должен быть ssl или starttls")
    smtp_port = int(values["SMTP_PORT"])
    if smtp_port == 25:
        raise ValueError("Порт SMTP 25 не поддерживается")

    return Settings(
        base_dir=root,
        data_dir=data_dir.resolve(),
        app_base_url=str(values["APP_BASE_URL"]).rstrip("/"),
        session_secret=session_secret,
        admin_password_hash=str(values["ADMIN_PASSWORD_HASH"]),
        proxyapi_base_url=str(values["PROXYAPI_BASE_URL"]).rstrip("/"),
        proxyapi_balance_url=str(values["PROXYAPI_BALANCE_URL"]),
        proxyapi_key=str(values["PROXYAPI_KEY"]),
        model_grading=str(values["MODEL_GRADING"]),
        model_aux=str(values["MODEL_AUX"]),
        smtp_host=str(values["SMTP_HOST"]),
        smtp_port=smtp_port,
        smtp_security=smtp_security,
        smtp_user=str(values["SMTP_USER"]),
        smtp_password=str(values["SMTP_PASSWORD"]),
        mail_from=str(values["MAIL_FROM"]),
        mail_to=str(values["MAIL_TO"]),
        tz_display=str(values["TZ_DISPLAY"]),
        enable_worker=_as_bool(values["ENABLE_WORKER"]),
    )
