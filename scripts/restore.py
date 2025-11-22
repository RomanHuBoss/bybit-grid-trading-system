from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError


logger = logging.getLogger(__name__)


def download_from_s3(bucket: str, backup_key: str, local_path: Path) -> Path:
    """
    Скачивает файл backup’а из S3 в локальный путь.

    :param bucket: Имя S3-бакета.
    :param backup_key: Ключ объекта в S3 (например: avi5/full/YYYY/MM/DD/avi5_full_...tar).
    :param local_path: Локальный путь, куда будет сохранён файл.
    :return: Путь к локальному файлу.
    :raises ClientError: При ошибке обращения к S3.
    """
    local_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Скачивание backup’а из s3://%s/%s в %s", bucket, backup_key, local_path)

    s3_client = boto3.client("s3")
    s3_client.download_file(bucket, backup_key, str(local_path))

    return local_path


def _extract_if_tar(backup_path: Path) -> Path:
    """
    Если backup — .tar, распаковывает его и возвращает путь к вложенному дампу.

    Ожидается, что внутри один файл (результат pg_dump в custom-формате).
    Если файл не .tar, возвращает исходный путь.
    """
    if backup_path.suffix != ".tar":
        return backup_path

    logger.info("Обнаружен tar-архив backup’а, распаковка: %s", backup_path)
    extract_dir = backup_path.with_suffix("_extracted")
    extract_dir.mkdir(parents=True, exist_ok=True)

    with tarfile.open(backup_path, mode="r") as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        if not members:
            raise RuntimeError(f"Архив {backup_path} не содержит файлов для восстановления")
        # Берём первый файл как основной дамп.
        member = members[0]
        tar.extract(member, path=extract_dir)
        dump_path = extract_dir / member.name

    logger.info("Распакованный дамп для восстановления: %s", dump_path)
    return dump_path


def run_pg_restore(dsn: str, backup_path: Path) -> None:
    """
    Выполняет pg_restore (или эквивалентную процедуру) в целевую БД.

    :param dsn: Строка подключения к БД (DSN либо имя базы).
    :param backup_path: Путь к файлу backup’а (tar или собственно дамп).
    :raises FileNotFoundError: Если файла backup’а нет.
    :raises subprocess.CalledProcessError: Если pg_restore завершился с ошибкой.
    """
    if not backup_path.exists():
        raise FileNotFoundError(f"Файл backup’а не найден: {backup_path}")

    dump_path = _extract_if_tar(backup_path)

    logger.info("Запуск pg_restore для дампа %s", dump_path)
    cmd = [
        "pg_restore",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--dbname",
        dsn,
        str(dump_path),
    ]
    # Не логируем DSN целиком, чтобы не унести пароль/секреты в логи.
    safe_cmd = list(cmd)
    try:
        idx = safe_cmd.index("--dbname") + 1
        safe_cmd[idx] = "<DSN hidden>"
    except (ValueError, IndexError):
        # Если по какой-то причине структура команды изменилась,
        # логируем как есть (но без отдельной подстановки пароля).
        pass
    logger.info("Команда: %s", " ".join(safe_cmd))
    subprocess.run(cmd, check=True)


def main(bucket: str, backup_key: str, dsn: str) -> None:
    """
    Высокоуровневая процедура восстановления БД из backup’а.

    1. Скачивает backup-файл из S3.
    2. При необходимости распаковывает его.
    3. Выполняет pg_restore в целевую БД.

    :param bucket: Имя S3-бакета.
    :param backup_key: Ключ backup-файла в S3.
    :param dsn: Строка подключения к БД (DSN).
    :raises ClientError: Ошибка при скачивании из S3.
    :raises subprocess.CalledProcessError: Ошибка при выполнении pg_restore.
    """
    # Каталог для временных файлов можно переопределить через переменную окружения.
    base_dir = os.getenv("RESTORE_WORK_DIR", str(Path.cwd() / "restore_tmp"))
    work_dir = Path(base_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    # Немного санитайзим имя локального файла.
    safe_name = backup_key.replace("/", "_")
    local_backup_path = work_dir / safe_name

    downloaded = download_from_s3(bucket, backup_key, local_backup_path)
    run_pg_restore(dsn, downloaded)

    logger.info("Восстановление БД из backup’а успешно завершено.")


# ===== CLI-обёртка =====


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Восстановление PostgreSQL из backup’а в S3 (scripts/restore.py)."
    )
    parser.add_argument(
        "backup_key",
        help="Ключ backup-файла в S3 (например: avi5/full/YYYY/MM/DD/avi5_full_...tar).",
    )
    parser.add_argument(
        "--bucket",
        required=False,
        help="Имя S3-бакета. По умолчанию берётся из BACKUP_S3_BUCKET.",
    )
    parser.add_argument(
        "--dsn",
        required=False,
        help="DSN для подключения к целевой БД. "
             "Если не задан, используется DATABASE_URL, DB_DSN или PG* переменные.",
    )
    parser.add_argument(
        "--work-dir",
        required=False,
        help="Каталог для временных файлов восстановления "
             "(по умолчанию RESTORE_WORK_DIR или ./restore_tmp).",
    )
    return parser.parse_args(argv)


def _resolve_dsn(cli_dsn: Optional[str]) -> str:
    """
    Разрешает DSN по приоритету: CLI → DATABASE_URL/DB_DSN → PG* переменные.

    :param cli_dsn: DSN, переданный из командной строки.
    :return: Строка подключения.
    :raises ValueError: Если DSN не удалось определить.
    """
    if cli_dsn:
        return cli_dsn

    env_dsn = os.getenv("DATABASE_URL") or os.getenv("DB_DSN")
    if env_dsn:
        return env_dsn

    host = os.getenv("PGHOST")
    port = os.getenv("PGPORT", "5432")
    user = os.getenv("PGUSER")
    password = os.getenv("PGPASSWORD")
    dbname = os.getenv("PGDATABASE")

    if host and user and dbname:
        parts = [f"host={host}", f"port={port}", f"user={user}", f"dbname={dbname}"]
        if password:
            parts.append(f"password={password}")
        return " ".join(parts)

    raise ValueError("Не удалось определить DSN для подключения к БД.")


def cli(argv: Optional[list[str]] = None) -> None:
    """
    Точка входа при использовании как CLI-утилиты.

    Оборачивает main() и возвращает осмысленные коды выхода
    для интеграции со скриптами restore_db.sh, cron и CI.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    args = _parse_args(argv)

    try:
        bucket = args.bucket or os.getenv("BACKUP_S3_BUCKET")
        if not bucket:
            raise ValueError(
                "Не задан S3-бакет. Укажите --bucket или переменную окружения BACKUP_S3_BUCKET."
            )

        if args.work_dir:
            os.environ["RESTORE_WORK_DIR"] = args.work_dir

        dsn = _resolve_dsn(args.dsn)

        main(bucket=bucket, backup_key=args.backup_key, dsn=dsn)
    except ClientError as exc:
        logger.error("Ошибка при работе с S3: %s", exc)
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        logger.error("pg_restore завершился с ошибкой: %s", exc)
        sys.exit(2)
    except Exception as exc:
        logger.error("Неожиданная ошибка в процессе восстановления: %s", exc)
        sys.exit(3)


if __name__ == "__main__":
    cli()
