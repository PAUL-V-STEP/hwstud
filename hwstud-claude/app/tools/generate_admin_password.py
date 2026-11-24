from __future__ import annotations

import secrets
import string

from ..auth import hash_password, verify_password


def generate_password(length: int = 20) -> str:
    """Генерирует пароль без вывода или записи его куда-либо самой функцией."""
    if length < 12:
        raise ValueError("Минимальная длина пароля — 12 символов")
    alphabet = string.ascii_letters + string.digits + "!@#%+-_="
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(char.islower() for char in password)
            and any(char.isupper() for char in password)
            and any(char.isdigit() for char in password)
            and any(not char.isalnum() for char in password)
        ):
            return password


def main() -> None:
    password = generate_password()
    encoded = hash_password(password)
    print("Локальный генератор пароля преподавателя")
    print("Не закрывайте окно, пока не сохранили обе строки.")
    print()
    print("Пароль для входа (сохраните его в менеджере паролей):")
    print(password)
    print()
    print("Скопируйте в .env следующую строку:")
    print(f"ADMIN_PASSWORD_HASH={encoded}")
    print()
    print("Пароль нигде не записан. После закрытия окна восстановить его нельзя.")
    input("После сохранения нажмите Enter, чтобы закрыть окно...")


if __name__ == "__main__":
    main()
