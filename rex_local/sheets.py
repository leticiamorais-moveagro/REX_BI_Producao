"""Publish a complete local interval to a stable Google Sheets worksheet.

The live worksheet is replaced only after a separate staging worksheet has
received every row. One atomic Sheets batch commits data, formats, control
information and staging removal together. No API response or source row appears
in error messages. The caller supplies an authenticated Sheets v4 service.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import time
from uuid import uuid4

from .model import SCHEMA


HEADERS = tuple(name for name, _kind, _mode in SCHEMA)
LIVE_TITLE = "Producao_REX"
CONTROL_TITLE = "Controle_REX"
ROLE_KEY = "rex_local_role"
PUBLISH_KEY = "rex_local_publish_id"
LIVE_ROLE = "production_v1"
CONTROL_ROLE = "control_v1"
CHUNK_BYTES = 1_500_000
WRITE_INTERVAL = 1.1
CONTROL_ROWS = 10
CONTROL_COLUMNS = 2


class SheetsError(ValueError):
    """A publication cannot be confirmed; messages contain no source values."""


def _range(sheet_id, rows, columns, *, start_row=0, start_column=0):
    return {
        "sheetId": sheet_id,
        "startRowIndex": start_row,
        "endRowIndex": rows,
        "startColumnIndex": start_column,
        "endColumnIndex": columns,
    }


def _marker(role, sheet_id):
    return {"createDeveloperMetadata": {"developerMetadata": {
        "metadataKey": ROLE_KEY,
        "metadataValue": role,
        "location": {"sheetId": sheet_id},
        "visibility": "DOCUMENT",
    }}}


def _role(sheet):
    roles = [item.get("metadataValue")
             for item in sheet.get("developerMetadata", [])
             if item.get("metadataKey") == ROLE_KEY]
    if len(roles) > 1:
        raise SheetsError("Aba REX com identificacao duplicada. Revise a planilha.")
    return roles[0] if roles else None


def _grid_size(sheet):
    properties = sheet.get("properties", {})
    if properties.get("sheetType", "GRID") != "GRID":
        return 0
    grid = properties.get("gridProperties", {})
    rows, columns = grid.get("rowCount"), grid.get("columnCount")
    if (not isinstance(rows, int) or isinstance(rows, bool) or rows < 1
            or not isinstance(columns, int) or isinstance(columns, bool)
            or columns < 1):
        raise SheetsError("Nao foi possivel medir as celulas alocadas da planilha.")
    return rows * columns


def _row_values(row):
    if not isinstance(row, dict) or set(row) != set(HEADERS):
        raise SheetsError("O banco local nao corresponde ao esquema REX esperado.")
    result = []
    for name, kind, mode in SCHEMA:
        value = row[name]
        if value is None:
            if mode == "REQUIRED":
                raise SheetsError("O banco local possui um campo obrigatorio vazio.")
            result.append("")
        elif kind == "DATE":
            if not isinstance(value, str):
                raise SheetsError("Data invalida no banco local.")
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                raise SheetsError("Data invalida no banco local.") from None
            result.append((parsed - date(1899, 12, 30)).days)
        elif kind == "NUMERIC":
            if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
                raise SheetsError("Metrica invalida no banco local.")
            try:
                number = float(Decimal(str(value)))
            except (ValueError, InvalidOperation, OverflowError):
                raise SheetsError("Metrica invalida no banco local.") from None
            if not math.isfinite(number):
                raise SheetsError("Metrica invalida no banco local.")
            result.append(number)
        elif kind == "INT64":
            if (not isinstance(value, int) or isinstance(value, bool)
                    or value < 0 or value > 2**53 - 1):
                raise SheetsError("Contagem fora da precisao numerica suportada no Sheets.")
            result.append(value)
        elif kind == "BOOL":
            if not isinstance(value, bool):
                raise SheetsError("Indicador invalido no banco local.")
            result.append(value)
        else:
            if not isinstance(value, str) or len(value) > 50_000:
                raise SheetsError("Texto invalido ou muito longo para uma celula Sheets.")
            # RAW keeps codes, leading zeros and formula-like strings literal.
            result.append(value)
    return result


class Publisher:
    """One local process must own the collector/publication lock while calling."""

    def __init__(self, service, spreadsheet_id, cell_limit=10_000_000,
                 sleep=time.sleep, clock=time.monotonic):
        if (not isinstance(spreadsheet_id, str) or not spreadsheet_id
                or any(not (char.isalnum() or char in "_-") for char in spreadsheet_id)):
            raise SheetsError("ID da planilha Google invalido.")
        if (not isinstance(cell_limit, int) or isinstance(cell_limit, bool)
                or cell_limit < 1):
            raise SheetsError("Limite de celulas invalido.")
        self.service = service
        self.spreadsheet_id = spreadsheet_id
        self.cell_limit = cell_limit
        self.sleep = sleep
        self.clock = clock
        self._last_write = None

    def _execute(self, request, message, *, write=False):
        if write:
            current = self.clock()
            if self._last_write is not None:
                delay = WRITE_INTERVAL - (current - self._last_write)
                if delay > 0:
                    self.sleep(delay)
            self._last_write = self.clock()
        try:
            return request.execute()
        except Exception:
            raise SheetsError(message) from None

    def _state(self):
        return self._execute(
            self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id,
                fields="spreadsheetId,sheets(properties,protectedRanges,merges,developerMetadata,tables)"),
            "Nao foi possivel consultar a estrutura da planilha Google.")

    def _batch(self, requests, message):
        return self._execute(
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": requests, "includeSpreadsheetInResponse": False}),
            message, write=True)

    def _managed_sheet(self, sheets, title, role, columns, header):
        for sheet in sheets:
            found_role = _role(sheet)
            found_title = sheet.get("properties", {}).get("title")
            if found_role == role and found_title != title:
                raise SheetsError("Uma aba REX foi renomeada. Restaure o nome antes de publicar.")
        found = [sheet for sheet in sheets
                 if sheet.get("properties", {}).get("title") == title]
        if not found:
            return None
        if len(found) != 1 or _role(found[0]) != role:
            raise SheetsError("Ja existe uma aba reservada sem identificacao REX. Use uma planilha nova.")
        sheet = found[0]
        properties = sheet["properties"]
        grid = properties.get("gridProperties", {})
        if properties.get("sheetType", "GRID") != "GRID" or grid.get("columnCount") != columns:
            raise SheetsError("A estrutura de uma aba REX foi alterada. Revise a planilha.")
        if sheet.get("merges") or sheet.get("tables"):
            raise SheetsError("Aba REX possui mesclas ou tabelas adicionais. Revise a planilha.")
        for protection in sheet.get("protectedRanges", []):
            if not protection.get("warningOnly", False) and not protection.get("requestingUserCanEdit", False):
                raise SheetsError("Aba REX possui celulas protegidas sem permissao de escrita.")
        end_column = "AE" if columns == len(HEADERS) else "B"
        response = self._execute(
            self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{title}'!A1:{end_column}1",
                valueRenderOption="UNFORMATTED_VALUE"),
            "Nao foi possivel conferir o cabecalho da aba REX.")
        values = response.get("values", [])
        if not values or values[0] != list(header):
            raise SheetsError("O cabecalho de uma aba REX foi alterado. Revise antes de publicar.")
        return sheet

    def _cleanup(self, stage_id):
        try:
            self._batch([{"deleteSheet": {"sheetId": stage_id}}],
                        "Nao foi possivel remover a aba temporaria REX.")
            return True
        except Exception:
            return False

    def _confirmed(self, control_id, publication_id):
        try:
            state = self._state()
            for sheet in state.get("sheets", []):
                if sheet.get("properties", {}).get("sheetId") == control_id and _role(sheet) == CONTROL_ROLE:
                    return any(item.get("metadataKey") == PUBLISH_KEY
                               and item.get("metadataValue") == publication_id
                               for item in sheet.get("developerMetadata", []))
        except Exception:
            pass
        return False

    def _upload(self, stage_title, store, start, end, planned_count):
        chunk = []
        chunk_size = 64
        next_row = 1
        actual_count = 0

        def send():
            nonlocal chunk, chunk_size, next_row
            if not chunk:
                return
            last_row = next_row + len(chunk) - 1
            self._execute(
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"'{stage_title}'!A{next_row}:AE{last_row}",
                    valueInputOption="RAW",
                    body={"majorDimension": "ROWS", "values": chunk}),
                "Falha no envio temporario ao Sheets; a copia publicada nao foi substituida.",
                write=True)
            next_row = last_row + 1
            chunk = []
            chunk_size = 64

        def add(values):
            nonlocal chunk_size
            # Match the Google Python client's default JSON serializer, including
            # ASCII escapes and spaces; UTF-8 characters otherwise undercount.
            size = len(json.dumps(values, allow_nan=False).encode("utf-8")) + 2
            if size + 64 > CHUNK_BYTES:
                raise SheetsError("Um registro excede o tamanho de envio permitido ao Sheets.")
            if chunk and chunk_size + size > CHUNK_BYTES:
                send()
            chunk.append(values)
            chunk_size += size

        add(list(HEADERS))
        for row in store.iter_rows(start, end):
            actual_count += 1
            if actual_count > planned_count:
                raise SheetsError("O banco local mudou durante a publicacao. Execute novamente com apenas um coletor.")
            values = _row_values(row)
            if not start.isoformat() <= row["data"] <= end.isoformat():
                raise SheetsError("O banco local retornou uma data fora do periodo de publicacao.")
            add(values)
        send()
        if actual_count != planned_count:
            raise SheetsError("O banco local mudou durante a publicacao. Execute novamente com apenas um coletor.")

    def publish(self, store, start: date, end: date, *, historical_start: date):
        stage_id = None
        committed = False
        try:
            if (type(start) is not date or type(end) is not date
                    or type(historical_start) is not date
                    or end < start or historical_start > start):
                raise SheetsError("Periodo de publicacao invalido.")
            expected_days = (end - start).days + 1
            collected = store.collected_days()
            available_days = sum(1 for day in collected if start <= day <= end)
            if available_days != expected_days:
                raise SheetsError("Faltam dias coletados no periodo solicitado. Complete a coleta antes de publicar.")
            count = store.count(start, end)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise SheetsError("Nao foi possivel contar os registros no banco local.")
            new_rows = count + 1
            state = self._state()
            sheets = state.get("sheets", [])
            live = self._managed_sheet(sheets, LIVE_TITLE, LIVE_ROLE, len(HEADERS), HEADERS)
            control = self._managed_sheet(sheets, CONTROL_TITLE, CONTROL_ROLE, CONTROL_COLUMNS, ("campo", "valor"))
            allocated = sum(_grid_size(sheet) for sheet in sheets)
            live_cells = new_rows * len(HEADERS)
            control_cells = CONTROL_ROWS * CONTROL_COLUMNS
            growth = max(0, live_cells - _grid_size(live)) if live else live_cells
            growth += max(0, control_cells - _grid_size(control)) if control else control_cells
            peak_cells = allocated + live_cells + growth
            if peak_cells > self.cell_limit:
                raise SheetsError(
                    f"Publicacao precisa de {peak_cells:,} celulas durante o envio; "
                    f"limite configurado: {self.cell_limit:,}. Historico local preservado.")
            ids = {sheet["properties"]["sheetId"] for sheet in sheets}

            def fresh_id():
                candidate = uuid4().int & 0x7fffffff
                while candidate in ids or candidate == 0:
                    candidate = uuid4().int & 0x7fffffff
                ids.add(candidate)
                return candidate

            live_id = live["properties"]["sheetId"] if live else fresh_id()
            control_id = control["properties"]["sheetId"] if control else fresh_id()
            stage_id = fresh_id()
            publication_id = str(uuid4())
            stage_title = "_REX_ENVIO_" + publication_id.replace("-", "")
            self._batch([{"addSheet": {"properties": {
                "sheetId": stage_id, "title": stage_title, "hidden": True,
                "gridProperties": {"rowCount": new_rows, "columnCount": len(HEADERS)},
            }}}], "Nao foi possivel criar a aba temporaria REX.")
            self._upload(stage_title, store, start, end, count)
            requests = []
            for existing, sheet_id, title, rows, columns, role in (
                (live, live_id, LIVE_TITLE, new_rows, len(HEADERS), LIVE_ROLE),
                (control, control_id, CONTROL_TITLE, CONTROL_ROWS, CONTROL_COLUMNS, CONTROL_ROLE),
            ):
                if existing is None:
                    requests.extend([
                        {"addSheet": {"properties": {
                            "sheetId": sheet_id, "title": title,
                            "gridProperties": {"rowCount": rows, "columnCount": columns},
                        }}},
                        _marker(role, sheet_id),
                    ])
                elif rows > existing["properties"]["gridProperties"]["rowCount"]:
                    requests.append({"updateSheetProperties": {
                        "properties": {"sheetId": sheet_id, "gridProperties": {"rowCount": rows}},
                        "fields": "gridProperties.rowCount",
                    }})
            clear_rows = max(new_rows, live["properties"]["gridProperties"]["rowCount"] if live else new_rows)
            requests.append({"updateCells": {
                "range": _range(live_id, clear_rows, len(HEADERS)),
                "fields": "userEnteredValue", "rows": [],
            }})
            requests.append({"copyPaste": {
                "source": _range(stage_id, new_rows, len(HEADERS)),
                "destination": _range(live_id, new_rows, len(HEADERS)),
                "pasteType": "PASTE_VALUES", "pasteOrientation": "NORMAL",
            }})
            if count:
                for index, (_name, kind, _mode) in enumerate(SCHEMA):
                    if kind == "DATE":
                        number_format = {"type": "DATE", "pattern": "yyyy-mm-dd"}
                    elif kind in ("NUMERIC", "INT64"):
                        number_format = {"type": "NUMBER", "pattern": "0.#########"}
                    elif kind in ("STRING", "TIMESTAMP"):
                        number_format = {"type": "TEXT"}
                    else:
                        continue
                    requests.append({"repeatCell": {
                        "range": _range(live_id, new_rows, index + 1, start_row=1, start_column=index),
                        "cell": {"userEnteredFormat": {"numberFormat": number_format}},
                        "fields": "userEnteredFormat.numberFormat",
                    }})
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            control_values = [
                ["campo", "valor"], ["versao", "rex-local-v1"],
                ["publicacao_id", publication_id], ["atualizado_em_utc", now],
                ["periodo_inicio", start.isoformat()], ["periodo_fim", end.isoformat()],
                ["dias_coletados", str(expected_days)], ["linhas", str(count)],
                ["historico_local_inicio", historical_start.isoformat()], ["situacao", "completo"],
            ]
            requests.append({"updateCells": {
                "range": _range(control_id, max(CONTROL_ROWS, control["properties"]["gridProperties"]["rowCount"] if control else CONTROL_ROWS), CONTROL_COLUMNS),
                "rows": [{"values": [{"userEnteredValue": {"stringValue": value}} for value in values]} for values in control_values],
                "fields": "userEnteredValue",
            }})
            existing_publish = [item for item in (control or {}).get("developerMetadata", [])
                                if item.get("metadataKey") == PUBLISH_KEY]
            if len(existing_publish) > 1:
                raise SheetsError("Identificacao de publicacao duplicada. Revise Controle_REX.")
            if existing_publish:
                metadata_id = existing_publish[0].get("metadataId")
                if not isinstance(metadata_id, int):
                    raise SheetsError("Identificacao de publicacao invalida. Revise Controle_REX.")
                requests.append({"updateDeveloperMetadata": {
                    "dataFilters": [{"developerMetadataLookup": {"metadataId": metadata_id}}],
                    "developerMetadata": {"metadataValue": publication_id}, "fields": "metadataValue",
                }})
            else:
                requests.append({"createDeveloperMetadata": {"developerMetadata": {
                    "metadataKey": PUBLISH_KEY, "metadataValue": publication_id,
                    "location": {"sheetId": control_id}, "visibility": "DOCUMENT",
                }}})
            # Shrink only after clearing/copying, so old-range bounds remain valid.
            for sheet_id, rows in ((live_id, new_rows), (control_id, CONTROL_ROWS)):
                requests.append({"updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "gridProperties": {"rowCount": rows, "frozenRowCount": 1 if rows > 1 else 0}},
                    "fields": "gridProperties.rowCount,gridProperties.frozenRowCount",
                }})
            requests.append({"deleteSheet": {"sheetId": stage_id}})
            reconciled = False
            try:
                self._batch(requests, "Publicacao no Google Sheets nao confirmada. Confira Controle_REX antes de repetir.")
                committed = True
            except SheetsError:
                if not self._confirmed(control_id, publication_id):
                    raise
                committed = True
                reconciled = True
            return {
                "linhas": count, "inicio": start.isoformat(), "fim": end.isoformat(),
                "dias": expected_days, "publicacao_id": publication_id,
                "atualizado_em_utc": now, "celulas_pico": peak_cells,
                "reconciliado": reconciled,
            }
        except SheetsError as exc:
            if stage_id is not None and not committed and not self._cleanup(stage_id):
                raise SheetsError(str(exc) + " Pode ter restado uma aba temporaria _REX_ENVIO_.") from None
            raise
        except Exception:
            cleanup_ok = stage_id is None or committed or self._cleanup(stage_id)
            message = "Nao foi possivel publicar o periodo no Google Sheets."
            if not cleanup_ok:
                message += " Pode ter restado uma aba temporaria _REX_ENVIO_."
            raise SheetsError(message) from None
