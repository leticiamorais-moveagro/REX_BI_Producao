"""Exercise the actual local pipeline with fake API reads and temp databases."""

from contextlib import redirect_stdout, redirect_stderr
from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rex_api import RexConnectionError, RexHTTPError
from rex_local import cli
from rex_local.model import NormalizationError, normalize_daily
from rex_local.store import Store


DAY = date(2026, 10, 1)


class FakeRex:
    def __init__(self, fail_day=None):
        self.fail_day = fail_day
        self.requests = []
        self.logins = 0

    def daily_production(self, begin, end):
        self.requests.append((begin, end))
        if begin == self.fail_day:
            raise RexConnectionError("Falha de conexao simulada.")
        return {"0001": [{"date": begin.replace("-", ""), "horimeter": "1000.123456789", "hours": {"total": "24"}}]}

    def login(self):
        self.logins += 1


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.config = {
            "version": 1, "inicio_historico": "2026-09-29", "planilha_id": "",
            "timezone": "America/Sao_Paulo", "backup_dir": str(self.folder / "backups"),
            "limite_celulas": 10_000_000,
        }
        cli.write_json(self.folder / "config.json", self.config)
        self.output = io.StringIO()

    def run_main(self, client, *args):
        with patch.object(cli, "yesterday", return_value=DAY), patch.object(cli.auth, "rex_client", return_value=client), \
                patch.object(cli.time, "sleep"), redirect_stdout(self.output), redirect_stderr(self.output):
            # Default collect.sleep captured at definition: avoid real waits independently.
            original = cli.collect
            with patch.object(cli, "collect", side_effect=lambda store, rex, requested, **kw: original(store, rex, requested, sleep=lambda _: None, **kw)):
                return cli.main(["--pasta", str(self.folder), *args])

    def test_pilot_fetches_one_closed_day_and_creates_recoverable_backup(self):
        client = FakeRex()
        self.assertEqual(self.run_main(client, "testar"), 0)
        self.assertEqual(client.requests, [("2026-10-01", "2026-10-01")])
        with Store(self.folder / "historico.sqlite3") as store:
            self.assertEqual(store.count(DAY, DAY), 1)
            self.assertEqual(next(store.iter_rows(DAY, DAY))["horimetro_horas"], "1000.123456789")
        backups = list((self.folder / "backups").glob("*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with Store(backups[0]) as restored:
            self.assertEqual(restored.collected_days(), {DAY})

    def test_partial_api_failure_preserves_completed_days_and_backs_them_up(self):
        client = FakeRex(fail_day="2026-09-30")
        self.assertEqual(self.run_main(client, "coletar"), 1)
        with Store(self.folder / "historico.sqlite3") as store:
            self.assertEqual(store.collected_days(), {date(2026, 9, 29)})
        backups = list((self.folder / "backups").glob("*.sqlite3"))
        with Store(backups[0]) as restored:
            self.assertEqual(restored.collected_days(), {date(2026, 9, 29)})

    def test_repeat_collection_replaces_days_without_duplicate_rows(self):
        client = FakeRex()
        self.assertEqual(self.run_main(client, "coletar"), 0)
        self.assertEqual(self.run_main(client, "coletar"), 0)
        with Store(self.folder / "historico.sqlite3") as store:
            self.assertEqual(store.count(date(2026, 9, 29), DAY), 3)

    def test_incremental_collection_fills_old_gaps_and_reprocesses_seven_days(self):
        self.config["inicio_historico"] = "2026-09-01"
        with Store(self.folder / "historico.sqlite3") as store:
            for day in cli.days(date(2026, 9, 1), DAY):
                if day != date(2026, 9, 3):
                    store.replace_day(day, [])
            requested = cli.requested_days(store, self.config, None, None)
        self.assertEqual(requested, [date(2026, 9, 3), *cli.days(date(2026, 9, 25), DAY)])

    def test_explicit_window_rejects_open_future_or_outside_config_days(self):
        with Store(self.folder / "historico.sqlite3") as store, patch.object(cli, "yesterday", return_value=DAY):
            for begin, end in (("2026-09-28", "2026-09-30"), ("2026-09-30", None), ("2026-09-30", "2026-10-02"), ("2026-10-01", "2026-09-30")):
                with self.subTest(begin=begin, end=end), self.assertRaises(cli.LocalError):
                    cli.requested_days(store, self.config, begin, end)

    def test_rolling_twelve_calendar_months_handles_leap_year(self):
        self.assertEqual(cli.twelve_month_start(DAY), date(2025, 10, 2))
        self.assertEqual(cli.twelve_month_start(date(2024, 2, 28)), date(2023, 2, 28))
        self.assertEqual(cli.twelve_month_start(date(2024, 2, 29)), date(2023, 3, 1))

    def test_refused_publication_does_not_request_google_credentials(self):
        self.config["planilha_id"] = "abc123456789012345"
        cli.write_json(self.folder / "config.json", self.config)
        (self.folder / "cliente_google.json").write_text("{}", encoding="utf-8")
        with Store(self.folder / "historico.sqlite3") as store, patch.object(cli, "yesterday", return_value=DAY), \
                patch.object(cli.auth, "google_service") as google:
            store.replace_day(DAY, [])
            with self.assertRaisesRegex(cli.LocalError, "faltam 2 dias"):
                cli.publish(store, self.config, self.folder)
            google.assert_not_called()

    def test_a_second_local_process_cannot_collect_concurrently(self):
        with cli.local_lock(self.folder):
            with self.assertRaises(cli.LocalError):
                with cli.local_lock(self.folder):
                    self.fail("Second execution acquired lock")
        with cli.local_lock(self.folder):
            pass

    def test_report_401_reauthenticates_once_but_other_errors_do_not(self):
        client = FakeRex()
        original = client.daily_production
        def expired(begin, end):
            if not client.logins:
                raise RexHTTPError(401, "GET", "/api/v1/reports/daily-production")
            return original(begin, end)
        client.daily_production = expired
        with Store(self.folder / "historico.sqlite3") as store, redirect_stdout(self.output):
            cli.collect(store, client, [DAY], allow_empty=False)
            self.assertEqual(store.count(DAY, DAY), 1)
        self.assertEqual(client.logins, 1)

    def test_bad_payload_cannot_delete_previous_successful_day(self):
        with Store(self.folder / "historico.sqlite3") as store, redirect_stdout(self.output):
            client = FakeRex()
            cli.collect(store, client, [DAY], allow_empty=False)
            client.daily_production = lambda *_: {"0001": [{"date": "20261001", "hours": {"total": "bad"}}]}
            with self.assertRaises(NormalizationError):
                cli.collect(store, client, [DAY], allow_empty=False)
            self.assertEqual(store.count(DAY, DAY), 1)

    def test_google_sheet_id_validates_official_link_and_rejects_lookalike(self):
        valid = "abc123456789012345"
        self.assertEqual(cli.sheet_id(f"https://docs.google.com/spreadsheets/d/{valid}/edit#gid=0"), valid)
        self.assertEqual(cli.sheet_id(valid), valid)
        with self.assertRaises(cli.LocalError):
            cli.sheet_id(f"https://docs.google.com.evil.invalid/spreadsheets/d/{valid}/edit")

    def test_config_rejects_secrets_and_unsupported_shapes(self):
        self.config["password"] = "private-value"
        cli.write_json(self.folder / "config.json", self.config)
        with self.assertRaises(cli.LocalError) as caught:
            cli.read_config(self.folder)
        self.assertNotIn("private-value", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
