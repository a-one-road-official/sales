"""Pure, exact-name company and Config cell plans using the shared typed builder.

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


def config_key_rows(native_config_grids: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Resolve exact, typed column-A keys from native GridData coordinates.

    Supply the actual data blocks from a current Config read, including column A.
    No range label, remembered row number or caller-computed offset is accepted.
    Overlapping blocks/duplicate keys fail rather than selecting a convenient row.
    """
    if isinstance(native_config_grids, (Mapping, str, bytes)):
        raise FieldUpdateError("NATIVE_CONFIG_GRIDS_REQUIRED")
    try:
        grids = list(native_config_grids)
    except TypeError as exc:
        raise FieldUpdateError("NATIVE_CONFIG_GRIDS_REQUIRED") from exc
    keys: dict[str, int] = {}
    seen_rows: set[int] = set()
    for grid in grids:
        if not isinstance(grid, Mapping):
            raise FieldUpdateError("NATIVE_CONFIG_GRID_INVALID")
        start_row = _integer(grid.get("startRow", 0), 0, "CONFIG_ROW_INVALID")
        start_column = _integer(grid.get("startColumn", 0), 0, "CONFIG_COLUMN_INVALID")
        if start_column != 0:
            raise FieldUpdateError("CONFIG_COLUMN_A_REQUIRED")
        rows = grid.get("rowData")
        if not isinstance(rows, list):
            raise FieldUpdateError("NATIVE_CONFIG_ROWS_REQUIRED")
        for offset, native_row in enumerate(rows):
            row = start_row + offset + 1
            if row in seen_rows:
                raise FieldUpdateError("OVERLAPPING_CONFIG_ROWS")
            seen_rows.add(row)
            if not isinstance(native_row, Mapping):
                raise FieldUpdateError("NATIVE_CONFIG_ROW_INVALID")
            cells = native_row.get("values", [])
            if not isinstance(cells, list):
                raise FieldUpdateError("NATIVE_CONFIG_VALUES_REQUIRED")
            if not cells:
                continue
            cell = cells[0]
            if not isinstance(cell, Mapping):
                raise FieldUpdateError("NATIVE_CONFIG_KEY_CELL_INVALID")
            typed = cell.get("effectiveValue", cell.get("userEnteredValue"))
            if not typed:
                if cell.get("formattedValue"):
                    raise FieldUpdateError("NATIVE_CONFIG_KEY_TYPED_VALUE_MISSING")
                continue
            if not isinstance(typed, Mapping) or set(typed) != {"stringValue"}:
                raise FieldUpdateError("NATIVE_CONFIG_KEY_MUST_BE_STRING")
            key = typed["stringValue"]
            if not isinstance(key, str):
                raise FieldUpdateError("NATIVE_CONFIG_KEY_MUST_BE_STRING")
            if key == "":
                continue
            if not key.strip():
                raise FieldUpdateError("NATIVE_CONFIG_BLANK_KEY")
            if key in keys:
                raise FieldUpdateError("DUPLICATE_CONFIG_KEY:" + key)
            keys[key] = row
    return keys


def build_config_updates(*, sheet_id: int,
                         native_config_grids: Iterable[Mapping[str, Any]],
                         expected_key_rows: Mapping[str, int],
                         updates: Mapping[str, Mapping[str, Any]],
                         allowed_fields: Mapping[str, Iterable[str]]) -> list[dict]:
    """Plan explicit key -> {value, note} edits for owned Config B/C cells.

    Capture expected_key_rows from the native key read used for preparation;
    pass freshly read native grids again under the existing commit protocol.
    Each targeted key must still occupy its captured row. A changed/missing key
    fails before any request is built. allowed_fields explicitly maps each
    owned key to its permitted fields: "value" (B) and/or "note" (C).

    Each output delegates unchanged Python literal types to the original
    one-based cell_update builder. Column A, gap cells and unspecified fields
    are never written. This pure planner supplies no field ownership, lease,
    compare-and-swap, health decision or external-action authority.
    """
    _integer(sheet_id, 0, "SHEET_ID_INVALID")
    if not isinstance(updates, Mapping):
        raise FieldUpdateError("NAMED_CONFIG_UPDATES_REQUIRED")
    if not isinstance(expected_key_rows, Mapping):
        raise FieldUpdateError("EXPECTED_CONFIG_KEYS_REQUIRED")
    if not isinstance(allowed_fields, Mapping):
        raise FieldUpdateError("EXPLICIT_CONFIG_ALLOWLIST_REQUIRED")
    columns = {"value": 2, "note": 3}
    ownership: dict[str, set[str]] = {}
    for key, fields in allowed_fields.items():
        if not isinstance(key, str) or not key.strip():
            raise FieldUpdateError("ALLOWLIST_CONFIG_KEY_INVALID")
        if isinstance(fields, (str, bytes)):
            raise FieldUpdateError("EXPLICIT_CONFIG_FIELD_ALLOWLIST_REQUIRED")
        try:
            field_list = list(fields)
        except TypeError as exc:
            raise FieldUpdateError("EXPLICIT_CONFIG_FIELD_ALLOWLIST_REQUIRED") from exc
        if any(not isinstance(field, str) or field not in columns for field in field_list):
            raise FieldUpdateError("CONFIG_ALLOWLIST_FIELD_INVALID")
        ownership[key] = set(field_list)
    for key, row in expected_key_rows.items():
        if not isinstance(key, str) or not key.strip():
            raise FieldUpdateError("EXPECTED_CONFIG_KEY_INVALID")
        _integer(row, 2, "EXPECTED_CONFIG_ROW_INVALID")
    rows = config_key_rows(native_config_grids)
    planned: list[tuple[int, int, Any]] = []
    for key, fields in updates.items():
        if not isinstance(key, str) or not key.strip():
            raise FieldUpdateError("EXACT_CONFIG_KEY_REQUIRED")
        if key not in ownership:
            raise FieldUpdateError("CONFIG_KEY_NOT_OWNED:" + key)
        if key not in expected_key_rows:
            raise FieldUpdateError("EXPECTED_CONFIG_KEY_MISSING:" + key)
        if key not in rows:
            raise FieldUpdateError("CONFIG_KEY_MISSING:" + key)
        if rows[key] != expected_key_rows[key]:
            raise FieldUpdateError("CONFIG_EXPECTED_KEY_CHANGED:" + key)
        if not isinstance(fields, Mapping):
            raise FieldUpdateError("NAMED_CONFIG_VALUE_NOTE_REQUIRED")
        for field, value in fields.items():
            if not isinstance(field, str) or field not in columns:
                raise FieldUpdateError("CONFIG_FIELD_INVALID")
            if field not in ownership[key]:
                raise FieldUpdateError("CONFIG_FIELD_NOT_OWNED:" + key + ":" + field)
            _value(value)
            planned.append((rows[key], columns[field], value))
    return [original_builder.cell_update(sheet_id, row, column, value)
            for row, column, value in sorted(planned, key=lambda item: item[:2])]
