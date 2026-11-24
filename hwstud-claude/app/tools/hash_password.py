from __future__ import annotations

import getpass

from ..auth import hash_password


def main() -> None:
    password = getpass.getpass("Пароль преподавателя: ")
    confirmation = getpass.getpass("Повторите пароль: ")
    if password != confirmation:
        raise SystemExit("Пароли не совпадают")
    print("\nДобавьте строку в .env (пароль в командную строку не попадает):")
    print(f"ADMIN_PASSWORD_HASH={hash_password(password)}")


if __name__ == "__main__":
    main()
