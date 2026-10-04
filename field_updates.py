"""Pure, header-addressed company-cell plans using the shared typed builder.

This module has no network, credentials, guard acquisition or customer action.
The caller supplies live native header GridData, a captured row number and an
explicit existing field-ownership allowlist. Identity, approval, qualification,
suppression and the fenced atomic commit/readback remain independent checks.
"""
from __future__ import annotations

import json
import math
from typing import Any, Iterable, Mapping

import sheets_persistence as original_builder


class FieldUpdateError(ValueError):
    pass


def _integer(value: Any, minimum: int, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise FieldUpdateError(reason)
    return value


def header_columns(native_header_grid: Mapping[str, Any]) -> dict[str, int]:
    """Return one-based columns from exact native row-1 labels and coordinates.

    Native GridData omits zero-valued startRow/startColumn. A sliced header read
    retains its actual startColumn; callers never supply a guessed A1 offset.
    Empty, unlabeled cells occupy a column but provide no field name.
    """
    if not isinstance(native_header_grid, Mapping):
        raise FieldUpdateError("NATIVE_HEADER_GRID_REQUIRED")
    start_row = _integer(native_header_grid.get("startRow", 0), 0, "HEADER_ROW_INVALID")
    start_column = _integer(native_header_grid.get("startColumn", 0), 0, "HEADER_COLUMN_INVALID")
    if start_row != 0:
        raise FieldUpdateError("HEADER_MUST_BE_NATIVE_ROW_ONE")
    rows = native_header_grid.get("rowData")
    if not isinstance(rows, list) or len(rows) != 1:
        raise FieldUpdateError("EXACTLY_ONE_NATIVE_HEADER_ROW_REQUIRED")
    if not isinstance(rows[0], Mapping) or not isinstance(rows[0].get("values"), list):
        raise FieldUpdateError("NATIVE_HEADER_VALUES_REQUIRED")
    columns = {}
    for offset, cell in enumerate(rows[0]["values"]):
        if not isinstance(cell, Mapping):
            raise FieldUpdateError("NATIVE_HEADER_CELL_INVALID")
        typed = cell.get("effectiveValue", cell.get("userEnteredValue"))
        if not typed:
            if cell.get("formattedValue"):
                raise FieldUpdateError("NATIVE_HEADER_TYPED_VALUE_MISSING")
            continue
        if not isinstance(typed, Mapping) or set(typed) != {"stringValue"}:
            raise FieldUpdateError("NATIVE_HEADER_MUST_BE_STRING")
        label = typed["stringValue"]
        if not isinstance(label, str):
            raise FieldUpdateError("NATIVE_HEADER_MUST_BE_STRING")
        if label == "":
            continue
        if not label.strip():
            raise FieldUpdateError("NATIVE_HEADER_BLANK_LABEL")
        if label in columns:
            raise FieldUpdateError("DUPLICATE_HEADER:" + label)
        columns[label] = start_column + offset + 1
    return columns


def _value(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise FieldUpdateError("CELL_NUMBER_MUST_BE_FINITE")
    if isinstance(value, (dict, list)):
        try:
            json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise FieldUpdateError("CELL_JSON_VALUE_INVALID") from exc
        return
    raise FieldUpdateError("CELL_VALUE_TYPE_UNSUPPORTED")


def build_field_updates(*, sheet_id: int, source_row: int,
                        expected_source_row: int,
                        native_header_grid: Mapping[str, Any],
                        updates: Mapping[str, Any],
                        allowed_fields: Iterable[str]) -> list[dict]:
    """Plan only explicitly owned named cells, each with a one-cell field mask.

    Values are Python literals, not native CellData or formula requests. The
    original cell_update preserves bool/number/string types and encodes JSON.
    An explicit None/empty-string clears just that requested, allowed cell.
    No approval or ownership is inferred from a value or from adjacent columns.
    """
    _integer(sheet_id, 0, "SHEET_ID_INVALID")
    _integer(source_row, 2, "SOURCE_ROW_INVALID")
    _integer(expected_source_row, 2, "EXPECTED_SOURCE_ROW_INVALID")
    if source_row != expected_source_row:
        raise FieldUpdateError("SOURCE_ROW_CHANGED")
    if not isinstance(updates, Mapping):
        raise FieldUpdateError("NAMED_FIELD_UPDATES_REQUIRED")
    if isinstance(allowed_fields, (str, bytes)):
        raise FieldUpdateError("EXPLICIT_FIELD_ALLOWLIST_REQUIRED")
    try:
        allowed = list(allowed_fields)
    except TypeError as exc:
        raise FieldUpdateError("EXPLICIT_FIELD_ALLOWLIST_REQUIRED") from exc
    if any(not isinstance(name, str) or not name.strip() for name in allowed):
        raise FieldUpdateError("ALLOWLIST_FIELD_INVALID")
    allowed_set = set(allowed)
    columns = header_columns(native_header_grid)
    for name, value in updates.items():
        if not isinstance(name, str) or not name.strip():
            raise FieldUpdateError("EXACT_HEADER_NAME_REQUIRED")
        if name not in allowed_set:
            raise FieldUpdateError("FIELD_NOT_OWNED:" + name)
        if name not in columns:
            raise FieldUpdateError("HEADER_MISSING:" + name)
        _value(value)
    return [original_builder.cell_update(sheet_id, source_row, columns[name], updates[name])
            for name in sorted(updates, key=columns.__getitem__)]
