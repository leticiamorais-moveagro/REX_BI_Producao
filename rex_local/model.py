"""Validate and flatten a full REX daily response before any database write.

Only the standard library is needed. Decimal metrics are emitted as JSON strings
so a JSON encoder never converts them to imprecise binary floating point values.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any


SCHEMA = (
    ("data", "DATE", "REQUIRED"),
    ("equipamento_chave", "STRING", "REQUIRED"),
    ("unidade_codigo", "STRING", "NULLABLE"),
    ("unidade_nome", "STRING", "NULLABLE"),
    ("equipamento_codigo", "STRING", "NULLABLE"),
    ("modelo_codigo", "STRING", "NULLABLE"),
    ("modelo_nome", "STRING", "NULLABLE"),
    ("operacao_codigo", "STRING", "NULLABLE"),
    ("operacao_nome", "STRING", "NULLABLE"),
    ("classe_operacao", "STRING", "NULLABLE"),
    ("tipo_operacao", "STRING", "NULLABLE"),
    ("operador_codigo", "STRING", "NULLABLE"),
    ("operador_nome", "STRING", "NULLABLE"),
    ("turno_codigo", "STRING", "NULLABLE"),
    ("turno_nome", "STRING", "NULLABLE"),
    ("centro_custo_codigo", "STRING", "NULLABLE"),
    ("centro_custo_nome", "STRING", "NULLABLE"),
    ("horimetro_horas", "NUMERIC", "NULLABLE"),
    ("horas_trabalhando", "NUMERIC", "NULLABLE"),
    ("horas_deslocamento", "NUMERIC", "NULLABLE"),
    ("horas_motor_ocioso", "NUMERIC", "NULLABLE"),
    ("horas_parado", "NUMERIC", "NULLABLE"),
    ("horas_desligado", "NUMERIC", "NULLABLE"),
    ("horas_total", "NUMERIC", "NULLABLE"),
    ("area", "NUMERIC", "NULLABLE"),
    ("distancia", "NUMERIC", "NULLABLE"),
    ("combustivel", "NUMERIC", "NULLABLE"),
    ("snapshots", "INT64", "NULLABLE"),
    ("offline", "BOOL", "NULLABLE"),
    ("importado_em", "TIMESTAMP", "REQUIRED"),
    ("carga_id", "STRING", "REQUIRED"),
)

_DECIMAL_TEXT = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_DATE_TEXT = re.compile(r"(?:[0-9]{8}|[0-9]{4}-[0-9]{2}-[0-9]{2})\Z")
_TIMESTAMP_TEXT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z"
)
_INT64_MAX = 2**63 - 1


class NormalizationError(ValueError):
    """The response cannot be loaded safely; messages never contain API data."""


def _fail(field: str) -> None:
    # All callers use fixed field labels, never values or equipment identifiers.
    raise NormalizationError(f"Campo REX invalido: {field}.") from None


def _object(value: Any, field: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail(field)
    return value


def _text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        _fail(field)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return str(value)
    if isinstance(value, Decimal) and value.is_finite():
        return str(value)
    _fail(field)


def _decimal(value: Any, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        _fail(field)
    if isinstance(value, str):
        value = value.strip()
        if len(value) > 256 or not _DECIMAL_TEXT.fullmatch(value):
            _fail(field)
    if isinstance(value, float) and not math.isfinite(value):
        _fail(field)
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        _fail(field)
    if not parsed.is_finite():
        _fail(field)
    return parsed


def _numeric(value: Any, field: str) -> str | None:
    parsed = _decimal(value, field)
    if parsed is None:
        return None
    if not parsed:
        return "0"
    # Decimal.normalize() would round under the global context (default 28
    # digits). Inspect the exact tuple instead, discarding only trailing zeros.
    sign, digits, exponent = parsed.as_tuple()
    while digits[-1] == 0:
        digits = digits[:-1]
        exponent += 1
    scale = max(-exponent, 0)
    integer_digits = max(len(digits) + exponent, 0)
    if scale > 9 or integer_digits > 29 or integer_digits + scale > 38:
        _fail(field)
    exact = Decimal((sign, digits, exponent))
    return format(exact, "f")


def _snapshots(value: Any) -> int | None:
    parsed = _decimal(value, "snapshots")
    if parsed is None:
        return None
    if parsed < 0 or parsed > _INT64_MAX or parsed != parsed.to_integral_value():
        _fail("snapshots")
    return int(parsed)


def _record_date(value: Any, day: date) -> str:
    if not isinstance(value, str) or not _DATE_TEXT.fullmatch(value):
        _fail("date")
    iso = value if len(value) == 10 else f"{value[:4]}-{value[4:6]}-{value[6:]}"
    try:
        parsed = date.fromisoformat(iso)
    except ValueError:
        _fail("date")
    if parsed != day:
        raise NormalizationError("REX retornou uma data fora do dia solicitado.")
    return iso


def _metadata(imported_at: Any, load_id: Any) -> None:
    if not isinstance(load_id, str) or not load_id.strip():
        raise NormalizationError("carga_id deve ser um texto nao vazio.")
    if not isinstance(imported_at, str) or not _TIMESTAMP_TEXT.fullmatch(imported_at):
        raise NormalizationError("importado_em deve ser um timestamp ISO com fuso horario.")
    try:
        parsed = datetime.fromisoformat(imported_at.replace("Z", "+00:00"))
    except ValueError:
        raise NormalizationError("importado_em deve ser um timestamp ISO com fuso horario.") from None
    if parsed.utcoffset() is None:
        raise NormalizationError("importado_em deve ser um timestamp ISO com fuso horario.")


def normalize_daily(
    payload: Any, day: date, *, imported_at: str, load_id: str
) -> list[dict]:
    """Return all rows for exactly ``day`` or fail without returning partial data.

    Missing and null optional fields remain None. Duplicate consolidated records
    are retained: the REX contract provides no unique key for deduplication.
    ``offline`` is copied as reported and is not interpreted as a stopped machine.
    """
    if not isinstance(day, date) or isinstance(day, datetime):
        raise NormalizationError("O dia solicitado deve ser datetime.date.")
    _metadata(imported_at, load_id)
    if not isinstance(payload, dict):
        raise NormalizationError("Producao diaria deve ser um objeto com listas por equipamento.")

    rows = []
    for equipment_key, records in payload.items():
        if not isinstance(equipment_key, str) or not equipment_key.strip():
            _fail("equipamento_chave")
        if not isinstance(records, list):
            raise NormalizationError("A lista de registros de um equipamento e invalida.")
        for item in records:
            if not isinstance(item, dict):
                raise NormalizationError("Registro REX invalido.")
            record_day = _record_date(item.get("date"), day)
            site = _object(item.get("site"), "site")
            equipment = _object(item.get("equipment"), "equipment")
            model = _object(equipment.get("model"), "equipment.model")
            operation = _object(item.get("operation"), "operation")
            operator = _object(item.get("operator"), "operator")
            shift = _object(item.get("shift"), "shift")
            cost_center = _object(item.get("cost-center"), "cost-center")
            hours = _object(item.get("hours"), "hours")
            offline = item.get("offline")
            if offline is not None and not isinstance(offline, bool):
                _fail("offline")
            row = {
                "data": record_day,
                "equipamento_chave": equipment_key,
                "unidade_codigo": _text(site.get("code"), "site.code"),
                "unidade_nome": _text(site.get("name"), "site.name"),
                "equipamento_codigo": _text(equipment.get("code"), "equipment.code"),
                "modelo_codigo": _text(model.get("code"), "equipment.model.code"),
                "modelo_nome": _text(model.get("name"), "equipment.model.name"),
                "operacao_codigo": _text(operation.get("code"), "operation.code"),
                "operacao_nome": _text(operation.get("name"), "operation.name"),
                "classe_operacao": _text(operation.get("class"), "operation.class"),
                "tipo_operacao": _text(operation.get("type"), "operation.type"),
                "operador_codigo": _text(operator.get("code"), "operator.code"),
                "operador_nome": _text(operator.get("name"), "operator.name"),
                "turno_codigo": _text(shift.get("code"), "shift.code"),
                "turno_nome": _text(shift.get("name"), "shift.name"),
                "centro_custo_codigo": _text(cost_center.get("code"), "cost-center.code"),
                "centro_custo_nome": _text(cost_center.get("name"), "cost-center.name"),
                "horimetro_horas": _numeric(item.get("horimeter"), "horimeter"),
                "horas_trabalhando": _numeric(hours.get("working"), "hours.working"),
                "horas_deslocamento": _numeric(hours.get("moving"), "hours.moving"),
                "horas_motor_ocioso": _numeric(hours.get("engine-idle"), "hours.engine-idle"),
                "horas_parado": _numeric(hours.get("stopped"), "hours.stopped"),
                "horas_desligado": _numeric(hours.get("off"), "hours.off"),
                "horas_total": _numeric(hours.get("total"), "hours.total"),
                "area": _numeric(item.get("area"), "area"),
                "distancia": _numeric(item.get("distance"), "distance"),
                "combustivel": _numeric(item.get("fuel"), "fuel"),
                "snapshots": _snapshots(item.get("snapshots")),
                "offline": offline,
                "importado_em": imported_at,
                "carga_id": load_id,
            }
            rows.append(row)
    return rows

