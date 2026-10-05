"""Offline tests of publication transactions, ownership and data fidelity."""

from copy import deepcopy
from datetime import date, timedelta
import json
import re
import unittest
from unittest.mock import patch

from rex_local.model import SCHEMA
from rex_local.sheets import (CONTROL_ROLE, CONTROL_TITLE, HEADERS, LIVE_ROLE,
                              LIVE_TITLE, PUBLISH_KEY, ROLE_KEY, Publisher, SheetsError)


DAY = date(2026, 10, 1)


def record(day=DAY, **changes):
    values = {name: None for name, _kind, _mode in SCHEMA}
    values.update(data=day.isoformat(), equipamento_chave="000123",
                  importado_em="2026-10-02T10:00:00+00:00", carga_id="carga-local")
    values.update(changes)
    return values


class FakeStore:
    def __init__(self, rows, days=None, claimed_count=None):
        self.rows = rows
        self.days = {DAY} if days is None else days
        self.claimed_count = claimed_count

    def collected_days(self):
        return self.days

    def count(self, start, end):
        return len(self.rows) if self.claimed_count is None else self.claimed_count

    def iter_rows(self, start, end):
        yield from self.rows


class Request:
    def __init__(self, action):
        self.action = action

    def execute(self):
        return self.action()


class FakeGoogle:
    """Minimal API double with atomic batch execution and persisted metadata."""

    def __init__(self):
        self.sheets_state = [{"properties": {"sheetId": 0, "title": "Página1",
                              "sheetType": "GRID", "gridProperties": {
                                  "rowCount": 1000, "columnCount": 26}},
                              "developerMetadata": [], "cells": []}]
        self.calls = []
        self.uploads = []
        self.final_batches = []
        self.fail_upload = False
        self.fail_commit = False
        self.lost_response = False
        self.fail_cleanup = False
        self.metadata_id = 1

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kwargs):
        def action():
            self.calls.append(("get", kwargs))
            if "range" not in kwargs:
                return {"spreadsheetId": "sheet-id", "sheets": deepcopy(self.sheets_state)}
            title = kwargs["range"].split("'!")[0][1:]
            sheet = self.by_title(title)
            first = sheet["cells"][:1]
            return {"values": deepcopy(first)}
        return Request(action)

    def update(self, **kwargs):
        def action():
            self.calls.append(("update", kwargs))
            if self.fail_upload:
                raise OSError("secret-password-from-untrusted-error")
            self.uploads.append(deepcopy(kwargs))
            match = re.fullmatch(r"'([^']+)'!A([0-9]+):AE([0-9]+)", kwargs["range"])
            if not match:
                raise AssertionError("bad upload range")
            sheet = self.by_title(match.group(1))
            first = int(match.group(2)) - 1
            for index, values in enumerate(kwargs["body"]["values"], first):
                self._put(sheet, index, values)
            return {"updatedRows": len(kwargs["body"]["values"])}
        return Request(action)

    def batchUpdate(self, **kwargs):
        def action():
            requests = kwargs["body"]["requests"]
            self.calls.append(("batch", deepcopy(kwargs)))
            final = any("copyPaste" in request for request in requests)
            if final:
                self.final_batches.append(deepcopy(requests))
                if self.fail_commit:
                    raise ValueError("secret-password-from-untrusted-error")
            if len(requests) == 1 and "deleteSheet" in requests[0] and self.fail_cleanup:
                raise ValueError("cleanup denied")
            candidate = deepcopy(self.sheets_state)
            replies = []
            for request in requests:
                replies.append(self._apply(candidate, request))
            self.sheets_state = candidate
            if final and self.lost_response:
                self.lost_response = False
                raise TimeoutError("reply was lost after applying everything")
            return {"replies": replies}
        return Request(action)

    def by_title(self, title):
        return next(sheet for sheet in self.sheets_state if sheet["properties"]["title"] == title)

    def _by_id(self, sheets, sheet_id):
        return next(sheet for sheet in sheets if sheet["properties"]["sheetId"] == sheet_id)

    @staticmethod
    def _put(sheet, row, values):
        while len(sheet["cells"]) <= row:
            sheet["cells"].append([])
        sheet["cells"][row] = deepcopy(values)

    def _apply(self, sheets, request):
        key, value = next(iter(request.items()))
        if key == "addSheet":
            props = deepcopy(value["properties"])
            props.setdefault("sheetType", "GRID")
            if any(sheet["properties"]["sheetId"] == props["sheetId"]
                   or sheet["properties"]["title"] == props["title"] for sheet in sheets):
                raise ValueError("already exists")
            sheets.append({"properties": props, "developerMetadata": [], "cells": []})
            return {"addSheet": {"properties": props}}
        if key == "deleteSheet":
            target = self._by_id(sheets, value["sheetId"])
            sheets.remove(target)
        elif key == "createDeveloperMetadata":
            metadata = deepcopy(value["developerMetadata"])
            metadata["metadataId"] = self.metadata_id
            self.metadata_id += 1
            sheet = self._by_id(sheets, metadata["location"]["sheetId"])
            sheet["developerMetadata"].append(metadata)
        elif key == "updateDeveloperMetadata":
            metadata_id = value["dataFilters"][0]["developerMetadataLookup"]["metadataId"]
            metadata = next(item for sheet in sheets for item in sheet["developerMetadata"]
                            if item["metadataId"] == metadata_id)
            metadata.update(value["developerMetadata"])
        elif key == "updateSheetProperties":
            sheet = self._by_id(sheets, value["properties"]["sheetId"])
            grid = value["properties"]["gridProperties"]
            sheet["properties"]["gridProperties"].update(grid)
            if "rowCount" in grid:
                del sheet["cells"][grid["rowCount"]:]
        elif key == "updateCells":
            grid = value["range"]
            sheet = self._by_id(sheets, grid["sheetId"])
            if grid["endRowIndex"] > sheet["properties"]["gridProperties"]["rowCount"]:
                raise ValueError("out of bounds")
            first = grid["startRowIndex"]
            for row_index in range(first, grid["endRowIndex"]):
                relative = row_index - first
                rows = value.get("rows", [])
                cells = rows[relative].get("values", []) if relative < len(rows) else []
                values = [next(iter(cell.get("userEnteredValue", {"empty": ""}).values()))
                          for cell in cells]
                self._put(sheet, row_index, values)
        elif key == "copyPaste":
            source, destination = value["source"], value["destination"]
            if ((source["endRowIndex"] - source["startRowIndex"])
                    != (destination["endRowIndex"] - destination["startRowIndex"])):
                raise ValueError("would repeat source rows")
            original = self._by_id(sheets, source["sheetId"])
            target = self._by_id(sheets, destination["sheetId"])
            for i in range(source["startRowIndex"], source["endRowIndex"]):
                self._put(target, destination["startRowIndex"] + i, original["cells"][i])
        elif key == "repeatCell":
            sheet = self._by_id(sheets, value["range"]["sheetId"])
            sheet.setdefault("formats", {})[value["range"]["startColumnIndex"]] = value["cell"]["userEnteredFormat"]["numberFormat"]
        else:
            raise AssertionError("unsupported request " + key)
        return {}


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.google = FakeGoogle()
        self.clock_value = 0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock_value += seconds

    def publisher(self, **kwargs):
        return Publisher(self.google, "sheet-id", sleep=self.sleep,
                         clock=lambda: self.clock_value, **kwargs)

    def publish(self, rows, **kwargs):
        return self.publisher().publish(FakeStore(rows), DAY, DAY, historical_start=DAY, **kwargs)

    def establish(self, rows=None):
        self.publish([record()] if rows is None else rows)
        self.google.calls.clear()
        self.google.uploads.clear()
        self.google.final_batches.clear()

    def assert_no_stage(self):
        self.assertFalse(any(sheet["properties"]["title"].startswith("_REX_ENVIO_")
                             for sheet in self.google.sheets_state))

    def test_first_publication_owns_stable_live_and_control_tabs(self):
        result = self.publish([record()])
        self.assertEqual(result["linhas"], 1)
        self.assertEqual(result["dias"], 1)
        self.assertFalse(result["reconciliado"])
        live, control = self.google.by_title(LIVE_TITLE), self.google.by_title(CONTROL_TITLE)
        self.assertEqual(live["cells"][0], list(HEADERS))
        self.assertEqual(live["developerMetadata"][0]["metadataValue"], LIVE_ROLE)
        self.assertEqual(control["developerMetadata"][0]["metadataValue"], CONTROL_ROLE)
        self.assertEqual(control["cells"][-1], ["situacao", "completo"])
        self.assert_no_stage()

    def test_raw_literals_codes_dates_numeric_and_bool_for_bi(self):
        self.publish([record(equipamento_codigo="0012", operacao_nome="=IMPORTXML(\"x\",\"y\")",
                             horas_trabalhando="12.125000001", offline=True)])
        live = self.google.by_title(LIVE_TITLE)
        self.assertEqual(live["cells"][1][0], (DAY - date(1899, 12, 30)).days)
        self.assertEqual(live["cells"][1][HEADERS.index("equipamento_codigo")], "0012")
        self.assertEqual(live["cells"][1][HEADERS.index("operacao_nome")], '=IMPORTXML("x","y")')
        self.assertEqual(live["cells"][1][HEADERS.index("horas_trabalhando")], 12.125000001)
        self.assertIs(live["cells"][1][HEADERS.index("offline")], True)
        self.assertTrue(all(upload["valueInputOption"] == "RAW" for upload in self.google.uploads))
        self.assertEqual(live["formats"][0], {"type": "DATE", "pattern": "yyyy-mm-dd"})
        self.assertEqual(live["formats"][4], {"type": "TEXT"})

    def test_replacement_keeps_ids_and_removes_old_rows(self):
        self.establish([record(), record(), record()])
        live_id = self.google.by_title(LIVE_TITLE)["properties"]["sheetId"]
        control_id = self.google.by_title(CONTROL_TITLE)["properties"]["sheetId"]
        self.publish([record(equipamento_chave="nova")])
        live = self.google.by_title(LIVE_TITLE)
        self.assertEqual(live["properties"]["sheetId"], live_id)
        self.assertEqual(self.google.by_title(CONTROL_TITLE)["properties"]["sheetId"], control_id)
        self.assertEqual(len(live["cells"]), 2)
        self.assertEqual(live["cells"][1][1], "nova")
        self.assertEqual(live["properties"]["gridProperties"]["rowCount"], 2)
        self.assert_no_stage()

    def test_growth_occurs_inside_final_transaction(self):
        self.establish()
        self.publish([record(), record(), record()])
        self.assertEqual(len(self.google.by_title(LIVE_TITLE)["cells"]), 4)
        commits = self.google.final_batches[-1]
        first_copy = next(i for i, request in enumerate(commits) if "copyPaste" in request)
        growth = next(i for i, request in enumerate(commits)
                      if "updateSheetProperties" in request and request["updateSheetProperties"]["properties"]["gridProperties"].get("rowCount") == 4)
        self.assertLess(growth, first_copy)

    def test_upload_failure_preserves_both_published_tabs_and_redacts_errors(self):
        self.establish()
        before = deepcopy(self.google.sheets_state)
        self.google.fail_upload = True
        with self.assertRaises(SheetsError) as caught:
            self.publish([record(equipamento_chave="nova")])
        self.assertNotIn("secret-password", str(caught.exception))
        self.assertEqual(self.google.sheets_state, before)
        self.assertFalse(self.google.final_batches)
        self.assert_no_stage()

    def test_invalid_final_batch_is_atomic_and_control_stays_old(self):
        self.establish()
        before = deepcopy(self.google.sheets_state)
        self.google.fail_commit = True
        with self.assertRaises(SheetsError):
            self.publish([record(equipamento_chave="nova")])
        self.assertEqual(self.google.sheets_state, before)
        self.assert_no_stage()

    def test_lost_commit_response_is_reconciled_without_repeating_batch(self):
        self.establish()
        self.google.lost_response = True
        result = self.publish([record(equipamento_chave="nova")])
        self.assertTrue(result["reconciliado"])
        self.assertEqual(len(self.google.final_batches), 1)
        self.assertEqual(self.google.by_title(LIVE_TITLE)["cells"][1][1], "nova")
        self.assert_no_stage()

    def test_cleanup_failure_is_reported_and_live_copy_is_preserved(self):
        self.establish()
        live_before = deepcopy(self.google.by_title(LIVE_TITLE))
        self.google.fail_upload = self.google.fail_cleanup = True
        with self.assertRaises(SheetsError) as caught:
            self.publish([record()])
        self.assertIn("aba temporaria", str(caught.exception))
        self.assertEqual(self.google.by_title(LIVE_TITLE), live_before)

    def test_budget_counts_unrelated_blank_grid_and_both_new_copies(self):
        publisher = self.publisher(cell_limit=26_000 + 62 + 20 - 1)
        with self.assertRaises(SheetsError):
            publisher.publish(FakeStore([]), DAY, DAY, historical_start=DAY)
        self.assertFalse(any(call[0] == "batch" for call in self.google.calls))

    def test_budget_counts_live_growth_while_staging_still_exists(self):
        self.establish()
        # Existing grids: default 26000 + live 62 + control 20.
        # New live+stage each124, and live grows62 => peak26268.
        with self.assertRaises(SheetsError):
            self.publisher(cell_limit=26_267).publish(FakeStore([record()] * 3), DAY, DAY, historical_start=DAY)
        self.assertFalse(self.google.final_batches)

    def test_unowned_reserved_tab_is_never_overwritten_even_matching_headers(self):
        self.google.sheets_state.append({"properties": {"sheetId": 8, "title": LIVE_TITLE,
             "gridProperties": {"rowCount": 2, "columnCount": 31}},
             "developerMetadata": [], "cells": [list(HEADERS), ["dados pessoais"]]})
        before = deepcopy(self.google.sheets_state)
        with self.assertRaises(SheetsError):
            self.publish([record()])
        self.assertEqual(self.google.sheets_state, before)
        self.assertFalse(any(call[0] == "batch" for call in self.google.calls))

    def test_changed_owned_header_is_rejected_before_any_write(self):
        self.establish()
        self.google.by_title(LIVE_TITLE)["cells"][0][0] = "DataManual"
        with self.assertRaises(SheetsError):
            self.publish([record()])
        self.assertFalse(any(call[0] == "batch" for call in self.google.calls))

    def test_renamed_owned_tab_is_not_recreated(self):
        self.establish()
        self.google.by_title(LIVE_TITLE)["properties"]["title"] = "NomeManual"
        with self.assertRaises(SheetsError):
            self.publish([record()])
        self.assertFalse(any(call[0] == "batch" for call in self.google.calls))

    def test_protected_and_merged_owned_tabs_are_rejected(self):
        for key, value in (("protectedRanges", [{"requestingUserCanEdit": False}]),
                           ("merges", [{"sheetId": 1}])):
            with self.subTest(key=key):
                self.google = FakeGoogle()
                self.establish()
                self.google.by_title(LIVE_TITLE)[key] = value
                with self.assertRaises(SheetsError):
                    self.publish([record()])
                self.assertFalse(any(call[0] == "batch" for call in self.google.calls))

    def test_missing_coverage_is_rejected_without_google_requests(self):
        store = FakeStore([record()], days={DAY})
        with self.assertRaises(SheetsError):
            self.publisher().publish(store, DAY - timedelta(days=1), DAY, historical_start=DAY - timedelta(days=1))
        self.assertEqual(self.google.calls, [])

    def test_empty_collected_day_is_published_as_header_only(self):
        result = self.publish([])
        live = self.google.by_title(LIVE_TITLE)
        self.assertEqual(result["linhas"], 0)
        self.assertEqual(live["cells"], [list(HEADERS)])
        self.assertEqual(live["properties"]["gridProperties"]["frozenRowCount"], 0)
        self.assertEqual(self.google.by_title(CONTROL_TITLE)["cells"][-1], ["situacao", "completo"])

    def test_changed_iterator_count_cannot_commit_partial_result(self):
        self.establish()
        before = deepcopy(self.google.sheets_state)
        for claimed in (0, 2):
            with self.subTest(claimed=claimed), self.assertRaises(SheetsError):
                self.publisher().publish(FakeStore([record()], claimed_count=claimed), DAY, DAY, historical_start=DAY)
        self.assertEqual(self.google.sheets_state, before)
        self.assertFalse(self.google.final_batches)

    def test_outside_interval_and_nonfinite_number_preserve_published_data(self):
        self.establish()
        before = deepcopy(self.google.sheets_state)
        for row in (record(day=DAY + timedelta(days=1)), record(area="NaN"), record(snapshots=2**53)):
            with self.subTest(row=row), self.assertRaises(SheetsError):
                self.publish([row])
        self.assertEqual(self.google.sheets_state, before)
        self.assertFalse(self.google.final_batches)

    def test_batches_bound_actual_json_bytes_including_unicode_escapes(self):
        with patch("rex_local.sheets.CHUNK_BYTES", 1_000):
            self.publish([record(operacao_nome="produção" * 5)] * 10)
        self.assertGreater(len(self.google.uploads), 1)
        for upload in self.google.uploads:
            self.assertLessEqual(len(json.dumps(upload["body"]).encode("utf-8")), 1_000)
        self.assertEqual(len(self.google.by_title(LIVE_TITLE)["cells"]), 11)

    def test_write_throttle_covers_stage_chunks_commit_and_cleanup(self):
        with patch("rex_local.sheets.CHUNK_BYTES", 800):
            self.publish([record()] * 5)
        writes = sum(call[0] in ("batch", "update") for call in self.google.calls)
        self.assertEqual(len(self.sleeps), writes - 1)
        self.assertTrue(all(delay >= 1.099 for delay in self.sleeps))

    def test_invalid_destination_or_period_does_not_touch_google(self):
        with self.assertRaises(SheetsError):
            Publisher(self.google, "https://docs.google.com/spreadsheets/d/id")
        with self.assertRaises(SheetsError):
            self.publisher().publish(FakeStore([]), DAY, DAY, historical_start=DAY + timedelta(days=1))
        self.assertEqual(self.google.calls, [])


if __name__ == "__main__":
    unittest.main()
