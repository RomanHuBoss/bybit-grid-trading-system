from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List

import boto3
from botocore.exceptions import ClientError


logger = logging.getLogger(__name__)


@dataclass
class BackupConfig:
    bucket: str
    retention_days: int
    dsn: str
    backup_dir: Path
    prefix: str = "avi5_full"
    s3_base_prefix: str = "avi5/full"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def now(self) -> datetime:
        """Текущее время в UTC, зафиксированное для данного backup-запуска."""
        return self.created_at

    @property
    def timestamp(self) -> str:
        """Строка временной метки для имени файла."""
        return self.now.strftime("%Y-%m-%dT%H-%M-%SZ")

    @property
    def local_archive_path(self) -> Path:
        """Путь к локальному .tar архиву."""
        return self.backup_dir / f"{self.prefix}_{self.timestamp}.tar"

    @property
    def s3_key(self) -> str:
        """
        Ключ объекта в S3 по рекомендуемой структуре:
        s3://<bucket>/avi5/full/YYYY/MM/DD/avi5_full_<timestamp>.tar
        """
        now = self.now
        return (
            f"{self.s3_base_prefix}/"
            f"{now.year:04d}/{now.month:02d}/{now.day:02d}/"
            f"{self.prefix}_{self.timestamp}.tar"
        )


def run_pg_dump(dsn: str, output_path: Path) -> Path:
    """
    Запускает pg_dump и сохраняет дамп БД в указанный файл.

    :param dsn: Строка подключения к БД (DSN).
    :param output_path: Полный путь к файлу дампа (обычно .sql или .dump).
    :return: Путь к созданному файлу.
    :raises subprocess.CalledProcessError: если pg_dump завершился с ошибкой.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["pg_dump", dsn, "-f", str(output_path)]
    # Не логируем DSN целиком, чтобы не унести в логи пароль и прочие секреты.
    logger.info("Запуск pg_dump, файл дампа: %s", output_path)
    subprocess.run(cmd, check=True)
    return output_path


def create_tar_archive(source_file: Path, archive_path: Path) -> Path:
    """
    Упаковывает одиночный файл в .tar архив.

    :param source_file: Путь к исходному файлу (дамп БД).
    :param archive_path: Путь к создаваемому .tar архиву.
    :return: Путь к архиву.
    """
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Создание tar-архива %s из %s", archive_path, source_file)
    with tarfile.open(archive_path, mode="w") as tar:
        tar.add(source_file, arcname=source_file.name)
    return archive_path


def upload_to_s3(file_path: Path, bucket: str, key: str) -> None:
    """
    Загружает файл в S3.

    :param file_path: Путь к локальному файлу.
    :param bucket: Имя S3-бакета.
    :param key: Ключ объекта в S3.
    :raises ClientError: при ошибках S3.
    """
    logger.info("Загрузка %s в s3://%s/%s", file_path, bucket, key)
    s3_client = boto3.client("s3")
    s3_client.upload_file(str(file_path), bucket, key)


def cleanup_old_backups(
    bucket: str,
    retention_days: int,
    s3_base_prefix: str = "avi5/full",
    *,
    now: Optional[datetime] = None,
) -> None:
    """
    Удаляет старые backup’ы из S3 согласно политике хранения.

    Политика из спеки:
    - в S3 backup’ы хранятся retention_days (по умолчанию 90) дней;
    - удаляем объекты старше retention_days, не трогая «живую» цепочку WAL.

    :param bucket: Имя S3-бакета.
    :param retention_days: Количество дней хранения.
    :param s3_base_prefix: Базовый префикс в S3 для полных backup’ов.
    :param now: Текущее время (для тестов), по умолчанию — datetime.now(timezone.utc).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    threshold = now - timedelta(days=retention_days)
    logger.info(
        "Очистка backup’ов в s3://%s/%s старше %s",
        bucket,
        s3_base_prefix,
        threshold.isoformat(),
    )

    s3_client = boto3.client("s3")
    continuation_token: Optional[str] = None

    while True:
        list_kwargs: dict[str, object] = {
            "Bucket": bucket,
            "Prefix": f"{s3_base_prefix}/",
        }
        if continuation_token:
            list_kwargs["ContinuationToken"] = continuation_token

        response = s3_client.list_objects_v2(**list_kwargs)
        contents = response.get("Contents", []) or []

        objects_to_delete: list[dict[str, str]] = []
        for obj in contents:
            key = obj["Key"]
            last_modified = obj["LastModified"]  # datetime с TZ
            if last_modified < threshold:
                objects_to_delete.append({"Key": key})

        if objects_to_delete:
            logger.info(
                "Удаление %d старых объектов из бакета %s",
                len(objects_to_delete),
                bucket,
            )
            s3_client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": objects_to_delete, "Quiet": True},
            )

        if response.get("IsTruncated"):
            continuation_token = response.get("NextContinuationToken")
        else:
            break


def _cleanup_local_backups(
    backup_dir: Path,
    *,
    keep_days: int = 7,
    now: Optional[datetime] = None,
) -> None:
    """
    Удаляет локальные backup’ы старше указанного количества дней.

    Локальная политика из спеки: хранить ~7 дней для быстрого восстановления.

    :param backup_dir: Каталог, в котором хранятся архивы.
    :param keep_days: Сколько дней локальных backup’ов хранить.
    :param now: Текущее время (для тестов), по умолчанию — datetime.now(timezone.utc).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    threshold = now - timedelta(days=keep_days)
    if not backup_dir.exists():
        return

    for path in backup_dir.glob("*.tar"):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if mtime < threshold:
            logger.info("Удаление локального backup’а %s (mtime=%s)", path, mtime.isoformat())
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("Не удалось удалить %s: %s", path, exc)


def main(
    bucket: str,
    retention_days: int,
    *,
    dsn: str,
    backup_dir: Optional[Path] = None,
    prefix: str = "avi5_full",
    s3_base_prefix: str = "avi5/full",
) -> None:
    """
    Основная точка входа для резервного копирования БД.

    1. Запускает pg_dump.
    2. Упаковывает результат в .tar.
    3. Загружает архив в S3.
    4. Очищает старые backup’ы согласно политике хранения.

    :param bucket: Имя S3-бакета.
    :param retention_days: Количество дней хранения backup’ов в S3.
    :param dsn: Строка подключения к БД.
    :param backup_dir: Локальный каталог для архивов.
    :param prefix: Префикс имени файла.
    :param s3_base_prefix: Базовый префикс в S3.
    """
    if backup_dir is None:
        backup_dir = Path.cwd() / "backups"

    config = BackupConfig(
        bucket=bucket,
        retention_days=retention_days,
        dsn=dsn,
        backup_dir=backup_dir,
        prefix=prefix,
        s3_base_prefix=s3_base_prefix,
    )

    now = config.now
    dump_file = backup_dir / f"{prefix}_{config.timestamp}.sql"

    logger.info("Старт backup’а БД, bucket=%s, retention_days=%d", bucket, retention_days)

    # 1. Дамп БД
    run_pg_dump(config.dsn, dump_file)

    # 2. Архивация
    archive_path = create_tar_archive(dump_file, config.local_archive_path)

    # После успешного создания архива — можно удалить исходный файл дампа
    try:
        dump_file.unlink()
    except OSError as exc:
        logger.warning("Не удалось удалить временный дамп %s: %s", dump_file, exc)

    # 3. Загрузка в S3
    upload_to_s3(archive_path, config.bucket, config.s3_key)

    # 4. Очистка старых backup’ов в S3
    cleanup_old_backups(config.bucket, config.retention_days, config.s3_base_prefix, now=now)

    # 5. Очистка локального каталога backup’ов
    _cleanup_local_backups(backup_dir, keep_days=7, now=now)

    logger.info("Backup БД успешно завершён: %s", archive_path)


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """
    Разбор аргументов командной строки.

    :param argv: Список аргументов (для тестов). Если None — используется sys.argv.
    :return: Namespace с параметрами backup-процесса.
    """
    parser = argparse.ArgumentParser(
        description="Запуск резервного копирования PostgreSQL с загрузкой в S3."
    )
    parser.add_argument(
        "--bucket",
        required=False,
        help="Имя S3-бакета. По умолчанию — значение переменной окружения BACKUP_S3_BUCKET.",
    )
    parser.add_argument(
        "--dsn",
        required=False,
        help="Строка подключения к БД (DSN). По умолчанию — DATABASE_URL, DB_DSN или PG* переменные.",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=90,
        help="Сколько дней хранить backup’ы в S3 (по умолчанию 90).",
    )
    parser.add_argument(
        "--backup-dir",
        type=str,
        default=None,
        help="Локальный каталог для хранения архивов (по умолчанию ./backups).",
    )
    return parser.parse_args(argv)


def _resolve_dsn(cli_dsn: Optional[str]) -> str:
    """
    Определяет DSN для подключения к БД.

    Приоритет источников:
      1) Явный DSN из CLI (--dsn).
      2) DATABASE_URL (основной контракт приложения).
      3) DB_DSN (legacy-алиас).
      4) Набор стандартных PG-переменных (PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE).

    :param cli_dsn: DSN, переданный из командной строки.
    :return: Строка подключения.
    :raises ValueError: если DSN не удалось определить.
    """
    if cli_dsn:
        return cli_dsn

    env_dsn = os.getenv("DATABASE_URL") or os.getenv("DB_DSN")
    if env_dsn:
        return env_dsn

    # Попытка собрать DSN из стандартных PG-переменных.
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


def cli(argv: Optional[List[str]] = None) -> None:
    """
    CLI-обёртка вокруг main().

    Обрабатывает аргументы, окружение и возвращает осмысленные exit-коды
    для использования в cron/CI.
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

        dsn = _resolve_dsn(args.dsn)
        backup_dir = Path(args.backup_dir) if args.backup_dir else Path.cwd() / "backups"

        main(
            bucket=bucket,
            retention_days=args.retention_days,
            dsn=dsn,
            backup_dir=backup_dir,
        )
    except subprocess.CalledProcessError as exc:
        logger.error("pg_dump завершился с ошибкой: %s", exc)
        sys.exit(1)
    except ClientError as exc:
        logger.error("Ошибка при работе с S3: %s", exc)
        sys.exit(2)
    except Exception as exc:
        logger.error("Неожиданная ошибка backup-скрипта: %s", exc)
        sys.exit(3)


if __name__ == "__main__":
    cli()
