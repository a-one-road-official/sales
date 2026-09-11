from __future__ import annotations


import hashlib
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urlparse


from google.auth import default
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


from models import Source
from safety import canonicalize_url




_CONFIG_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_CONFIG_CACHE_LOCK = threading.Lock()
_CONFIG_CACHE_TTL_SECONDS = 30.0


SOURCE_HEADERS = [
    "source_id","source_type","source_name","source_url","country","event_year",
    "exhibitor_directory_url","first_discovered_at","last_crawled_at","crawl_status",
    "exhibitor_count","last_error",
]




class SheetsRepo:
    def __init__(self, spreadsheet_id: str):
        self._read_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._last_read_at = 0.0
        self._last_write_at = 0.0
        self._read_cache: dict[str, tuple[float, list[list[str]]]] = {}
        try:
            self._read_min_interval = max(
                0.2,
                min(5.0, float(os.getenv("LEAD_FACTORY_SHEETS_READ_MIN_INTERVAL_SECONDS", "1.05") or 1.05)),
            )
        except ValueError:
            self._read_min_interval = 1.05
        try:
            self._read_cache_ttl = max(
                0.0,
                min(30.0, float(os.getenv("LEAD_FACTORY_SHEETS_READ_CACHE_TTL_SECONDS", "3") or 3)),
            )
        except ValueError:
            self._read_cache_ttl = 3.0
        try:
            self._write_min_interval = max(
                0.2,
                min(5.0, float(os.getenv("LEAD_FACTORY_SHEETS_WRITE_MIN_INTERVAL_SECONDS", "0.25") or 0.25)),
            )
        except ValueError:
            self._write_min_interval = 0.25
        creds, _ = default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
        self.svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.spreadsheet_id = spreadsheet_id


    def read(self, range_: str) -> list[list[str]]:
        # Sheets quota is shared by the runtime identity. Serialize reads per
        # container, reuse very recent identical reads, and back off on transient
        # quota/server responses so parallel Scheduler ticks do not turn a
        # recoverable burst into a dead pipeline.
        with self._read_lock:
            now = time.monotonic()
            cached = self._read_cache.get(range_)
            if cached and self._read_cache_ttl and now - cached[0] < self._read_cache_ttl:
                return [list(row) for row in cached[1]]
            for attempt in range(5):
                elapsed = time.monotonic() - self._last_read_at
                if elapsed < self._read_min_interval:
                    time.sleep(self._read_min_interval - elapsed)
                try:
                    res = self.svc.spreadsheets().values().get(
                        spreadsheetId=self.spreadsheet_id, range=range_
                    ).execute()
                    self._last_read_at = time.monotonic()
                    values = res.get("values", [])
                    self._read_cache[range_] = (self._last_read_at, [list(row) for row in values])
                    return [list(row) for row in values]
                except HttpError as exc:
                    status = getattr(exc.resp, "status", None)
                    if status not in {429, 500, 502, 503, 504} or attempt == 4:
                        raise
                    # Sheets' per-user read quota is a one-minute window.
                    # Short retries only reproduce 429s while concurrent internal
                    # ticks are active, so let the read retry cross that window.
                    delay = min(70.0, 15.0 * (attempt + 1)) if status == 429 else min(8.0, 2 ** attempt)
                    time.sleep(delay)
            return []


    def read_once(self, range_: str) -> list[list[str]]:
        """Read one bounded range once; callers own retry policy."""
        with self._read_lock:
            now = time.monotonic()
            cached = self._read_cache.get(range_)
            if cached and self._read_cache_ttl and now - cached[0] < self._read_cache_ttl:
                return [list(row) for row in cached[1]]
            elapsed = time.monotonic() - self._last_read_at
            if elapsed < self._read_min_interval:
                time.sleep(self._read_min_interval - elapsed)
            res = self.svc.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=range_
            ).execute()
            self._last_read_at = time.monotonic()
            values = res.get("values", [])
            self._read_cache[range_] = (self._last_read_at, [list(row) for row in values])
            return [list(row) for row in values]


    def _execute_write(self, operation):
        """Serialize writes and retry transient Sheets quota/server responses."""
        with self._write_lock:
            for attempt in range(5):
                elapsed = time.monotonic() - self._last_write_at
                if elapsed < self._write_min_interval:
                    time.sleep(self._write_min_interval - elapsed)
                try:
                    result = operation()
                    self._last_write_at = time.monotonic()
                    with self._read_lock:
                        self._read_cache.clear()
                    return result
                except HttpError as exc:
                    status = getattr(exc.resp, "status", None)
                    if status not in {429, 500, 502, 503, 504} or attempt == 4:
                        raise
                    time.sleep(min(8.0, 2 ** attempt))
        return None


    def _bounded_append_range(self, sheet: str, width: int) -> str:
        """Return the narrowest append range required by the payload.

        Using A:ZZ on every technical append caused Google Sheets to physically
        expand several machine tabs to hundreds of columns and eventually hit
        the workbook-wide 10M-cell ceiling. Bound appends to the actual payload.
        """
        width = max(1, int(width or 1))
        last_col = self._column_letter(width)
        escaped = str(sheet).replace("'", "''")
        return f"'{escaped}'!A:{last_col}"

    def append(self, sheet: str, values: list) -> None:
        # Human-facing SSOT writes must go through promotion validation and the
        # append-structure writer. Raw list appends cannot prove Gate/added_at
        # invariants and are therefore rejected at the repository boundary.
        if sheet == "営業リスト＿Factory/BPO":
            raise RuntimeError("direct_human_ssot_append_blocked:use_promote_to_sales_if_new")
        append_range = self._bounded_append_range(sheet, len(values))
        self._execute_write(lambda: self.svc.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=append_range,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [values]},
        ).execute())
        if sheet == "Config":
            self.invalidate_config_cache()


    def append_rows(self, sheet: str, values_rows: list[list]) -> None:
        """Append multiple technical rows in one Sheets API call."""
        if not values_rows:
            return
        if sheet == "営業リスト＿Factory/BPO":
            raise RuntimeError("direct_human_ssot_append_blocked:use_batch_promotion")
        width = max((len(row) for row in values_rows), default=1)
        append_range = self._bounded_append_range(sheet, width)
        self._execute_write(lambda: self.svc.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=append_range,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values_rows},
        ).execute())
        if sheet == "Config":
            self.invalidate_config_cache()


    def append_dict(self, sheet: str, row: dict) -> None:
        headers_rows = self.read(f"{sheet}!1:1")
        if not headers_rows:
            raise RuntimeError(f"missing_header:{sheet}")
        headers = headers_rows[0]
        if sheet == "営業リスト＿Factory/BPO":
            from promotion_accounting import validate_new_sales_payload
            validate_new_sales_payload(row, headers)
        ordered = [row.get(h, "") for h in headers]
        self.append(sheet, ordered)




    def ensure_outreach_schema(self) -> None:
        """Ensure provenance/status columns exist without changing existing draft history."""
        for sheet, headers in {
            "LeadFactory_MessageDrafts": (
                "prompt_doc_title", "prompt_doc_id", "prompt_modified_time",
                "prompt_hash", "prompt_status", "stale_reason", "regeneration_error",
            ),
            "LeadFactory_ApprovalQueue": (
                "prompt_doc_title", "prompt_doc_id", "prompt_modified_time",
                "prompt_hash", "prompt_status",
            ),
        }.items():
            for header in headers:
                self._ensure_header(sheet, header)


    def _update_dict_fields(self, sheet: str, row_number: int, changes: dict) -> None:
        if not changes:
            return
        headers_rows = self.read(f"{sheet}!1:1")
        headers = headers_rows[0] if headers_rows else []
        for key in changes:
            if key not in headers:
                self._ensure_header(sheet, key)
        headers_rows = self.read(f"{sheet}!1:1")
        headers = headers_rows[0] if headers_rows else []
        header_index = {str(header): index for index, header in enumerate(headers) if header}
        data = []
        for key, value in changes.items():
            if key not in header_index:
                continue
            col = self._column_letter(header_index[key] + 1)
            data.append({"range": f"'{sheet}'!{col}{int(row_number)}", "values": [[value]]})
        if data:
            self._execute_write(lambda: self.svc.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data},
            ).execute())


    @staticmethod
    def _column_letter(index_1_based: int) -> str:
        n = int(index_1_based)
        if n < 1:
            raise ValueError("column_index_must_be_positive")
        out = ""
        while n:
            n, rem = divmod(n - 1, 26)
            out = chr(65 + rem) + out
        return out


    def append_dict_preserving_previous_row_structure(self, sheet: str, row: dict, min_row: int | None = None) -> int:
        with self._write_lock:
            """Write a new human-facing lead into the first empty SSOT row.


            Invariants:
            - Never append at the physical sheet tail merely because formulas exist there.
            - For the sales SSOT, start at the configured human append anchor and fill downward.
            - Preserve existing formulas/CRM values in unrelated columns.
            - Copy only user-entered format + data validation from the nearest prior populated lead row.
            - Write only fields explicitly present in ``row``.
            """
            headers_rows = self.read(f"'{sheet}'!1:1")
            if not headers_rows:
                raise RuntimeError(f"missing_header:{sheet}")
            headers = headers_rows[0]
            header_index = {str(h): i for i, h in enumerate(headers) if h}
            cfg = self.get_config()
            is_human_ssot = sheet == cfg.get("LEAD_FACTORY_HUMAN_SSOT_SHEET", "営業リスト＿Factory/BPO")
            if is_human_ssot:
                from promotion_accounting import validate_new_sales_payload
                validate_new_sales_payload(row, headers)


            cfg = cfg if is_human_ssot else {}
            if min_row is None:
                try:
                    min_row = int(cfg.get("LEAD_FACTORY_HUMAN_APPEND_MIN_ROW", "2") or 2)
                except Exception:
                    min_row = 2
            min_row = max(2, int(min_row))


            meta = self.svc.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id,
                fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))",
            ).execute()
            props = None
            for item in meta.get("sheets", []):
                p = item.get("properties", {})
                if p.get("title") == sheet:
                    props = p
                    break
            if props is None:
                raise RuntimeError(f"missing_sheet:{sheet}")


            sheet_id = int(props["sheetId"])
            row_count = int(props.get("gridProperties", {}).get("rowCount", 0) or 0)
            if min_row > row_count:
                self.svc.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"requests": [{
                        "appendDimension": {
                            "sheetId": sheet_id, "dimension": "ROWS", "length": min_row - row_count + 100
                        }
                    }]},
                ).execute()
                row_count = min_row + 99


            # Find the first empty company-name cell from the human append anchor downward.
            scan = self.read(f"'{sheet}'!A{min_row}:A{row_count}")
            new_row_number = min_row
            for offset in range(max(0, row_count - min_row + 1)):
                value = ""
                if offset < len(scan) and scan[offset]:
                    value = str(scan[offset][0] or "").strip()
                if not value:
                    new_row_number = min_row + offset
                    break
            else:
                new_row_number = row_count + 1
                self.svc.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"requests": [{
                        "appendDimension": {"sheetId": sheet_id, "dimension": "ROWS", "length": 100}
                    }]},
                ).execute()


            # Nearest prior populated human lead row is the structure template.
            prior = self.read(f"'{sheet}'!A1:A{new_row_number - 1}")
            template_row = 1
            for idx in range(len(prior) - 1, -1, -1):
                if prior[idx] and str(prior[idx][0] or "").strip():
                    template_row = idx + 1
                    break


            last_col = self._column_letter(len(headers))
            structure = self.svc.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id,
                ranges=[f"'{sheet}'!A{template_row}:{last_col}{template_row}"],
                includeGridData=True,
                fields="sheets(data(rowData(values(userEnteredFormat,dataValidation))))",
            ).execute()
            cells = []
            try:
                cells = structure["sheets"][0]["data"][0]["rowData"][0].get("values", [])
            except Exception:
                cells = []
            if cells:
                formatted = []
                for i in range(len(headers)):
                    src = cells[i] if i < len(cells) else {}
                    dst = {}
                    if "userEnteredFormat" in src:
                        dst["userEnteredFormat"] = src["userEnteredFormat"]
                    if "dataValidation" in src:
                        dst["dataValidation"] = src["dataValidation"]
                    formatted.append(dst)
                self.svc.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"requests": [{
                        "updateCells": {
                            "start": {"sheetId": sheet_id, "rowIndex": new_row_number - 1, "columnIndex": 0},
                            "rows": [{"values": formatted}],
                            "fields": "userEnteredFormat,dataValidation",
                        }
                    }]},
                ).execute()


            # Only touch explicitly supplied fields; never blank unrelated CRM/formula columns.
            data = []
            for key, value in row.items():
                if key not in header_index:
                    continue
                col = self._column_letter(header_index[key] + 1)
                data.append({"range": f"'{sheet}'!{col}{new_row_number}", "values": [[value]]})
            if data:
                self.svc.spreadsheets().values().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"valueInputOption": "RAW", "data": data},
                ).execute()
            return new_row_number


    def append_rows_preserving_previous_row_structure(
        self, sheet: str, rows: list[dict], start_row: int
    ) -> tuple[int, int]:
        """Write qualified rows contiguously at the SSOT bottom in bounded batches."""
        if not rows:
            return (0, 0)
        with self._write_lock:
            headers_rows = self.read(f"'{sheet}'!1:1")
            if not headers_rows:
                raise RuntimeError(f"missing_header:{sheet}")
            headers = [str(h or "").strip() for h in headers_rows[0]]
            header_index = {h: i for i, h in enumerate(headers) if h}
            if sheet == "営業リスト＿Factory/BPO":
                from promotion_accounting import validate_new_sales_payload
                for row in rows:
                    validate_new_sales_payload(row, headers)

            start = max(2, int(start_row))
            end = start + len(rows) - 1
            meta = self.svc.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id,
                fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))",
            ).execute()
            props = next(
                (
                    item.get("properties", {})
                    for item in meta.get("sheets", [])
                    if item.get("properties", {}).get("title") == sheet
                ),
                None,
            )
            if props is None:
                raise RuntimeError(f"missing_sheet:{sheet}")
            sheet_id = int(props["sheetId"])
            row_count = int(props.get("gridProperties", {}).get("rowCount", 0) or 0)
            if end > row_count:
                self.svc.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"requests": [{
                        "appendDimension": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "length": end - row_count + 100,
                        }
                    }]},
                ).execute()

            prior = self.read(f"'{sheet}'!A1:A{max(1, start - 1)}")
            template_row = 1
            for idx in range(len(prior) - 1, -1, -1):
                if prior[idx] and str(prior[idx][0] or "").strip():
                    template_row = idx + 1
                    break
            source = {
                "sheetId": sheet_id,
                "startRowIndex": template_row - 1,
                "endRowIndex": template_row,
                "startColumnIndex": 0,
                "endColumnIndex": len(headers),
            }
            destination = {
                "sheetId": sheet_id,
                "startRowIndex": start - 1,
                "endRowIndex": end,
                "startColumnIndex": 0,
                "endColumnIndex": len(headers),
            }
            self.svc.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": [
                    {"copyPaste": {"source": source, "destination": destination, "pasteType": "PASTE_FORMAT"}},
                    {"copyPaste": {"source": source, "destination": destination, "pasteType": "PASTE_DATA_VALIDATION"}},
                ]},
            ).execute()

            last_col = self._column_letter(len(headers))
            values = [[row.get(header, "") for header in headers] for row in rows]
            # Keep each request comfortably below Sheets payload and timeout limits.
            for offset in range(0, len(values), 250):
                chunk = values[offset:offset + 250]
                chunk_start = start + offset
                chunk_end = chunk_start + len(chunk) - 1
                self.svc.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"'{sheet}'!A{chunk_start}:{last_col}{chunk_end}",
                    valueInputOption="RAW",
                    body={"values": chunk},
                ).execute()
            return (start, end)


    def update_row(self, sheet: str, row_number: int, values: list) -> None:
        self._execute_write(lambda: self.svc.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"{sheet}!A{row_number}:ZZ{row_number}",
            valueInputOption="RAW",
            body={"values": [values]},
        ).execute())


    def update_range(self, range_: str, values: list[list]) -> None:
        """Narrow values update used to preserve unrelated formulas/validation."""
        self._execute_write(lambda: self.svc.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=range_,
            valueInputOption="RAW",
            body={"values": values},
        ).execute())
        if str(range_).startswith("Config!") or str(range_).startswith("'Config'!"):
            self.invalidate_config_cache()


    def _ensure_header(self, sheet: str, header: str) -> None:
        """Add one machine column when a resilient storage field is first used."""
        headers_rows = self.read(f"{sheet}!1:1")
        headers = headers_rows[0] if headers_rows else []
        if header in headers:
            return
        meta = self.svc.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id,
            fields="sheets(properties(sheetId,title,gridProperties(columnCount)))",
        ).execute()
        sheet_id = None
        column_count = 0
        for item in meta.get("sheets", []):
            props = item.get("properties", {})
            if props.get("title") == sheet:
                sheet_id = props.get("sheetId")
                column_count = int((props.get("gridProperties") or {}).get("columnCount") or 0)
                break
        next_column = len(headers) + 1
        if sheet_id is None:
            raise RuntimeError(f"missing_sheet:{sheet}")
        if next_column > column_count:
            self.svc.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": [{
                    "appendDimension": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "length": next_column - column_count,
                    }
                }]},
            ).execute()
        self.update_range(
            f"{sheet}!{self._column_letter(next_column)}1",
            [[header]],
        )


    def read_formulas(self, range_: str) -> list[list[str]]:
        res = self.svc.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=range_,
            valueRenderOption="FORMULA",
        ).execute()
        return res.get("values", [])


    def upsert_dict(self, sheet: str, key_header: str, key_value: str, row: dict) -> None:
        headers_rows = self.read(f"{sheet}!1:1")
        if not headers_rows:
            raise RuntimeError(f"missing_header:{sheet}")
        headers = headers_rows[0]
        if key_header not in headers:
            raise RuntimeError(f"missing_key_header:{sheet}:{key_header}")
        key_idx = headers.index(key_header)
        rows = self.read(f"{sheet}!A2:ZZ")
        ordered = [row.get(h, "") for h in headers]
        for i, existing in enumerate(rows, start=2):
            if len(existing) > key_idx and str(existing[key_idx]) == str(key_value):
                self.update_row(sheet, i, ordered)
                return
        self.append(sheet, ordered)


    def meta_log(self, row: dict) -> None:
        self.append_dict("LeadFactory_MetaLog", row)


    def get_config(self) -> dict[str, str]:
        now = time.monotonic()
        cache_key = self.spreadsheet_id
        with _CONFIG_CACHE_LOCK:
            cached = _CONFIG_CACHE.get(cache_key)
            if cached and now - cached[0] < _CONFIG_CACHE_TTL_SECONDS:
                return dict(cached[1])
        rows = self.read("Config!A2:B1000")
        config = {str(r[0]): str(r[1]) for r in rows if len(r) >= 2 and r[0]}
        with _CONFIG_CACHE_LOCK:
            _CONFIG_CACHE[cache_key] = (time.monotonic(), config)
        return dict(config)

    def invalidate_config_cache(self) -> None:
        with _CONFIG_CACHE_LOCK:
            _CONFIG_CACHE.pop(self.spreadsheet_id, None)


    def list_sources(self) -> list[Source]:
        rows = self.read("LeadFactory_Sources!A2:L")
        out: list[Source] = []
        for r in rows:
            if not r:
                continue
            padded = r + [""] * (12 - len(r))
            out.append(Source(
                source_id=padded[0], source_type=padded[1], source_name=padded[2],
                source_url=padded[3], country=padded[4], event_year=padded[5],
                exhibitor_directory_url=padded[6], last_crawled_at=padded[8], crawl_status=padded[9],
                exhibitor_count=int(padded[10]) if str(padded[10]).isdigit() else None,
                last_error=padded[11],
            ))
        return out


    def source_by_id(self, source_id: str) -> Source:
        for s in self.list_sources():
            if s.source_id == source_id:
                return s
        raise KeyError(source_id)


    def sources_for_crawl(
        self,
        limit: int = 20,
        *,
        lane: str | None = None,
        recrawl_after_minutes: int = 1440,
    ) -> list[Source]:
        """Return a bounded least-recently-crawled queue for one supply lane.

        - mittelstand: source_type starts with MITTELSTAND_
        - growth: every other source type

        Newly discovered/retry sources run first. Completed healthy sources
        become eligible again only after `recrawl_after_minutes`. This rotates
        across source universes instead of repeatedly hammering one directory.
        """
        blocked = {"AUTH_REQUIRED", "CIRCUIT_OPEN", "DISABLED"}
        priority = {
            "DISCOVERED": 0,
            "READY": 0,
            "RETRY": 1,
            "ERROR": 1,
            "DEGRADED": 1,
            "RAW_CAPTURED_GATE_PENDING": 2,
            "CRAWLED": 2,
            "HEALTHY": 2,
        }
        lane_norm = (lane or "").strip().lower()
        now = datetime.now(timezone.utc)

        def lane_ok(src: Source) -> bool:
            is_mittel = str(src.source_type or "").upper().startswith("MITTELSTAND_")
            if lane_norm == "mittelstand":
                return is_mittel
            if lane_norm == "growth":
                return not is_mittel
            return True

        def due(src: Source) -> bool:
            status = str(src.crawl_status or "").upper()
            if status in {"DISCOVERED", "READY", "RETRY", "ERROR", "DEGRADED", ""}:
                return True
            raw = str(src.last_crawled_at or "").strip()
            if not raw:
                return True
            try:
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_min = (now - ts.astimezone(timezone.utc)).total_seconds() / 60.0
                return age_min >= max(1, int(recrawl_after_minutes))
            except Exception:
                return True

        candidates = [
            src for src in self.list_sources()
            if str(src.crawl_status or "").upper() not in blocked and lane_ok(src) and due(src)
        ]
        candidates.sort(key=lambda src: (
            priority.get(str(src.crawl_status or "").upper(), 1),
            str(src.last_crawled_at or ""),
            src.source_id,
        ))
        return candidates[:max(0, int(limit))]


    def update_source_crawl_state(
        self,
        source_id: str,
        *,
        crawl_status: str,
        exhibitor_count: int | None = None,
        last_error: str = "",
        last_crawled_at: str | None = None,
    ) -> dict:
        """Narrow-write source crawl state to I:L only.


        The discovery metadata in A:H is immutable here.
        """
        rows = self.read("LeadFactory_Sources!A2:L")
        target_row = None
        current = None
        for row_number, r in enumerate(rows, start=2):
            padded = r + [""] * (12 - len(r))
            if str(padded[0]) == str(source_id):
                target_row = row_number
                current = padded
                break
        if target_row is None or current is None:
            raise KeyError(source_id)
        ts = last_crawled_at or datetime.now(timezone.utc).isoformat()
        count_value = "" if exhibitor_count is None else int(exhibitor_count)
        self.update_range(
            f"LeadFactory_Sources!I{target_row}:L{target_row}",
            [[ts, str(crawl_status), count_value, str(last_error or "")[:5000]]],
        )
        return {
            "source_id": source_id,
            "row_number": target_row,
            "last_crawled_at": ts,
            "crawl_status": str(crawl_status),
            "exhibitor_count": count_value,
            "last_error": str(last_error or "")[:5000],
        }


    def add_source_if_new(self, candidate: dict) -> tuple[bool, str]:
        url = canonicalize_url(candidate.get("exhibitor_directory_url") or candidate.get("source_url") or "")
        if not url:
            return False, ""
        existing = {canonicalize_url(s.source_url): s.source_id for s in self.list_sources()}
        if url in existing:
            return False, existing[url]
        sid = "source-" + hashlib.sha256(url.encode()).hexdigest()[:20]
        now = datetime.now(timezone.utc).isoformat()
        row = [
            sid,
            candidate.get("source_type", "EXHIBITION"),
            candidate.get("source_name", ""),
            url,
            candidate.get("country", ""),
            str(candidate.get("event_year", "")),
            candidate.get("exhibitor_directory_url", url),
            now,
            "",
            "DISCOVERED",
            "",
            "",
        ]
        self.append("LeadFactory_Sources", row)
        return True, sid


    @staticmethod
    def _normalize_domain(value: str) -> str:
        raw = (value or "").strip()
        if not raw:
            return ""
        candidate = raw if "://" in raw else f"https://{raw}"
        try:
            host = (urlparse(candidate).hostname or "").lower().rstrip(".")
        except Exception:
            return ""
        return host.removeprefix("www.")


    @staticmethod
    def _normalize_name(value: str) -> str:
        return " ".join((value or "").strip().lower().split())


    def append_raw_records(self, source: Source, records: Iterable[dict]) -> tuple[int, int]:
        """Add only genuinely new companies to LeadFactory_Raw.


        Duplicate checks are deterministic and happen before append:
        1) existing LeadFactory_Raw (normalized domain, then normalized name)
        2) existing Sales SSOT `営業リスト＿Factory/BPO` (website domain, then company name)


        New rows are written with all intake-control columns populated, so the
        acquisition layer does not depend on fragile ARRAYFORMULA expansion.
        """
        existing_raw_rows = self.read("LeadFactory_Raw!A2:R")
        raw_domains: set[str] = set()
        raw_names: set[str] = set()
        for r in existing_raw_rows:
            padded = r + [""] * (18 - len(r))
            name_key = self._normalize_name(padded[1])
            domain_key = self._normalize_domain(padded[15] or padded[2] or padded[3])
            if name_key:
                raw_names.add(name_key)
            if domain_key:
                raw_domains.add(domain_key)


        sales_rows = self.read("'営業リスト＿Factory/BPO'!A2:G")
        sales_domains: set[str] = set()
        sales_names: set[str] = set()
        for r in sales_rows:
            padded = r + [""] * (7 - len(r))
            name_key = self._normalize_name(padded[0])
            domain_key = self._normalize_domain(padded[6])
            if name_key:
                sales_names.add(name_key)
            if domain_key:
                sales_domains.add(domain_key)


        new_count = 0
        dup_count = 0
        now = datetime.now(timezone.utc).isoformat()


        for rec in records:
            name = (rec.get("company_name") or "").strip()
            if not name:
                continue


            name_key = self._normalize_name(name)
            domain = self._normalize_domain(rec.get("domain") or rec.get("website") or "")
            website = (rec.get("website") or "").strip()
            if not website and domain:
                website = f"https://{domain}"


            exists_in_raw = (domain and domain in raw_domains) or (not domain and name_key in raw_names)
            exists_in_sales = (domain and domain in sales_domains) or (not domain and name_key in sales_names)
            if exists_in_raw or exists_in_sales:
                dup_count += 1
                continue


            stable_key = domain or name_key
            lead_id = "lead-" + hashlib.sha256(f"{source.source_id}|{stable_key}".encode()).hexdigest()[:24]
            if not domain:
                intake_status = "NEEDS_DOMAIN"
            elif str(source.source_type).upper().startswith("MITTELSTAND_"):
                intake_status = "READY_FOR_MITTELSTAND_GATE"
            else:
                intake_status = "READY_FOR_GATE"


            self.append("LeadFactory_Raw", [
                lead_id,                       # A lead_id
                name,                          # B company_name
                domain,                        # C domain
                website,                       # D website
                "",                            # E hq_country
                source.source_type,            # F source_type
                source.source_name,            # G source_name
                source.source_url,             # H source_url
                rec.get("source_record_url", source.crawl_url),  # I
                now,                           # J discovered_at
                now,                           # K last_seen_at
                "PENDING",                     # L screening_status
                "",                            # M last_screened_at
                "",                            # N gate_version
                "",                            # O error
                domain,                        # P normalized_domain
                "NEW",                         # Q duplicate_state
                intake_status,                 # R intake_status
            ])
            new_count += 1
            raw_names.add(name_key)
            if domain:
                raw_domains.add(domain)


        return new_count, dup_count




    def find_raw_match(self, company_name: str, domain_or_website: str = "") -> dict | None:
        headers_rows = self.read("LeadFactory_Raw!1:1")
        if not headers_rows:
            return None
        headers = headers_rows[0]
        rows = self.read("LeadFactory_Raw!A2:R")
        target_domain = self._normalize_domain(domain_or_website)
        target_name = self._normalize_name(company_name)
        for row_number, r in enumerate(rows, start=2):
            padded = r + [""] * max(0, len(headers) - len(r))
            d = dict(zip(headers, padded))
            existing_domain = self._normalize_domain(d.get("normalized_domain") or d.get("domain") or d.get("website") or "")
            existing_name = self._normalize_name(d.get("company_name", ""))
            if (target_domain and existing_domain == target_domain) or (not target_domain and target_name and existing_name == target_name):
                d["row_number"] = row_number
                return d
        return None


    def append_trigger_signals(self, signals: Iterable[dict]) -> dict:
        existing_rows = self.read("LeadFactory_TriggerSignals!A2:P")
        existing_ids = {str(r[0]) for r in existing_rows if r}
        new_signals = matched_raw = created_raw = unresolved = duplicates = 0
        details = []
        now = datetime.now(timezone.utc).isoformat()


        for signal in signals:
            company_name = str(signal.get("company_name") or "").strip()
            record_url = str(signal.get("source_record_url") or signal.get("source_url") or "").strip()
            if not company_name or not record_url:
                unresolved += 1
                continue
            domain = self._normalize_domain(signal.get("domain") or signal.get("website") or "")
            website = str(signal.get("website") or "").strip() or (f"https://{domain}" if domain else "")
            stable = "|".join([
                self._normalize_name(company_name),
                domain,
                str(signal.get("signal_type") or "").upper(),
                str(signal.get("signal_date") or ""),
                record_url,
            ])
            signal_id = "signal-" + hashlib.sha256(stable.encode()).hexdigest()[:24]
            if signal_id in existing_ids:
                duplicates += 1
                continue


            raw = self.find_raw_match(company_name, domain or website)
            resolution = "MATCHED_RAW" if raw else ""
            raw_lead_id = str(raw.get("lead_id", "")) if raw else ""
            if raw:
                matched_raw += 1
            else:
                pseudo_source = Source(
                    source_id=f"trigger-{signal_id}",
                    source_type="TRIGGER_SIGNAL",
                    source_name=str(signal.get("source_name") or "Trigger Signal Discovery"),
                    source_url=str(signal.get("source_url") or record_url),
                    exhibitor_directory_url=str(signal.get("source_url") or record_url),
                )
                added, _ = self.append_raw_records(pseudo_source, [{
                    "company_name": company_name,
                    "website": website,
                    "domain": domain,
                    "source_record_url": record_url,
                }])
                raw = self.find_raw_match(company_name, domain or website)
                if raw is None and not domain:
                    raw = self.find_raw_match(company_name, "")
                raw_lead_id = str(raw.get("lead_id", "")) if raw else ""
                if added and raw_lead_id:
                    resolution = "CREATED_RAW" if domain else "CREATED_RAW_NEEDS_DOMAIN"
                    created_raw += 1
                elif raw_lead_id:
                    resolution = "MATCHED_RAW"
                    matched_raw += 1
                else:
                    resolution = "UNRESOLVED"
                    unresolved += 1


            row = {
                "signal_id": signal_id,
                "company_name": company_name,
                "domain": domain,
                "website": website,
                "signal_type": str(signal.get("signal_type") or "").upper(),
                "signal_date": str(signal.get("signal_date") or ""),
                "signal_strength": str(signal.get("signal_strength") or "LOW").upper(),
                "headline": str(signal.get("headline") or ""),
                "source_name": str(signal.get("source_name") or ""),
                "source_url": str(signal.get("source_url") or record_url),
                "source_record_url": record_url,
                "discovered_at": now,
                "entity_resolution": resolution,
                "raw_lead_id": raw_lead_id,
                "error": "",
                "notes": str(signal.get("notes") or ""),
            }
            self.append_dict("LeadFactory_TriggerSignals", row)
            existing_ids.add(signal_id)
            new_signals += 1
            details.append({"signal_id": signal_id, "company_name": company_name, "resolution": resolution, "raw_lead_id": raw_lead_id})


        return {
            "status": "COMPLETE",
            "new_signals": new_signals,
            "matched_raw": matched_raw,
            "created_raw": created_raw,
            "unresolved": unresolved,
            "duplicates": duplicates,
            "details": details,
        }


    def _human_ssot_config(self) -> tuple[str, int]:
        cfg = self.get_config()
        sheet = cfg.get("LEAD_FACTORY_HUMAN_SSOT_SHEET", "営業リスト＿Factory/BPO")
        try:
            min_row = int(cfg.get("LEAD_FACTORY_HUMAN_APPEND_MIN_ROW", "2887") or 2887)
        except Exception:
            min_row = 2887
        return sheet, max(2, min_row)

    def _find_sales_row_by_lf_lead_id(self, lead_id: str) -> int | None:
        lead_id = str(lead_id or "").strip()
        if not lead_id:
            return None
        sheet, _ = self._human_ssot_config()
        headers_rows = self.read(f"'{sheet}'!1:1")
        if not headers_rows or "LF_lead_id" not in headers_rows[0]:
            return None
        headers = headers_rows[0]
        idx = headers.index("LF_lead_id")
        last_col = self._column_letter(len(headers))
        rows = self.read(f"'{sheet}'!A2:{last_col}")
        for row_number, row in enumerate(rows, start=2):
            if len(row) > idx and str(row[idx] or "").strip() == lead_id:
                return row_number
        return None

    def _narrow_update_sales_fields(self, row_number: int, fields: dict) -> None:
        sheet, _ = self._human_ssot_config()
        headers_rows = self.read(f"'{sheet}'!1:1")
        if not headers_rows:
            raise RuntimeError(f"missing_header:{sheet}")
        headers = headers_rows[0]
        index = {str(h): i for i, h in enumerate(headers) if h}
        data = []
        for key, value in fields.items():
            if key not in index:
                continue
            col = self._column_letter(index[key] + 1)
            data.append({"range": f"'{sheet}'!{col}{row_number}", "values": [[value]]})
        if data:
            self.svc.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data},
            ).execute()

    def _refresh_sales_from_candidate(self, row_number: int, candidate: dict) -> None:
        """Refresh research/evidence on the existing intake row without making a second lead row."""
        values = {}
        evidence = []
        for key in ("research_sources", "evidence", "G1_evidence", "G2_evidence", "G3_evidence", "G4_evidence", "G5_evidence", "G6_evidence", "M1_evidence", "M2_evidence", "M3_evidence", "why_now_evidence"):
            value = candidate.get(key)
            if not value:
                continue
            for part in str(value).split(" | "):
                part = part.strip()
                if part and part not in evidence:
                    evidence.append(part)
        reason = candidate.get("selection_reason") or candidate.get("most_important_reason") or candidate.get("M3_reason") or candidate.get("G6_reason") or ""
        if reason:
            values["selection_reason"] = reason
        if evidence:
            values["research_sources"] = " | ".join(evidence)
        if candidate.get("evaluated_at"):
            values["reviewed_at"] = candidate.get("evaluated_at")
        if candidate.get("hq_country") or candidate.get("country"):
            values["hq_country"] = candidate.get("hq_country") or candidate.get("country")
            values["country"] = candidate.get("country") or candidate.get("hq_country")
        if candidate.get("website"):
            values["website"] = candidate.get("website")
        if candidate.get("domain"):
            values["original_domain"] = self._normalize_domain(candidate.get("domain"))
        self._narrow_update_sales_fields(row_number, values)


    def find_sales_match(self, company_name: str, domain_or_website: str = "") -> dict | None:
        """Return an existing SSOT row by canonical domain, falling back to name only when no domain exists.


        This is intentionally read-only. Promotion must never update an existing sales row or Status.
        """
        headers_rows = self.read("'営業リスト＿Factory/BPO'!1:1")
        if not headers_rows:
            raise RuntimeError("missing_header:営業リスト＿Factory/BPO")
        headers = headers_rows[0]
        name_idx = headers.index("company_name") if "company_name" in headers else 0
        website_idx = headers.index("website") if "website" in headers else None
        status_idx = headers.index("Status") if "Status" in headers else None
        target_domain = self._normalize_domain(domain_or_website)
        target_name = self._normalize_name(company_name)
        rows = self.read("'営業リスト＿Factory/BPO'!A2:ZZ")
        for row_number, r in enumerate(rows, start=2):
            padded = r + [""] * max(0, len(headers) - len(r))
            existing_name = self._normalize_name(padded[name_idx] if len(padded) > name_idx else "")
            existing_domain = ""
            if website_idx is not None and len(padded) > website_idx:
                existing_domain = self._normalize_domain(padded[website_idx])
            domain_match = bool(target_domain and existing_domain and target_domain == existing_domain)
            # Exact normalized company-name equality is always a valid duplicate block.
            # This prevents a new candidate with a resolved domain from duplicating a historic
            # SSOT row whose website was blank or stale. Domain remains the strongest key.
            name_fallback_match = bool(target_name and existing_name == target_name)
            if domain_match or name_fallback_match:
                return {
                    "row_number": row_number,
                    "company_name": padded[name_idx] if len(padded) > name_idx else "",
                    "status": padded[status_idx] if status_idx is not None and len(padded) > status_idx else "",
                    "domain": existing_domain,
                }
        return None


    def _sales_row_from_candidate(self, candidate: dict, now: str) -> dict:
        """Build the header-keyed human SSOT row after Gate approval."""
        def first_value(*keys: str):
            for key in keys:
                value = candidate.get(key)
                if value not in (None, "", [], {}):
                    return value
            return ""

        company_name = str(candidate.get("company_name") or candidate.get("original_company") or "").strip()
        domain = self._normalize_domain(candidate.get("domain") or candidate.get("website") or "")
        website = str(candidate.get("website") or "").strip()
        if not company_name:
            raise ValueError("missing_company_name")
        if not domain:
            raise ValueError("verified_official_domain_required")
        if not website:
            website = f"https://{domain}"

        evidence_parts = []
        for key in (
            "research_sources", "evidence",
            "G1_evidence", "G2_evidence", "G3_evidence", "G4_evidence", "G5_evidence", "G6_evidence",
            "M1_evidence", "M2_evidence", "M3_evidence", "why_now_evidence",
        ):
            value = candidate.get(key)
            if value in (None, "", [], {}):
                continue
            parts = value if isinstance(value, (list, tuple, set)) else str(value).split(" | ")
            for part in parts:
                part = str(part).strip()
                if part and part not in evidence_parts:
                    evidence_parts.append(part)

        g6_or_m3_result = str(
            first_value("G6_result", "M3_result", "japan_openness", "japan_presence") or ""
        ).strip()
        japan_evidence = first_value("G6_evidence", "M3_evidence", "japan_evidence_url")
        japan_reason = first_value("japan_opportunity_note", "G6_reason", "M3_reason")
        source_type = str(candidate.get("source_type") or "").strip().upper()
        lane_subcategory = "Mittelstand" if source_type.startswith("MITTELSTAND_") else "Growth"

        return {
            "company_name": company_name,
            "Status": "未接触",
            "Category": first_value("category") or "Factory",
            "ステータス理由": "",
            "hq_country": first_value("hq_country", "country"),
            "funding_stage": first_value("funding_stage"),
            "website": website,
            "what_it_solves": first_value("what_it_solves"),
            "japan_status": g6_or_m3_result,
            "source": first_value("source", "source_name") or "LeadFactory",
            "added_at": now,
            "japan_distributor_status": first_value("japan_distributor_status", "channel_structure", "exclusivity"),
            "japan_evidence_url": japan_evidence,
            "japan_checked_at": first_value("evaluated_at") if (g6_or_m3_result or japan_evidence) else "",
            "original_domain": domain,
            "subcategory": first_value("subcategory") or lane_subcategory,
            "priority": first_value("priority", "priority_signal"),
            "classification_confidence": first_value("classification_confidence", "confidence"),
            "selection_reason": first_value("selection_reason", "most_important_reason", "M3_reason"),
            "record_origin": "LeadFactory",
            "research_sources": " | ".join(evidence_parts),
            "reviewed_at": first_value("evaluated_at") or now,
            "japan_opportunity_note": japan_reason,
            "country": first_value("country", "hq_country"),
            "last_funding_date": first_value("last_funding_date"),
            "last_funding_amount": first_value("last_funding_amount"),
            "investors": first_value("investors"),
            "LF_lead_id": first_value("lead_id"),
            "LF_company_name": company_name,
            "LF_domain": domain,
            "LF_website": website,
            "LF_hq_country": first_value("hq_country", "country"),
            "LF_source_type": first_value("source_type"),
            "LF_source_name": first_value("source_name", "source"),
            "LF_source_url": first_value("source_url"),
            "LF_source_record_url": first_value("source_record_url"),
            "LF_discovered_at": first_value("discovered_at"),
            "LF_last_seen_at": first_value("last_seen_at"),
            "LF_screening_status": first_value("final_result", "screening_status"),
            "LF_last_screened_at": first_value("evaluated_at", "last_screened_at"),
            "LF_gate_version": first_value("gate_version"),
            "LF_error": first_value("error"),
            "LF_normalized_domain": domain,
            "LF_duplicate_state": first_value("duplicate_state"),
            "LF_intake_status": first_value("intake_status"),
            "LF_history": f"{now}|PROMOTED|{first_value('final_result', 'screening_status')}",
        }

    def promote_candidates_batch(self, candidates: list[dict], max_rows: int = 500) -> list[dict]:
        """Deduplicate and promote approved candidates to contiguous SSOT rows."""
        if not candidates:
            return []
        cfg = self.get_config()
        sheet = cfg.get("LEAD_FACTORY_HUMAN_SSOT_SHEET", "営業リスト＿Factory/BPO")
        headers_rows = self.read(f"'{sheet}'!1:1")
        if not headers_rows:
            raise RuntimeError(f"missing_header:{sheet}")
        headers = [str(h or "").strip() for h in headers_rows[0]]
        name_idx = headers.index("company_name") if "company_name" in headers else 0
        website_idx = headers.index("website") if "website" in headers else None
        original_domain_idx = headers.index("original_domain") if "original_domain" in headers else None
        lead_idx = headers.index("LF_lead_id") if "LF_lead_id" in headers else None
        last_col = self._column_letter(len(headers))
        existing_rows = self.read(f"'{sheet}'!A2:{last_col}")
        names = set()
        domains = set()
        lead_ids = set()
        last_used_row = 1
        for row_number, raw in enumerate(existing_rows, start=2):
            padded = list(raw) + [""] * max(0, len(headers) - len(raw))
            company = str(padded[name_idx] or "").strip()
            if company:
                last_used_row = row_number
                names.add(self._normalize_name(company))
            for index in (website_idx, original_domain_idx):
                if index is not None and index < len(padded):
                    normalized = self._normalize_domain(padded[index])
                    if normalized:
                        domains.add(normalized)
            if lead_idx is not None and lead_idx < len(padded):
                value = str(padded[lead_idx] or "").strip()
                if value:
                    lead_ids.add(value)

        try:
            configured_min_row = int(cfg.get("LEAD_FACTORY_HUMAN_APPEND_MIN_ROW", "2") or 2)
        except (TypeError, ValueError):
            configured_min_row = 2
        max_rows = max(1, min(1500, int(max_rows or 500)))
        now = datetime.now(timezone.utc).isoformat()
        accepted = []
        outcomes = []
        for candidate in candidates:
            final_result = str(candidate.get("final_result") or "").strip().upper()
            if final_result not in {"GO", "PASS"}:
                outcomes.append({"candidate": candidate, "result": {"status": "NOT_ELIGIBLE", "final_result": final_result}})
                continue
            try:
                row = self._sales_row_from_candidate(candidate, now)
            except ValueError as exc:
                outcomes.append({"candidate": candidate, "result": {"status": "NOT_ELIGIBLE", "reason": str(exc)}})
                continue
            candidate_lead_id = str(candidate.get("lead_id") or "").strip()
            candidate_name = self._normalize_name(row.get("company_name"))
            candidate_domain = self._normalize_domain(row.get("website") or row.get("original_domain"))
            if candidate_lead_id and candidate_lead_id in lead_ids:
                outcomes.append({"candidate": candidate, "result": {"status": "EXISTING_LINKED", "company_name": row["company_name"], "domain": candidate_domain}})
                continue
            if candidate_name in names or (candidate_domain and candidate_domain in domains):
                outcomes.append({"candidate": candidate, "result": {"status": "EXISTING", "company_name": row["company_name"], "domain": candidate_domain}})
                continue
            if len(accepted) >= max_rows:
                continue
            accepted.append((candidate, row))
            names.add(candidate_name)
            if candidate_domain:
                domains.add(candidate_domain)
            if candidate_lead_id:
                lead_ids.add(candidate_lead_id)

        if accepted:
            start_row = max(2, configured_min_row, last_used_row + 1)
            first, last = self.append_rows_preserving_previous_row_structure(
                sheet, [row for _, row in accepted], start_row
            )
            for offset, (candidate, row) in enumerate(accepted):
                outcomes.append({
                    "candidate": candidate,
                    "result": {
                        "status": "PROMOTED",
                        "company_name": row.get("company_name", ""),
                        "domain": self._normalize_domain(row.get("original_domain") or row.get("website")),
                        "new_status": "未接触",
                        "row_number": first + offset,
                    },
                })
        return outcomes


    def promote_to_sales_if_new(self, candidate: dict) -> dict:
        """Append one qualified company to the human-facing sales SSOT exactly once.


        Hard invariants:
        - Both Growth and Mittelstand lanes promote GO/PASS only. UNKNOWN stays in technical history.
        - Existing rows are never updated. Existing Status is therefore impossible to overwrite.
        - Domain is the primary duplicate key; exact normalized company-name equality also blocks duplicates.
        - Newly appended rows start with Status=未接触.
        """
        final_result = str(candidate.get("final_result", "")).strip().upper()
        source_type = str(candidate.get("source_type", "")).strip().upper()
        is_mittelstand = source_type.startswith("MITTELSTAND_") or "M1_result" in candidate
        allowed = {"GO", "PASS"}
        if final_result not in allowed:
            return {"status": "NOT_ELIGIBLE", "final_result": final_result, "is_mittelstand": is_mittelstand}
        company_name = str(candidate.get("company_name") or candidate.get("original_company") or "").strip()
        if not company_name:
            return {"status": "INVALID", "reason": "missing_company_name"}
        domain = self._normalize_domain(candidate.get("domain") or candidate.get("website") or "")
        website = str(candidate.get("website") or "").strip()
        if not domain:
            return {
                "status": "NOT_ELIGIBLE",
                "final_result": final_result,
                "reason": "verified_official_domain_required",
                "is_mittelstand": is_mittelstand,
            }
        if not website:
            website = f"https://{domain}"
        lead_id = str(candidate.get("lead_id") or "").strip()
        linked_row = self._find_sales_row_by_lf_lead_id(lead_id) if lead_id else None
        if linked_row is not None:
            return {"status": "EXISTING_LINKED", "company_name": company_name, "domain": domain, "row_number": linked_row}
        if not website and domain:
            website = f"https://{domain}"


        existing = self.find_sales_match(company_name, domain or website)
        if existing:
            self._refresh_sales_from_candidate(int(existing["row_number"]), candidate)
            return {"status": "EXISTING_REFRESHED", "existing": existing}


        now = datetime.now(timezone.utc).isoformat()


        def first_value(*keys: str):
            for key in keys:
                value = candidate.get(key)
                if value not in (None, "", [], {}):
                    return value
            return ""


        evidence_parts = []
        for key in (
            "research_sources", "evidence",
            "G1_evidence", "G2_evidence", "G3_evidence", "G4_evidence", "G5_evidence", "G6_evidence",
            "M1_evidence", "M2_evidence", "M3_evidence", "why_now_evidence",
        ):
            value = candidate.get(key)
            if value in (None, "", [], {}):
                continue
            if isinstance(value, (list, tuple, set)):
                parts = [str(v).strip() for v in value if str(v).strip()]
            else:
                parts = [part.strip() for part in str(value).split(" | ") if part.strip()]
            for part in parts:
                if part not in evidence_parts:
                    evidence_parts.append(part)


        g6_or_m3_result = str(first_value("G6_result", "M3_result", "japan_openness", "japan_presence") or "").strip()
        japan_evidence = first_value("G6_evidence", "M3_evidence", "japan_evidence_url")
        japan_reason = first_value("japan_opportunity_note", "G6_reason", "M3_reason")
        lane_subcategory = "Mittelstand" if is_mittelstand else "Growth"


        row = {
            "company_name": company_name,
            "Status": "未接触",
            "Category": first_value("category") or "Factory",
            "ステータス理由": "",
            "hq_country": first_value("hq_country", "country"),
            "funding_stage": first_value("funding_stage"),
            "website": website,
            "what_it_solves": first_value("what_it_solves"),
            "japan_status": g6_or_m3_result,
            "source": first_value("source", "source_name") or "LeadFactory",
            "added_at": now,
            "japan_distributor_status": first_value("japan_distributor_status", "channel_structure", "exclusivity"),
            "japan_evidence_url": japan_evidence,
            "japan_checked_at": first_value("evaluated_at") if (g6_or_m3_result or japan_evidence) else "",
            "original_domain": domain,
            "subcategory": first_value("subcategory") or lane_subcategory,
            "priority": first_value("priority", "priority_signal"),
            "classification_confidence": first_value("classification_confidence", "confidence"),
            "selection_reason": first_value("selection_reason", "most_important_reason", "M3_reason"),
            "record_origin": "LeadFactory",
            "research_sources": " | ".join(evidence_parts),
            "reviewed_at": first_value("evaluated_at") or now,
            "japan_opportunity_note": japan_reason,
            "country": first_value("country", "hq_country"),
            "last_funding_date": first_value("last_funding_date"),
            "last_funding_amount": first_value("last_funding_amount"),
            "investors": first_value("investors"),
            "LF_lead_id": first_value("lead_id"),
            "LF_company_name": company_name,
            "LF_domain": domain,
            "LF_website": website,
            "LF_hq_country": first_value("hq_country", "country"),
            "LF_source_type": first_value("source_type"),
            "LF_source_name": first_value("source_name", "source"),
            "LF_source_url": first_value("source_url"),
            "LF_source_record_url": first_value("source_record_url"),
            "LF_discovered_at": first_value("discovered_at"),
            "LF_last_seen_at": first_value("last_seen_at"),
            "LF_screening_status": first_value("final_result", "screening_status"),
            "LF_last_screened_at": first_value("evaluated_at", "last_screened_at"),
            "LF_gate_version": first_value("gate_version"),
            "LF_error": first_value("error"),
            "LF_normalized_domain": domain,
            "LF_duplicate_state": first_value("duplicate_state"),
            "LF_intake_status": first_value("intake_status"),
            "LF_history": f"{now}|PROMOTED|{first_value('final_result', 'screening_status')}",
        }
        cfg = self.get_config()
        target_sheet = cfg.get("LEAD_FACTORY_HUMAN_SSOT_SHEET", "営業リスト＿Factory/BPO")
        try:
            min_row = int(cfg.get("LEAD_FACTORY_HUMAN_APPEND_MIN_ROW", "2") or 2)
        except Exception:
            min_row = 2
        row_number = self.append_dict_preserving_previous_row_structure(target_sheet, row, min_row=min_row)
        return {"status": "PROMOTED", "company_name": company_name, "domain": domain, "new_status": "未接触", "row_number": row_number}


    def list_promotable_candidates(self, lane: str | None = None) -> list[dict]:
        """Return latest eligible Gate results from both lanes, joined to Raw.


        Latest result wins per lead_id. This prevents a stale historical GO from being promoted
        after a newer UNKNOWN/NO evaluation. Synthetic smoke rows without a Raw lead_id are ignored.
        """
        raw_headers = self.read("LeadFactory_Raw!1:1")
        raw_rows = self.read("LeadFactory_Raw!A2:R")
        raw_by_lead: dict[str, dict] = {}
        if raw_headers:
            hs = raw_headers[0]
            for r in raw_rows:
                padded = r + [""] * max(0, len(hs) - len(r))
                d = dict(zip(hs, padded))
                if d.get("lead_id"):
                    raw_by_lead[str(d["lead_id"])] = d


        latest: dict[str, dict] = {}


        def absorb(sheet: str, allowed: set[str], company_field: str):
            headers_rows = self.read(f"{sheet}!1:1")
            if not headers_rows:
                return
            headers = headers_rows[0]
            rows = self.read(f"{sheet}!A2:ZZ")
            for r in rows:
                padded = r + [""] * max(0, len(headers) - len(r))
                d = dict(zip(headers, padded))
                lead_id = str(d.get("lead_id", "")).strip()
                if not lead_id or lead_id not in raw_by_lead:
                    continue
                evaluated_at = str(d.get("evaluated_at", ""))
                prev = latest.get(lead_id)
                if prev and str(prev.get("evaluated_at", "")) > evaluated_at:
                    continue
                raw = raw_by_lead[lead_id]
                merged = {**raw, **d}
                merged["company_name"] = str(d.get(company_field) or raw.get("company_name") or "")
                merged["website"] = raw.get("website") or (f"https://{d.get('domain')}" if d.get("domain") else "")
                merged["source_name"] = raw.get("source_name", "")
                merged["hq_country"] = raw.get("hq_country", "") or d.get("country", "")
                merged["_allowed"] = str(d.get("final_result", "")).strip().upper() in allowed
                latest[lead_id] = merged


        absorb("LeadFactory_GateResults", {"GO", "PASS"}, "company_name")
        absorb("LeadFactory_MittelstandResults", {"GO"}, "original_company")
        lane_key = str(lane or "").strip().upper()
        out = []
        for d in latest.values():
            if not d.get("_allowed"):
                continue
            source_type = str(d.get("source_type") or "").upper()
            if lane_key == "MITTELSTAND" and not source_type.startswith("MITTELSTAND_"):
                continue
            if lane_key == "GROWTH" and source_type.startswith("MITTELSTAND_"):
                continue
            out.append(d)
        return out


    def promotion_tick(self, lane: str | None = None) -> dict:
        from promotion_accounting import (
            accounting_snapshot,
            reconcile_promotion_ledger,
            record_promotion,
        )

        reconciliation_before = reconcile_promotion_ledger(self)
        candidates = self.list_promotable_candidates(lane=lane)
        configured_batch = self.get_config().get("LEAD_FACTORY_PROMOTION_BATCH_SIZE", "500")
        try:
            batch_size = max(1, min(1500, int(configured_batch or 500)))
        except (TypeError, ValueError):
            batch_size = 500
        outcomes = self.promote_candidates_batch(candidates, max_rows=batch_size)
        promoted = 0
        existing = 0
        skipped = 0
        ledger_errors = 0
        details = []
        for item in outcomes:
            candidate = item.get("candidate") or {}
            result = dict(item.get("result") or {})
            if result.get("status") == "PROMOTED":
                promoted += 1
                try:
                    ledger_result = record_promotion(self, candidate, result, reason="new_gate_promotion")
                    result = {**result, "ledger": ledger_result}
                except Exception as exc:
                    ledger_errors += 1
                    result = {**result, "ledger_error": f"{type(exc).__name__}:{exc}"}
            elif str(result.get("status") or "").startswith("EXISTING"):
                existing += 1
            else:
                skipped += 1
            details.append({"lead_id": candidate.get("lead_id", ""), **result})
        reconciliation_after = reconcile_promotion_ledger(self)
        cfg = self.get_config()
        try:
            baseline = int(cfg.get("LEAD_FACTORY_GOAL_BASELINE_SSOT", "0") or 0)
        except (TypeError, ValueError):
            baseline = 0
        accounting = accounting_snapshot(
            self,
            baseline,
            cfg.get("LEAD_FACTORY_GOAL_START_AT", ""),
        )
        return {
            "status": "COMPLETE" if ledger_errors == 0 else "COMPLETE_WITH_LEDGER_ERRORS",
            "eligible": len(candidates),
            "promoted": promoted,
            "existing": existing,
            "skipped": skipped,
            "ledger_errors": ledger_errors,
            "ledger_reconciliation_before": reconciliation_before,
            "ledger_reconciliation_after": reconciliation_after,
            "accounting": accounting,
            "details": details,
        }


    def list_needs_domain(self, limit: int = 30, lane: str | None = None) -> list[dict]:
        rows = self.read("LeadFactory_Raw!A2:R")
        normal: list[dict] = []
        priority: list[dict] = []
        lane_key = str(lane or "").strip().upper()
        for row_number, r in enumerate(rows, start=2):
            padded = r + [""] * (18 - len(r))
            source_type = str(padded[5]).upper()
            if lane_key == "MITTELSTAND" and not source_type.startswith("MITTELSTAND_"):
                continue
            if lane_key == "GROWTH" and source_type.startswith("MITTELSTAND_"):
                continue
            if str(padded[17]).upper() != "NEEDS_DOMAIN":
                continue
            if str(padded[11]).upper() not in {"", "PENDING"}:
                continue
            candidate = {
                "row_number": row_number, "lead_id": padded[0], "company_name": padded[1],
                "domain": padded[2], "website": padded[3], "hq_country": padded[4],
                "source_type": padded[5], "source_name": padded[6], "source_url": padded[7],
                "source_record_url": padded[8],
            }
            if str(padded[6]).strip() == "MAKTEK Eurasia 2026":
                priority.append(candidate)
            else:
                normal.append(candidate)
        return (priority + normal)[:max(0, int(limit))]

    def _raw_domain_duplicate_state(self, lead_id: str, company_name: str, domain: str) -> str:
        domain = self._normalize_domain(domain)
        if not domain:
            return "UNRESOLVED"
        if self.find_sales_match(company_name, domain):
            return "EXISTS_IN_SALES"
        rows = self.read("LeadFactory_Raw!A2:R")
        for r in rows:
            padded = r + [""] * (18 - len(r))
            if str(padded[0]) == str(lead_id):
                continue
            other = self._normalize_domain(padded[15] or padded[2] or padded[3])
            if other and other == domain:
                return "DUPLICATE_RAW"
        return "NEW"


    def update_raw_domain_resolution(
        self,
        lead_id: str,
        domain: str,
        website: str = "",
        hq_country: str = "",
        confidence: str = "HIGH",
        evidence: str = "",
        *,
        row_number: int | None = None,
        source_type: str = "",
        preserve_formulas: bool = True,
        check_duplicates: bool = True,
    ) -> dict:
        """Advance one Raw row with one bounded Sheets write.

        Worker calls may provide the row number and literal intake route captured
        by the same Raw snapshot. That avoids a second full-sheet read and keeps
        the duplicate decision at promotion, where the human SSOT is checked
        authoritatively.
        """
        domain = self._normalize_domain(domain or website)
        confidence_key = str(confidence).upper()
        if confidence_key != "HIGH" or not domain:
            return {"status": "UNRESOLVED", "lead_id": lead_id, "confidence": confidence_key}

        company_name = ""
        if row_number is None:
            rows = self.read("LeadFactory_Raw!A2:R")
            for candidate_row_number, r in enumerate(rows, start=2):
                padded = r + [""] * (18 - len(r))
                if str(padded[0]) != str(lead_id):
                    continue
                row_number = candidate_row_number
                company_name = str(padded[1])
                source_type = str(source_type or padded[5]).upper()
                if not hq_country:
                    hq_country = str(padded[4] or "")
                break
        if row_number is None:
            raise KeyError(f"lead_not_found:{lead_id}")
        row_number = int(row_number)
        source_type = str(source_type or "").upper()
        if check_duplicates:
            if not company_name:
                row = self.read(f"LeadFactory_Raw!A{row_number}:R{row_number}")
                padded = (row[0] if row else []) + [""] * 18
                company_name = str(padded[1])
                source_type = source_type or str(padded[5]).upper()
                if not hq_country:
                    hq_country = str(padded[4] or "")
            duplicate_state = self._raw_domain_duplicate_state(lead_id, company_name, domain)
        else:
            duplicate_state = "NEW"
        intake_status = (
            "READY_FOR_MITTELSTAND_GATE" if source_type.startswith("MITTELSTAND_")
            else "READY_FOR_GATE"
        ) if duplicate_state == "NEW" else "SKIP"
        canonical_website = str(website or f"https://{domain}").strip()

        p_formula = q_formula = r_formula = False
        if preserve_formulas:
            formulas = self.read_formulas(f"LeadFactory_Raw!P{row_number}:R{row_number}")
            frow = ((formulas[0] if formulas else []) + ["", "", ""])[:3]
            p_formula = str(frow[0]).startswith("=")
            q_formula = str(frow[1]).startswith("=")
            r_formula = str(frow[2]).startswith("=")

        updates = [
            {"range": f"LeadFactory_Raw!C{row_number}:E{row_number}", "values": [[domain, canonical_website, hq_country]]},
        ]
        if not p_formula:
            updates.append({"range": f"LeadFactory_Raw!P{row_number}", "values": [[domain]]})
        if not q_formula:
            updates.append({"range": f"LeadFactory_Raw!Q{row_number}", "values": [[duplicate_state]]})
        if source_type.startswith("MITTELSTAND_") or not r_formula:
            updates.append({"range": f"LeadFactory_Raw!R{row_number}", "values": [[intake_status]]})
        self._execute_write(lambda: self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"valueInputOption": "RAW", "data": updates},
        ).execute())

        return {
            "status": "RESOLVED",
            "lead_id": lead_id,
            "domain": domain,
            "website": canonical_website,
            "duplicate_state": duplicate_state,
            "intake_status": intake_status,
            "formula_control_preserved": {
                "P": p_formula,
                "Q": q_formula,
                "R": r_formula and not source_type.startswith("MITTELSTAND_"),
            },
            "evidence": evidence,
        }


    def add_access_request(self, source: Source, reason: str, required_action: str) -> str:
        req_id = f"access-{uuid.uuid4()}"
        now = datetime.now(timezone.utc).isoformat()
        self.append("LeadFactory_AccessRequests", [
            req_id, source.source_id, source.source_name, source.crawl_url, reason,
            required_action, now, "OPEN", "", "", "", "", "", "",
        ])
        return req_id


    def register_scraper(self, row: dict) -> None:
        if "adapter_code" in row:
            self._ensure_header("LeadFactory_Scrapers", "adapter_code")
        self.upsert_dict("LeadFactory_Scrapers", "source_id", str(row.get("source_id", "")), row)


    def get_scraper(self, source_id: str) -> dict | None:
        headers_rows = self.read("LeadFactory_Scrapers!1:1")
        if not headers_rows:
            return None
        headers = headers_rows[0]
        rows = self.read("LeadFactory_Scrapers!A2:ZZ")
        if "source_id" not in headers:
            return None
        idx = headers.index("source_id")
        for r in rows:
            if len(r) > idx and r[idx] == source_id:
                padded = r + [""] * (len(headers) - len(r))
                return dict(zip(headers, padded))
        return None


    def update_scraper_health(self, source_id: str, **changes) -> None:
        current = self.get_scraper(source_id)
        if current is None:
            return
        current.update(changes)
        current["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.register_scraper(current)


    def append_test(self, row: dict) -> None:
        self.append_dict("LeadFactory_ScraperTests", row)


    def rows_as_dicts_once(
        self,
        sheet: str,
        end_col: str = "ZZ",
        *,
        max_row: int | None = None,
    ) -> list[dict]:
        """Read a bounded sheet range in one request and preserve row numbers."""
        range_ref = (
            f"'{sheet}'!A1:{end_col}"
            if max_row is None
            else f"'{sheet}'!A1:{end_col}{max(1, int(max_row))}"
        )
        values = self.read_once(range_ref)
        if not values:
            return []
        headers = values[0]
        out = []
        for row_number, row in enumerate(values[1:], start=2):
            padded = list(row) + [""] * max(0, len(headers) - len(row))
            item = dict(zip(headers, padded))
            item["row_number"] = row_number
            out.append(item)
        return out


    def _rows_as_dicts(self, sheet: str, end_col: str = "ZZ") -> list[dict]:
        headers_rows = self.read(f"{sheet}!1:1")
        if not headers_rows:
            return []
        headers = headers_rows[0]
        rows = self.read(f"{sheet}!A2:{end_col}")
        out = []
        for row_number, row in enumerate(rows, start=2):
            padded = row + [""] * max(0, len(headers) - len(row))
            item = dict(zip(headers, padded))
            item["row_number"] = row_number
            out.append(item)
        return out

    def outreach_draft_rows(self) -> list[dict]:
        return self._rows_as_dicts("LeadFactory_MessageDrafts", "ZZ")

    def outreach_queue_rows(self) -> list[dict]:
        return self._rows_as_dicts("LeadFactory_ApprovalQueue", "ZZ")

    def update_message_draft(self, draft_id: str, changes: dict) -> bool:
        for row in self.outreach_draft_rows():
            if str(row.get("draft_id") or "").strip() == str(draft_id or "").strip():
                self._update_dict_fields("LeadFactory_MessageDrafts", int(row["row_number"]), changes)
                return True
        return False

    def update_approval_queue_for_draft(self, draft_id: str, changes: dict) -> bool:
        for row in self.outreach_queue_rows():
            if str(row.get("draft_id") or "").strip() == str(draft_id or "").strip():
                self._update_dict_fields("LeadFactory_ApprovalQueue", int(row["row_number"]), changes)
                return True
        return False

    def outreach_candidate_by_key(self, company_key: str) -> dict | None:
        key = str(company_key or "").strip()
        if not key:
            return None
        for candidate in self.list_promotable_candidates():
            if str(candidate.get("lead_id") or "").strip() == key:
                return candidate
        return None

    def outreach_candidates(self, limit: int = 5, lane: str | None = None) -> list[dict]:
        """Qualified, untouched leads that still need an internal send-ready draft."""
        drafted = {
            str(row.get("company_key") or "").strip()
            for row in self._rows_as_dicts("LeadFactory_MessageDrafts", "X")
            if str(row.get("company_key") or "").strip()
        }
        lane_key = str(lane or "").strip().upper()
        out = []
        for candidate in self.list_promotable_candidates(lane=lane):
            key = str(candidate.get("lead_id") or "").strip()
            if not key or key in drafted:
                continue
            source_type = str(candidate.get("source_type") or "").upper()
            if lane_key == "MITTELSTAND" and not source_type.startswith("MITTELSTAND_"):
                continue
            if lane_key == "GROWTH" and source_type.startswith("MITTELSTAND_"):
                continue
            row_number = self._find_sales_row_by_lf_lead_id(key)
            if not row_number:
                continue
            sheet, _ = self._human_ssot_config()
            status_rows = self.read(f"'{sheet}'!B{row_number}:B{row_number}")
            status = str(status_rows[0][0]).strip() if status_rows and status_rows[0] else ""
            if status != "未接触":
                continue
            out.append(candidate)
            if len(out) >= max(0, int(limit)):
                break
        return out

    def latest_contact(self, company_key: str) -> dict | None:
        key = str(company_key or "").strip()
        if not key:
            return None
        found = None
        for row in self._rows_as_dicts("LeadFactory_ContactResearch", "X"):
            if str(row.get("company_key") or "").strip() == key:
                found = row
        return found

    def append_contact_research(self, row: dict) -> None:
        self.append_dict("LeadFactory_ContactResearch", row)

    def append_message_draft(self, row: dict) -> None:
        self.append_dict("LeadFactory_MessageDrafts", row)

    def append_approval_queue(self, row: dict) -> None:
        self.append_dict("LeadFactory_ApprovalQueue", row)

    def append_operational_event(self, row: dict) -> None:
        """Write pipeline/send outcomes to the existing operational event ledger."""
        self.append_dict("SalesControl_Events", row)

    def operational_event_summary(self, limit: int = 5000) -> dict:
        """Aggregate send, reply, and pipeline failure outcomes for the control UI."""
        rows = self._rows_as_dicts("SalesControl_Events", "Z")[-max(1, int(limit)):]
        counts: dict[str, int] = {}
        failures: dict[str, int] = {}
        for row in rows:
            event_type = str(row.get("event_type") or "UNKNOWN").strip().upper()
            counts[event_type] = counts.get(event_type, 0) + 1
            if event_type.endswith("FAILED") or event_type in {"PIPELINE_FAILURE", "OUTBOUND_BLOCKED"}:
                code = str(row.get("reason_code") or "UNKNOWN_FAILURE").strip().upper()
                failures[code] = failures.get(code, 0) + 1
        return {
            "events_scanned": len(rows),
            "event_counts": counts,
            "failure_counts": failures,
            "last_events": [
                {"occurred_at": r.get("occurred_at", ""), "event_type": r.get("event_type", ""),
                 "company_name": r.get("company_name", ""), "reason_code": r.get("reason_code", ""),
                 "status": r.get("match_status", "")}
                for r in rows[-20:]
            ],
        }


    def append_operational_event(self, row: dict) -> None:
        """Write pipeline/send outcomes to the existing operational event ledger."""
        self.append_dict("SalesControl_Events", row)


    def operational_event_summary(self, limit: int = 5000) -> dict:
        """Aggregate send, reply, and pipeline failure outcomes for the control UI."""
        rows = self._rows_as_dicts("SalesControl_Events", "Z")[-max(1, int(limit)):]
        counts: dict[str, int] = {}
        failures: dict[str, int] = {}
        for row in rows:
            event_type = str(row.get("event_type") or "UNKNOWN").strip().upper()
            counts[event_type] = counts.get(event_type, 0) + 1
            if event_type.endswith("FAILED") or event_type in {"PIPELINE_FAILURE", "OUTBOUND_BLOCKED"}:
                code = str(row.get("reason_code") or "UNKNOWN_FAILURE").strip().upper()
                failures[code] = failures.get(code, 0) + 1
        return {
            "events_scanned": len(rows),
            "event_counts": counts,
            "failure_counts": failures,
            "last_events": [
                {"occurred_at": r.get("occurred_at", ""), "event_type": r.get("event_type", ""),
                 "company_name": r.get("company_name", ""), "reason_code": r.get("reason_code", ""),
                 "status": r.get("match_status", "")}
                for r in rows[-20:]
            ],
        }


    def append_runlog(self, row: list) -> None:
        self.append("LeadFactory_RunLog", row)


    def append_mittelstand_result(self, row: dict) -> None:
        self.append_dict("LeadFactory_MittelstandResults", row)


    def list_pending_gate(self, limit: int = 20) -> list[dict]:
        rows = self.read("LeadFactory_Raw!A2:R")
        normal: list[dict] = []
        priority: list[dict] = []
        for row_number, r in enumerate(rows, start=2):
            padded = r + [""] * (18 - len(r))
            source_type = str(padded[5]).upper()
            intake = str(padded[17]).upper()
            screening = str(padded[11]).upper()
            if source_type.startswith("MITTELSTAND_"):
                continue
            if intake != "READY_FOR_GATE":
                continue
            if screening not in {"", "PENDING"}:
                continue
            candidate = {
                "row_number": row_number,
                "lead_id": padded[0],
                "company_name": padded[1],
                "domain": padded[2],
                "website": padded[3],
                "hq_country": padded[4],
                "source_type": padded[5],
                "source_name": padded[6],
                "source_url": padded[7],
                "source_record_url": padded[8],
            }
            if str(padded[6]).strip() == "MAKTEK Eurasia 2026":
                priority.append(candidate)
            else:
                normal.append(candidate)
        return (priority + normal)[:max(0, int(limit))]

    def list_pending_mittelstand(self, limit: int = 20) -> list[dict]:
        rows = self.read("LeadFactory_Raw!A2:R")
        normal: list[dict] = []
        priority: list[dict] = []
        for row_number, r in enumerate(rows, start=2):
            padded = r + [""] * (18 - len(r))
            source_type = str(padded[5]).upper()
            intake = str(padded[17]).upper()
            screening = str(padded[11]).upper()
            if not source_type.startswith("MITTELSTAND_"):
                continue
            if intake != "READY_FOR_MITTELSTAND_GATE":
                continue
            if screening not in {"", "PENDING"}:
                continue
            candidate = {
                "row_number": row_number,
                "lead_id": padded[0],
                "company_name": padded[1],
                "domain": padded[2],
                "website": padded[3],
                "hq_country": padded[4],
                "source_type": padded[5],
                "source_name": padded[6],
                "source_url": padded[7],
                "source_record_url": padded[8],
            }
            if str(padded[6]).strip() == "MAKTEK Eurasia 2026":
                priority.append(candidate)
            else:
                normal.append(candidate)
        return (priority + normal)[:max(0, int(limit))]

    def update_raw_screening(
        self,
        lead_id: str,
        screening_status: str,
        gate_version: str,
        error: str = "",
        *,
        row_number: int | None = None,
    ) -> None:
        if row_number is None:
            rows = self.read("LeadFactory_Raw!A2:R")
            target_row = None
            for candidate_row_number, r in enumerate(rows, start=2):
                padded = r + [""] * (18 - len(r))
                if str(padded[0]) == str(lead_id):
                    target_row = candidate_row_number
                    break
            if target_row is None:
                raise KeyError(f"lead_not_found:{lead_id}")
        else:
            target_row = int(row_number)
        self.update_range(
            f"LeadFactory_Raw!L{target_row}:O{target_row}",
            [[screening_status, datetime.now(timezone.utc).isoformat(), gate_version, error]],
        )


    def count_valid_qualified_ssot(self) -> int:
        from promotion_accounting import count_valid_qualified_ssot
        return count_valid_qualified_ssot(self)

    def promotion_accounting_snapshot(self, baseline: int, start_at: str | None) -> dict:
        from promotion_accounting import accounting_snapshot
        return accounting_snapshot(self, baseline, start_at)

    def reconcile_promotion_ledger(self) -> dict:
        from promotion_accounting import reconcile_promotion_ledger
        return reconcile_promotion_ledger(self)

    def count_promoted_leads(self) -> int:
        """Count current Lead Factory promotions in the technical SSOT."""
        rows = self.read("LeadFactory_Raw!A2:R")
        return sum(1 for row in rows if len(row) > 17 and str(row[17] or "").strip().upper() == "PROMOTED_TO_SALES")

    def recent_supply_runlogs(self, lane: str, limit: int = 10) -> list[dict]:
        """Return recent autonomous supply run summaries for a lane."""
        rows = self.read("LeadFactory_RunLog!A2:R")
        lane_marker = f"lane:{str(lane or '').strip().upper()}"
        out = []
        for row in reversed(rows):
            padded = row + [""] * (18 - len(row))
            if lane_marker not in str(padded[13] or "").upper():
                continue
            out.append({"run_id": padded[0], "status": padded[3], "promoted": int(str(padded[11] or "0") or 0), "started_at": padded[1], "finished_at": padded[2]})
            if len(out) >= max(0, int(limit)):
                break
        return out


from single_sheet_mode import install as _install_single_sheet_mode
_install_single_sheet_mode(SheetsRepo)
