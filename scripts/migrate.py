from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, List

from alembic.config import Config
from alembic.command import upgrade
from alembic.util.exc import CommandError

# Корень проекта: предполагаем структуру вида
# repo_root/
#   alembic.ini
#   scripts/migrate.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def setup_alembic_config(ini_path: Optional[str | Path] = None) -> Config:
    """
    Создаёт и настраивает Alembic Config для запуска миграций.

    :param ini_path: Путь до alembic.ini. Если не указан, берётся файл в корне проекта.
    :raises FileNotFoundError: если ini-файл не найден.
    :return: Экземпляр Alembic Config.
    """
    if ini_path is None:
        ini_path = DEFAULT_ALEMBIC_INI

    ini_path = Path(ini_path)
    if not ini_path.is_file():
        raise FileNotFoundError(f"Не найден файл alembic.ini по пути: {ini_path}")

    config = Config(str(ini_path))

    # Дополнительно прокидываем путь до корня проекта — может использоваться в env.py / alembic.ini
    config.set_main_option("project_root", str(PROJECT_ROOT))

    return config


def main(revision: str = "head") -> None:
    """
    Запускает alembic upgrade до указанной ревизии.

    :param revision: Целевая ревизия Alembic (по умолчанию 'head').
    :raises FileNotFoundError: если alembic.ini не найден.
    :raises CommandError: если Alembic вернул ошибку при выполнении миграций.
    """
    config = setup_alembic_config()
    upgrade(config, revision)


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """
    Разбор аргументов командной строки.

    :param argv: Список аргументов (для тестов). Если None — берётся sys.argv.
    :return: Namespace с полями revision и ini_path.
    """
    parser = argparse.ArgumentParser(
        description="CLI-обёртка для запуска Alembic миграций (upgrade)."
    )
    parser.add_argument(
        "revision",
        nargs="?",
        default="head",
        help="Ревизия Alembic для команды upgrade (по умолчанию 'head').",
    )
    parser.add_argument(
        "--ini",
        dest="ini_path",
        help="Путь до alembic.ini (по умолчанию — файл в корне проекта).",
    )
    return parser.parse_args(argv)


def cli(argv: Optional[List[str]] = None) -> None:
    """
    Точка входа для командной строки.

    Обработка ошибок переведена в exit-коды, чтобы скрипт можно было
    использовать в CI/CD и bash-скриптах.
    """
    args = _parse_args(argv)

    ini_path: Optional[Path] = None
    if args.ini_path:
        ini_path = Path(args.ini_path)

    try:
        # Если пользователь указал свой ini — подменяем поведение setup_alembic_config.
        if ini_path is not None:
            config = setup_alembic_config(ini_path)
            upgrade(config, args.revision)
        else:
            main(revision=args.revision)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except CommandError as exc:
        print(f"Ошибка Alembic при выполнении миграций: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()
