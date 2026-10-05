"""Local full-history storage with atomic replacement of complete REX days.

No Google libraries or network access are used. NUMERIC values stay exact TEXT,
and collected-day metadata includes days that legitimately contain no records.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sqlite3
import uuid

from .model import SCHEMA


_COLUMNS = tuple(field[0] for field in SCHEMA)
_COLUMN_SET = frozenset(_COLUMNS)
_SQL_COLUMNS = ", ".join(f'"{name}"' for name in _COLUMNS)
_PLACEHOLDERS = ", ".join("?" for _ in _COLUMNS)
_SQL_TYPE = {"DATE": "TEXT", "STRING": "TEXT", "NUMERIC": "TEXT",
             "TIMESTAMP": "TEXT", "INT64": "INTEGER", "BOOL": "INTEGER"}
_DECIMAL_TEXT = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?\Z")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z"
)


class StoreError(ValueError):
    """A local operation was rejected without exposing a business record."""


def _fail(field):
    raise StoreError(f"Campo invalido no historico local: {field}.") from None


def _day(value):
    if not isinstance(value, date) or isinstance(value, datetime):
        raise StoreError("O dia deve ser datetime.date.")
    return value.isoformat()


def _range(start, end):
    begin, finish = _day(start), _day(end)
    if begin > finish:
        raise StoreError("A data inicial deve ser menor ou igual a final.")
    return begin, finish


def _numeric(value, field):
    if not isinstance(value, str) or len(value) > 256 or not _DECIMAL_TEXT.fullmatch(value):
        _fail(field)
    try:
        number = Decimal(value)
    except InvalidOperation:
        _fail(field)
    if not number.is_finite():
        _fail(field)
    if number:
        _, digits, exponent = number.as_tuple()
        while digits[-1] == 0:
            digits, exponent = digits[:-1], exponent + 1
        scale, integer_digits = max(-exponent, 0), max(len(digits) + exponent, 0)
        if scale > 9 or integer_digits > 29 or integer_digits + scale > 38:
            _fail(field)
    return value  # Preserve the exact normalized JSON decimal, not float.


def _encode_row(row, iso_day):
    if not isinstance(row, dict) or set(row) != _COLUMN_SET:
        raise StoreError("Registro com campos extras ou ausentes no historico local.")
    if row["data"] != iso_day:
        raise StoreError("Registro fora do dia solicitado; o historico foi preservado.")
    values = []
    for name, kind, mode in SCHEMA:
        value = row[name]
        if value is None:
            if mode == "REQUIRED":
                _fail(name)
            values.append(None)
            continue
        if kind == "NUMERIC":
            value = _numeric(value, name)
        elif kind == "INT64":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
                _fail(name)
        elif kind == "BOOL":
            if not isinstance(value, bool):
                _fail(name)
            value = int(value)
        else:
            if not isinstance(value, str):
                _fail(name)
            if mode == "REQUIRED" and not value.strip():
                _fail(name)
            if kind == "DATE":
                if not _ISO_DATE.fullmatch(value):
                    _fail(name)
                try:
                    date.fromisoformat(value)
                except ValueError:
                    _fail(name)
            elif kind == "TIMESTAMP":
                if not _TIMESTAMP.fullmatch(value):
                    _fail(name)
                try:
                    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    _fail(name)
                if timestamp.utcoffset() is None:
                    _fail(name)
        values.append(value)
    return tuple(values)


class Store:
    """SQLite history for all collected days, with no automatic expiration.

    ``replace_day`` retains duplicate consolidated records. A newly collected
    empty day is audited; replacing a populated day with an empty response needs
    explicit ``allow_empty=True``. ``backup`` accepts a directory (timestamped
    filename) or a filename with an extension and never overwrites an old file.
    """

    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        try:
            definitions = ", ".join(
                f'"{name}" {_SQL_TYPE[kind]}' + (" NOT NULL" if mode == "REQUIRED" else "")
                for name, kind, mode in SCHEMA
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS rex_production "
                f"(row_id INTEGER PRIMARY KEY, {definitions})"
            )
            actual = tuple((row["name"], row["type"], row["notnull"])
                           for row in self._connection.execute("PRAGMA table_info(rex_production)"))
            expected = (("row_id", "INTEGER", 0),) + tuple(
                (name, _SQL_TYPE[kind], int(mode == "REQUIRED")) for name, kind, mode in SCHEMA
            )
            if actual != expected:
                raise StoreError("O schema do historico local difere do modelo REX atual.")
            self._connection.execute("CREATE INDEX IF NOT EXISTS rex_production_day ON rex_production(data)")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS rex_collected_days ("
                "data TEXT PRIMARY KEY, row_count INTEGER NOT NULL, "
                "collected_at TEXT NOT NULL, audit_id TEXT NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS rex_load_audit ("
                "audit_id TEXT PRIMARY KEY, data TEXT NOT NULL, row_count INTEGER NOT NULL, "
                "collected_at TEXT NOT NULL)"
            )
        except Exception:
            self._connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def close(self):
        self._connection.close()

    def replace_day(self, day, rows, *, allow_empty=False):
        """Validate every row, then atomically replace only this complete day."""
        iso_day = _day(day)
        if not isinstance(rows, (list, tuple)):
            raise StoreError("Os registros devem ser uma lista de linhas normalizadas.")
        if not isinstance(allow_empty, bool):
            raise StoreError("allow_empty deve ser booleano.")
        encoded = [_encode_row(row, iso_day) for row in rows]
        audit_id = uuid.uuid4().hex
        collected_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            previous = self._connection.execute(
                "SELECT COUNT(*) FROM rex_production WHERE data = ?", (iso_day,)
            ).fetchone()[0]
            if not encoded and previous and not allow_empty:
                raise StoreError("Dia vazio substituiria registros existentes; use allow_empty=True somente apos conferir.")
            self._connection.execute("DELETE FROM rex_production WHERE data = ?", (iso_day,))
            self._connection.executemany(
                f"INSERT INTO rex_production ({_SQL_COLUMNS}) VALUES ({_PLACEHOLDERS})", encoded
            )
            self._connection.execute(
                "INSERT INTO rex_collected_days(data, row_count, collected_at, audit_id) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(data) DO UPDATE SET row_count=excluded.row_count, "
                "collected_at=excluded.collected_at, audit_id=excluded.audit_id",
                (iso_day, len(encoded), collected_at, audit_id),
            )
            self._connection.execute(
                "INSERT INTO rex_load_audit(audit_id, data, row_count, collected_at) VALUES (?, ?, ?, ?)",
                (audit_id, iso_day, len(encoded), collected_at),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def collected_days(self):
        return {date.fromisoformat(row[0]) for row in self._connection.execute("SELECT data FROM rex_collected_days")}

    def count(self, start, end):
        begin, finish = _range(start, end)
        return self._connection.execute(
            "SELECT COUNT(*) FROM rex_production WHERE data BETWEEN ? AND ?", (begin, finish)
        ).fetchone()[0]

    def iter_rows(self, start, end):
        begin, finish = _range(start, end)
        cursor = self._connection.execute(
            f"SELECT {_SQL_COLUMNS} FROM rex_production WHERE data BETWEEN ? AND ? ORDER BY data, row_id",
            (begin, finish),
        )
        for stored in cursor:
            row = {name: stored[name] for name in _COLUMNS}
            if row["offline"] is not None:
                row["offline"] = bool(row["offline"])
            yield row

    def overview(self):
        """Counts and coverage dates only; no equipment/operator/operation data."""
        days = self._connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(row_count=0), 0), MIN(data), MAX(data) FROM rex_collected_days"
        ).fetchone()
        return {
            "path": str(self.path),
            "rows": self._connection.execute("SELECT COUNT(*) FROM rex_production").fetchone()[0],
            "collected_days": days[0], "empty_days": days[1],
            "first_day": days[2], "last_day": days[3],
            "bytes": self.path.stat().st_size,
        }

    def backup(self, destination):
        """Create an independent consistent backup; refuse all overwrites."""
        target = Path(destination).expanduser().resolve()
        if target.is_dir() or (not target.exists() and not target.suffix):
            target.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            target = target / f"rex-historico-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb"):
                pass
        except FileExistsError:
            raise StoreError("O arquivo de backup ja existe; escolha outro nome.") from None
        backup_connection = None
        try:
            backup_connection = sqlite3.connect(target)
            self._connection.backup(backup_connection)
            if backup_connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise StoreError("O backup nao passou na verificacao de integridade.")
        except Exception:
            if backup_connection is not None:
                backup_connection.close()
                backup_connection = None
            target.unlink()
            raise
        finally:
            if backup_connection is not None:
                backup_connection.close()
        return target
