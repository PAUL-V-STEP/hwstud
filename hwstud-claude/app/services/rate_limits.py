from __future__ import annotations

LOGIN_NICK_LIMIT = 10
LOGIN_NICK_WINDOW = 600
LOGIN_IP_LIMIT = 300
LOGIN_IP_WINDOW = 3600
REGISTER_IP_LIMIT = 60
REGISTER_IP_WINDOW = 3600
NICK_FORMAT_IP_LIMIT = 50
NICK_FORMAT_IP_WINDOW = 600
CLASS_CODE_NICK_LIMIT = 5
CLASS_CODE_NICK_WINDOW = 600
CLASS_CODE_IP_LIMIT = 100
CLASS_CODE_IP_WINDOW = 600


def login_nick_key(ip: str, nick_norm: str) -> str:
    return f"login:{ip}:{nick_norm}"


def check_login_attempt(limiter, ip: str, nick_norm: str | None) -> tuple[str | None, str | None]:
    pair_key = login_nick_key(ip, nick_norm) if nick_norm else None
    checks = [("login_ip:" + ip, LOGIN_IP_LIMIT, LOGIN_IP_WINDOW,
               "Слишком много попыток с вашей сети. Повторите позже")]
    if pair_key:
        checks.append((
            pair_key, LOGIN_NICK_LIMIT, LOGIN_NICK_WINDOW,
            "Слишком много попыток для этого ника. Повторите через 10 минут",
        ))
    else:
        checks.append((
            "nick_format:" + ip, NICK_FORMAT_IP_LIMIT, NICK_FORMAT_IP_WINDOW,
            "Слишком много попыток. Повторите позже",
        ))
    for key, limit, window, error in checks:
        if not limiter.allowed(key, limit, window):
            return error, pair_key
    for key, _limit, window, _error in checks:
        limiter.register(key, window)
    return None, pair_key


def reset_login_nick(limiter, pair_key: str | None) -> None:
    if pair_key:
        limiter.reset(pair_key)


def check_registration(limiter, ip: str) -> str | None:
    key = "register_ip:" + ip
    if not limiter.allowed(key, REGISTER_IP_LIMIT, REGISTER_IP_WINDOW):
        return "Слишком много регистраций с вашей сети. Обратитесь к преподавателю"
    return None


def record_registration(limiter, ip: str) -> None:
    limiter.register("register_ip:" + ip, REGISTER_IP_WINDOW)
