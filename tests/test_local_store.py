"""Atomic SQLite history, exact metrics, complete-day coverage and backups."""

from contextlib import closing
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest

from rex_local.model import SCHEMA, normalize_daily
from rex_local.store import Store, StoreError


DAY = date(2026, 10, 1)
PREVIOUS = date(2026, 9, 30)


def rows(day=DAY):
    return normalize_daily({"00123": [{
        "date": day.strftime("%Y%m%d"),
        "site": {"code": "0007", "name": "Unidade"},
        "horimeter": "12345678901234567890123456789.123456789",
        "operation": {"name": "Manutencao", "class": "maintenance"},
        "hours": {"off": "24", "stopped": "0", "total": "24"},
        "snapshots": 2**63 - 1, "offline": True,
    }]}, day, imported_at="2026-10-02T09:00:00Z", load_id="load-id")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "historico.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def test_rows_round_trip_exactly_and_follow_schema_order(self):
        expected = rows()
        expected[0]["operacao_nome"] = "Texto ' ; DROP TABLE rex_production; --"
        self.store.replace_day(DAY, expected)
        actual = list(self.store.iter_rows(DAY, DAY))
        self.assertEqual(actual, expected)
        self.assertEqual(tuple(actual[0]), tuple(name for name, _, _ in SCHEMA))
        self.assertEqual(self.store.count(DAY, DAY), 1)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as connection:
            numeric_type, bool_type = connection.execute(
                "SELECT typeof(horimetro_horas), typeof(offline) FROM rex_production"
            ).fetchone()
        self.assertEqual((numeric_type, bool_type), ("text", "integer"))

    def test_duplicates_retained_and_only_selected_day_replaced(self):
        self.store.replace_day(PREVIOUS, rows(PREVIOUS))
        duplicated = rows() * 2
        self.store.replace_day(DAY, duplicated)
        self.assertEqual(self.store.count(PREVIOUS, DAY), 3)
        self.assertEqual(list(self.store.iter_rows(DAY, DAY)), duplicated)
        replacement = rows()
        replacement[0]["horas_total"] = "12.123456789"
        self.store.replace_day(DAY, replacement)
        self.assertEqual(self.store.count(PREVIOUS, DAY), 2)
        self.assertEqual(list(self.store.iter_rows(PREVIOUS, PREVIOUS)), rows(PREVIOUS))
        self.assertEqual(list(self.store.iter_rows(DAY, DAY)), replacement)

    def test_all_rows_validated_before_any_delete_or_transaction(self):
        self.store.replace_day(DAY, rows())
        statements = []
        self.store._connection.set_trace_callback(statements.append)
        invalid = rows()[0]
        invalid["horas_total"] = "invalid-private-value"
        with self.assertRaises(StoreError) as caught:
            self.store.replace_day(DAY, [rows()[0], invalid])
        self.assertNotIn("invalid-private-value", str(caught.exception))
        self.assertFalse(any(statement.startswith("BEGIN") or statement.startswith("DELETE") for statement in statements))
        self.assertEqual(list(self.store.iter_rows(DAY, DAY)), rows())

    def test_missing_or_extra_fields_rejected_without_modifying_history(self):
        self.store.replace_day(DAY, rows())
        for variation in ("missing", "extra"):
            candidate = rows()
            if variation == "missing":
                candidate[0].pop("offline")
            else:
                candidate[0]["extra"] = "private-value"
            with self.subTest(variation=variation), self.assertRaises(StoreError):
                self.store.replace_day(DAY, candidate)
            self.assertEqual(list(self.store.iter_rows(DAY, DAY)), rows())

    def test_wrong_record_date_and_invalid_day_rejected(self):
        self.store.replace_day(DAY, rows())
        with self.assertRaises(StoreError):
            self.store.replace_day(DAY, rows(PREVIOUS))
        for wrong_day in (DAY.isoformat(), datetime(2026, 10, 1), None):
            with self.subTest(wrong_day=type(wrong_day).__name__), self.assertRaises(StoreError):
                self.store.replace_day(wrong_day, rows())
        self.assertEqual(list(self.store.iter_rows(DAY, DAY)), rows())

    def test_new_empty_day_tracks_coverage_and_overview(self):
        self.store.replace_day(PREVIOUS, [])
        self.store.replace_day(DAY, rows())
        self.assertEqual(self.store.collected_days(), {PREVIOUS, DAY})
        self.assertEqual(self.store.count(PREVIOUS, PREVIOUS), 0)
        overview = self.store.overview()
        self.assertEqual(overview["rows"], 1)
        self.assertEqual(overview["collected_days"], 2)
        self.assertEqual(overview["empty_days"], 1)
        self.assertEqual(overview["first_day"], PREVIOUS.isoformat())
        self.assertEqual(overview["last_day"], DAY.isoformat())
        self.assertGreater(overview["bytes"], 0)
        self.assertNotIn("00123", str(overview))
        self.assertNotIn("Manutencao", str(overview))

    def test_empty_response_cannot_erase_populated_day_without_flag(self):
        self.store.replace_day(PREVIOUS, rows(PREVIOUS))
        self.store.replace_day(DAY, rows())
        with self.assertRaises(StoreError):
            self.store.replace_day(DAY, [])
        self.assertEqual(list(self.store.iter_rows(DAY, DAY)), rows())
        self.store.replace_day(DAY, [], allow_empty=True)
        self.assertEqual(self.store.count(DAY, DAY), 0)
        self.assertEqual(self.store.count(PREVIOUS, PREVIOUS), 1)
        self.assertEqual(self.store.collected_days(), {PREVIOUS, DAY})
        self.assertEqual(self.store.overview()["empty_days"], 1)

    def test_insert_failure_rolls_back_deleted_rows_and_audit(self):
        self.store.replace_day(DAY, rows())
        with closing(sqlite3.connect(self.path, isolation_level=None)) as connection:
            connection.execute("CREATE TRIGGER test_fail BEFORE INSERT ON rex_production BEGIN SELECT RAISE(ABORT, 'failure'); END")
            audit_before = connection.execute("SELECT * FROM rex_load_audit").fetchall()
            collected_before = connection.execute("SELECT * FROM rex_collected_days").fetchall()
        candidate = rows()
        candidate[0]["horas_total"] = "8"
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.replace_day(DAY, candidate)
        self.assertEqual(list(self.store.iter_rows(DAY, DAY)), rows())
        with closing(sqlite3.connect(self.path, isolation_level=None)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM rex_load_audit").fetchall(), audit_before)
            self.assertEqual(connection.execute("SELECT * FROM rex_collected_days").fetchall(), collected_before)

    def test_audit_failure_also_rolls_back_business_rows_and_coverage(self):
        self.store.replace_day(DAY, rows())
        with closing(sqlite3.connect(self.path, isolation_level=None)) as connection:
            connection.execute("CREATE TRIGGER test_fail_audit BEFORE INSERT ON rex_load_audit BEGIN SELECT RAISE(ABORT, 'failure'); END")
        candidate = rows()
        candidate[0]["horas_total"] = "8"
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.replace_day(DAY, candidate)
        self.assertEqual(list(self.store.iter_rows(DAY, DAY)), rows())
        self.assertEqual(self.store.collected_days(), {DAY})

    def test_nullable_booleans_and_codes_survive_storage(self):
        candidates = []
        for offline in (None, False, True):
            item = rows()[0]
            item["offline"] = offline
            candidates.append(item)
        self.store.replace_day(DAY, candidates)
        actual = list(self.store.iter_rows(DAY, DAY))
        self.assertEqual(actual, candidates)
        self.assertIsNone(actual[0]["offline"])
        self.assertIs(actual[1]["offline"], False)
        self.assertIs(actual[2]["offline"], True)
        self.assertEqual(actual[0]["unidade_codigo"], "0007")

    def test_types_precision_required_fields_and_snapshot_boundaries_checked(self):
        mutations = (("horas_total", 0.1), ("horas_total", "1.1234567891"),
                     ("horas_total", "100000000000000000000000000000"), ("horas_total", "NaN"),
                     ("offline", 1), ("snapshots", True), ("snapshots", -1), ("snapshots", 2**63),
                     ("equipamento_chave", ""), ("equipamento_chave", None), ("operacao_nome", []),
                     ("carga_id", ""), ("importado_em", "2026-10-02T09:00:00"))
        for field, value in mutations:
            candidate = rows()
            candidate[0][field] = value
            with self.subTest(field=field, value=type(value).__name__), self.assertRaises(StoreError):
                self.store.replace_day(DAY, candidate)
        self.assertEqual(self.store.collected_days(), set())

    def test_inclusive_range_sorted_by_day_and_input_not_mutated(self):
        expected = rows()
        before = deepcopy(expected)
        self.store.replace_day(DAY, expected)
        self.store.replace_day(PREVIOUS, rows(PREVIOUS))
        self.assertEqual(expected, before)
        self.assertEqual([row["data"] for row in self.store.iter_rows(PREVIOUS, DAY)], [PREVIOUS.isoformat(), DAY.isoformat()])
        self.assertEqual(self.store.count(PREVIOUS, DAY), 2)
        for method in (self.store.count, lambda a, b: list(self.store.iter_rows(a, b))):
            with self.assertRaises(StoreError):
                method(DAY, PREVIOUS)

    def test_coverage_and_history_persist_when_reopened(self):
        self.store.replace_day(PREVIOUS, [])
        self.store.replace_day(DAY, rows())
        with Store(self.path) as reopened:
            self.assertEqual(reopened.collected_days(), {PREVIOUS, DAY})
            self.assertEqual(list(reopened.iter_rows(DAY, DAY)), rows())

    def test_each_replacement_preserves_load_audit_without_ttl(self):
        self.store.replace_day(DAY, rows())
        self.store.replace_day(DAY, rows())
        self.store.replace_day(PREVIOUS, [])
        with closing(sqlite3.connect(self.path, isolation_level=None)) as connection:
            audit = connection.execute("SELECT data, row_count FROM rex_load_audit ORDER BY rowid").fetchall()
        self.assertEqual(audit, [(DAY.isoformat(), 1), (DAY.isoformat(), 1), (PREVIOUS.isoformat(), 0)])
        self.assertEqual(self.store.overview()["collected_days"], 2)

    def test_backup_directory_creates_distinct_recoverable_integrity_checked_files(self):
        self.store.replace_day(PREVIOUS, [])
        self.store.replace_day(DAY, rows())
        directory = Path(self.directory.name) / "backups"
        first = self.store.backup(directory)
        second = self.store.backup(directory)
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, directory)
        self.assertTrue(first.name.startswith("rex-historico-"))
        with closing(sqlite3.connect(first, isolation_level=None)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.store.replace_day(DAY, [], allow_empty=True)
        with Store(first) as restored:
            self.assertEqual(restored.collected_days(), {PREVIOUS, DAY})
            self.assertEqual(list(restored.iter_rows(DAY, DAY)), rows())

    def test_explicit_backup_path_never_overwrites_existing_file_or_source(self):
        self.store.replace_day(DAY, rows())
        target = Path(self.directory.name) / "manual.sqlite3"
        self.assertEqual(self.store.backup(target), target)
        original_bytes = target.read_bytes()
        with self.assertRaises(StoreError):
            self.store.backup(target)
        self.assertEqual(target.read_bytes(), original_bytes)
        with self.assertRaises(StoreError):
            self.store.backup(self.path)
        self.assertEqual(list(self.store.iter_rows(DAY, DAY)), rows())

    def test_incompatible_existing_schema_is_rejected(self):
        incompatible = Path(self.directory.name) / "wrong.sqlite3"
        with closing(sqlite3.connect(incompatible, isolation_level=None)) as connection:
            connection.execute("CREATE TABLE rex_production(data TEXT)")
        with self.assertRaises(StoreError):
            Store(incompatible)


if __name__ == "__main__":
    unittest.main()
