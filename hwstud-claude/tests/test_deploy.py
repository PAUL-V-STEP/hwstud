from __future__ import annotations

from pathlib import Path

from app.config import PROJECT_ROOT


def test_systemd_unit_has_required_limits_and_single_worker():
    unit = (PROJECT_ROOT / "deploy" / "hwstud.service").read_text(encoding="utf-8")
    assert "User=hwstud" in unit
    assert "EnvironmentFile=/opt/hwstud/.env" in unit
    assert "WorkingDirectory=/opt/hwstud/app" in unit
    assert "--workers 1" in unit
    assert "MemoryMax=1200M" in unit
    assert "Restart=always" in unit
    assert "ProtectSystem=full" in unit
    assert "--forwarded-allow-ips" not in unit


def test_caddy_preserves_limits_and_does_not_publish_data():
    caddy = (PROJECT_ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert "max_size 90MB" in caddy
    assert "reverse_proxy 127.0.0.1:8000" in caddy
    assert "root * /opt/hwstud/app" in caddy
    assert "encode zstd gzip" in caddy
    assert "Strict-Transport-Security" in caddy
    assert "/data" not in caddy
    assert "trusted_proxies" not in caddy
    assert caddy.count("{") == caddy.count("}")


def test_env_examples_contain_no_secret_values():
    for path in (PROJECT_ROOT / ".env.example", PROJECT_ROOT / "deploy" / "hwstud.env.example"):
        values = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
        for key in ("SESSION_SECRET", "ADMIN_PASSWORD_HASH", "PROXYAPI_KEY", "SMTP_PASSWORD"):
            assert values[key] == ""
        assert values["DATA_DIR"] == "/opt/hwstud/data"
        assert values["APP_BASE_URL"] == "https://hwstud.ru"


def test_deploy_covers_install_update_restore_and_limits():
    text = (PROJECT_ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    for required in (
        "systemctl stop hwstud",
        "PRAGMA integrity_check",
        "MemoryMax=1200M",
        "python -m app.tools.hash_password",
        "check_models --confirm-paid",
        "Работы студентов в этот архив не входят",
        "Не меняет DNS",
    ):
        assert required in text
    assert "cron" in text.lower()


def test_all_direct_requirements_are_pinned():
    lines = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    assert lines
    assert all("==" in line for line in lines if line and not line.startswith("#"))
