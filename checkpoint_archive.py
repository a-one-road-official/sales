"""Lossless, archive-first compaction for existing Sheets checkpoint callers.

Pure stdlib/request planning: no network, credentials, scheduler or customer I/O.
Keep a prepared plan after an uncertain append. Read its exact event identities,
verify/reconstruct the archive, then acquire the existing guard and fresh-read
the literal STATE cell before calling ``compaction_requests``. A snapshot hash
comparison is safe against participating writers only while that guard is held;
Google Sheets does not provide a conditional cell-value update.

Only explicitly listed historical roots and exact VERIFIED transactions with a
recognized identity shape are candidates. Pending/current/unknown root fields
and operational counts/cursors are never selected. Transaction stubs preserve
the legacy rows/identity/decision_id/packet_sha shapes and VERIFIED status.
Use ``hydrate_state`` before a consumer needs archived historical detail.

V2 uses one history reference and live deltas, rather than retaining every
transaction stub forever. Use ``history_read_view`` (or ``load_history_view``)
for the legacy logical shape, then ``merge_next_head`` for every state write.
The 20k threshold is a soft archival trigger; only the 40k head budget is a
capacity gate. No eligible history is not itself a reason to shrink a batch.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Mapping

from sheets_persistence import (append_receipts, cell_update,
    commit_and_release_requests, verify_event_readback)

VERSION = "CHECKPOINT_ARCHIVE_V1"
REF = "__checkpoint_archive_v1__"
HISTORY_VERSION = "CHECKPOINT_ARCHIVE_V2"
HISTORY_REF = "__checkpoint_history_v2__"
HISTORY_KEYS = ("baseline_v5", "baseline_v6", "baseline_v7", "baseline_v8",
                "baseline_v9", "calibration_previous")
COMPACT_AT_UNITS = 20_000
MAX_HEAD_UNITS = 40_000
MAX_EVENT_CELL_UNITS = 20_000
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_TX_KEEP = ("status", "lane", "rows", "identity", "decision_id", "packet_sha",
            "next_cursor_candidate", "policy_version", "verified_at", "row_count")
MEMBERSHIP_PATHS = (
    ("reviewed_source_rows",), ("verified_final_source_rows",),
    ("overall_verified_rows",),
    ("dual_lane", "lane_a", "verified_completed_rows"),
    ("dual_lane", "lane_b", "verified_completed_rows"),
    ("dual_lane", "lane_a", "current_policy_verified_rows"),
    ("dual_lane", "lane_b", "current_policy_verified_rows"),
    ("calibration", "completed_rows"), ("calibration", "selected_rows"),
)


class ArchiveError(ValueError):
    pass


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utf16_units(text):
    return len(text.encode("utf-16-le")) // 2


def _object(text):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ArchiveError("DUPLICATE_JSON_KEY:" + key)
            out[key] = value
        return out
    try:
        value = json.loads(text, object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ArchiveError("NONFINITE_JSON")))
    except (ValueError, UnicodeError) as exc:
        raise ArchiveError("INVALID_STATE_JSON:" + str(exc)) from exc
    if not isinstance(value, dict):
        raise ArchiveError("STATE_MUST_BE_OBJECT")
    dumps(value)  # Reject non-JSON values/invalid Unicode before planning any write.
    sha256(text)
    return value


def _at(value, path):
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise ArchiveError("ARCHIVED_PATH_MISSING")
        value = value[key]
    return value


def _row_count(tx):
    rows = tx.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    if all(isinstance(row, dict) for row in rows):
        numbers = [row.get("row") for row in rows]
        decisions = [row.get("decision_id") for row in rows]
    elif all(type(row) is int for row in rows):
        numbers, decisions = rows, tx.get("decision_id")
    else:
        return None
    if (not isinstance(decisions, list) or len(decisions) != len(numbers)
            or any(type(row) is not int or row < 2 for row in numbers)
            or len(set(numbers)) != len(numbers)
            or any(not isinstance(d, str) or not _HASH.fullmatch(d) for d in decisions)):
        return None
    for key in ("identity", "packet_sha"):
        if key in tx and (not isinstance(tx[key], list) or len(tx[key]) != len(rows)):
            return None
    return len(rows)


def _stub(value, path, manifest_id):
    reference = {"manifest_event_id": manifest_id, "path": list(path),
                 "record_sha256": sha256(dumps(value))}
    result = {}
    if len(path) == 2 and path[0] == "transactions":
        result = {key: copy.deepcopy(value[key]) for key in _TX_KEEP if key in value}
        reference["row_count"] = len(value["rows"])
    result[REF] = reference
    return result


def _selection(state, history_keys, manifest_id):
    active = str(state.get("target_policy_version") or state.get("version") or "")
    match = re.search(r"(?:^|_)V(\d+)(?:_|$)", active)
    active_version = int(match[1]) if match else None
    paths = []
    for key in history_keys:
        old = re.fullmatch(r"baseline_v(\d+)", key)
        if key != "calibration_previous" and not old:
            raise ArchiveError("HISTORY_KEY_NOT_EXPLICIT_BASELINE:" + key)
        if old and (active_version is None or int(old[1]) >= active_version):
            raise ArchiveError("BASELINE_NOT_PROVEN_OLD:" + key)
        value = state.get(key)
        if not isinstance(value, dict) or REF in value:
            continue
        if value.get("pending_rows") or value.get("pending"):
            continue
        paths.append([key])
    transactions = state.get("transactions", {})
    if not isinstance(transactions, dict):
        raise ArchiveError("TRANSACTIONS_MUST_BE_OBJECT")
    for identity, tx in transactions.items():
        if (isinstance(tx, dict) and tx.get("status") == "VERIFIED"
                and not tx.get("pending") and not tx.get("pending_rows")
                and not tx.get("pending_transactions")
                and tx.get("outcome") not in {"UNRESOLVED", "UNKNOWN"}
                and REF not in tx and _row_count(tx) is not None):
            paths.append(["transactions", identity])
    # Never enlarge a tiny baseline or an already compact transaction.
    return [path for path in paths if utf16_units(dumps(_stub(_at(state, path), path, manifest_id)))
            < utf16_units(dumps(_at(state, path)))]


def _event(identity, action, payload, *, at, run_id, origin, version=VERSION):
    return {"event_id": identity, "occurred_at": at, "recorded_at": at,
            "action_type": action, "source": "CHECKPOINT_ARCHIVE",
            "writer": "checkpoint_archive", "code_version": version,
            "idempotency_key": identity, "canonical_action_id": identity,
            "reason": "Lossless verified-history archival; zero customer actions",
            "crm_payload": dict(payload, run_id=run_id, phase=action, origin=origin),
            "crm_result": "RECORDED"}


def prepare_archive(state_text, *, source_state_key, at, run_id, origin,
                    history_keys=HISTORY_KEYS, max_cell_units=MAX_EVENT_CELL_UNITS,
                    max_head_units=MAX_HEAD_UNITS, _bounded_history=False):
    """Prepare immutable backups and a reviewable head; never prune or execute I/O.

    Archive IDs are tied to this run and exact source bytes. Preserve the whole
    returned plan, including its timestamps/IDs, across uncertain append results.
    """
    state = _object(state_text)
    if not all(isinstance(v, str) and v for v in (source_state_key, at, run_id, origin)):
        raise ArchiveError("ARCHIVE_PROVENANCE_REQUIRED")
    when = datetime.fromisoformat(at.replace("Z", "+00:00"))
    if when.tzinfo is None:
        raise ArchiveError("TIMEZONE_REQUIRED")
    if not 1000 <= max_cell_units <= MAX_EVENT_CELL_UNITS or not 1000 <= max_head_units <= MAX_HEAD_UNITS:
        raise ArchiveError("INVALID_CELL_BUDGET")
    source_sha = sha256(state_text)
    version = HISTORY_VERSION if _bounded_history else VERSION
    identity = [source_state_key, source_sha, run_id, at]
    if _bounded_history:
        identity.append(version)
    archive_id = "state-archive:" + sha256(dumps(identity))
    manifest_id = archive_id + ":manifest"
    if _bounded_history:
        compact, paths = _bounded_head(state, manifest_id, source_state_key,
                                       source_sha, tuple(history_keys))
    else:
        paths = _selection(state, tuple(history_keys), manifest_id)
    if not paths:
        raise ArchiveError("NO_ELIGIBLE_HISTORY_TO_COMPACT")
    common = dict(at=at, run_id=run_id, origin=origin, version=version)
    chunks, offset = [], 0
    while offset < len(state_text):
        index = len(chunks)
        if index >= 256:
            raise ArchiveError("ARCHIVE_CHUNK_LIMIT")
        def candidate(length):
            data = state_text[offset:offset + length]
            return {"archive_id": archive_id, "source_state_key": source_state_key,
                    "snapshot_sha256": source_sha, "index": index,
                    "count": 256, "data": data, "data_sha256": sha256(data)}
        lo, hi, size = 1, len(state_text) - offset, 0
        while lo <= hi:
            mid = (lo + hi) // 2
            event = _event(archive_id + f":chunk:{index}", "STATE_ARCHIVE_CHUNK", candidate(mid), **common)
            if utf16_units(dumps(event["crm_payload"])) <= max_cell_units:
                size, lo = mid, mid + 1
            else:
                hi = mid - 1
        if not size:
            raise ArchiveError("ARCHIVE_METADATA_EXCEEDS_BUDGET")
        chunks.append(candidate(size))
        offset += size
    for chunk in chunks:
        chunk["count"] = len(chunks)
    events = [_event(archive_id + f":chunk:{chunk['index']}", "STATE_ARCHIVE_CHUNK", chunk, **common)
              for chunk in chunks]
    manifest = {"schema": version, "archive_id": archive_id, "source_state_key": source_state_key,
                "snapshot_sha256": source_sha, "snapshot_utf16_units": utf16_units(state_text),
                "snapshot_utf8_bytes": len(state_text.encode("utf-8")), "paths": paths,
                "chunks": [{"event_id": event["event_id"], "index": chunk["index"],
                            "sha256": chunk["data_sha256"]} for event, chunk in zip(events, chunks)]}
    events.append(_event(manifest_id, "STATE_ARCHIVE_MANIFEST", manifest, **common))
    for event in events:
        for value in event.values():
            literal = dumps(value) if isinstance(value, (dict, list)) else str(value)
            if utf16_units(literal) > max_cell_units:
                raise ArchiveError("EVENT_CELL_EXCEEDS_BUDGET")
    if not _bounded_history:
        compact = copy.deepcopy(state)
        for path in paths:
            parent = compact if len(path) == 1 else compact[path[0]]
            parent[path[-1]] = _stub(_at(state, path), path, manifest_id)
    text = dumps(compact)
    if utf16_units(text) > max_head_units:
        raise ArchiveError("COMPACT_HEAD_EXCEEDS_BUDGET_NO_PRUNING")
    return {"schema": version, "source_state_key": source_state_key, "source_text": state_text,
            "snapshot_sha256": source_sha, "archive_id": archive_id,
            "manifest_event_id": manifest_id, "events": events, "paths": paths,
            "compact_text": text, "compact_sha256": sha256(text),
            "before_units": utf16_units(state_text), "after_units": utf16_units(text),
            "max_cell_units": max_cell_units, "max_head_units": max_head_units,
            "history_keys": list(history_keys), "context": common}


def archive_requests(layout, plan):
    """Observation-only backup append. No STATE update and no shared guard."""
    return append_receipts(layout, plan["events"])


@dataclass(frozen=True)
class VerifiedArchive:
    manifest_event_id: str
    snapshot_sha256: str
    source_state_key: str
    source_text: str
    schema: str = VERSION


def verify_archive(layout, plan, observed_rows):
    """Return proof only after unique exact readback and lossless reconstruction."""
    rows = [list(row) for row in observed_rows]
    checked = verify_event_readback(layout, plan["events"], rows)
    if not checked["verified"]:
        raise ArchiveError("ARCHIVE_READBACK_UNRESOLVED:" + dumps(checked))
    mapped = {}
    for row in rows:
        item = dict(zip(layout.event_headers, row))
        mapped[item.get("event_id")] = item
    def payload(identity):
        raw = mapped[identity]["crm_payload"]
        return _object(raw) if isinstance(raw, str) else copy.deepcopy(raw)
    manifest = payload(plan["manifest_event_id"])
    if (manifest.get("schema") not in {VERSION, HISTORY_VERSION}
            or manifest.get("schema") != plan.get("schema", manifest.get("schema"))):
        raise ArchiveError("UNKNOWN_ARCHIVE_SCHEMA")
    texts = []
    for index, entry in enumerate(manifest["chunks"]):
        part = payload(entry["event_id"])
        if (part["index"] != index or entry["index"] != index or part["count"] != len(manifest["chunks"])
                or part["archive_id"] != manifest["archive_id"]
                or part["snapshot_sha256"] != manifest["snapshot_sha256"]
                or sha256(part["data"]) != part["data_sha256"] or part["data_sha256"] != entry["sha256"]):
            raise ArchiveError("ARCHIVE_CHUNK_INTEGRITY_FAILURE")
        texts.append(part["data"])
    source = "".join(texts)
    if (sha256(source) != manifest["snapshot_sha256"] or source != plan["source_text"]
            or sha256(source) != plan["snapshot_sha256"]
            or utf16_units(source) != manifest["snapshot_utf16_units"]
            or len(source.encode("utf-8")) != manifest["snapshot_utf8_bytes"]
            or manifest["source_state_key"] != plan["source_state_key"]):
        raise ArchiveError("ARCHIVE_SNAPSHOT_INTEGRITY_FAILURE")
    _object(source)
    return VerifiedArchive(plan["manifest_event_id"], sha256(source),
                           plan["source_state_key"], source, manifest["schema"])


def load_archive(layout, manifest_event_id, observed_rows, *, expected_source_key=None):
    """Load a durable archive using only its head reference and native rows.

    A later worker does not need the old local plan. The manifest identity binds
    source key, exact snapshot SHA, original run and timestamp; canonical event
    readback, chunk hashes and whole-snapshot reconstruction are then rechecked.
    Missing, duplicate or changed records fail rather than becoming empty history.
    """
    rows = [list(row) for row in observed_rows]
    by_id = {}
    for row in rows:
        item = dict(zip(layout.event_headers, row))
        by_id.setdefault(item.get("event_id"), []).append(item)
    def one(identity):
        matches = by_id.get(identity, [])
        if len(matches) != 1:
            raise ArchiveError("ARCHIVE_ID_MISSING_OR_DUPLICATE:" + str(identity))
        return matches[0]
    def data(row):
        value = row.get("crm_payload")
        return _object(value if isinstance(value, str) else dumps(value))
    try:
        manifest_row = one(manifest_event_id)
        manifest = data(manifest_row)
        version = manifest.get("schema")
        if version not in {VERSION, HISTORY_VERSION} or not isinstance(manifest.get("chunks"), list):
            raise ArchiveError("UNKNOWN_ARCHIVE_SCHEMA")
        if expected_source_key is not None and manifest.get("source_state_key") != expected_source_key:
            raise ArchiveError("ARCHIVE_SOURCE_KEY_MISMATCH")
        at, run_id, origin = manifest_row["occurred_at"], manifest["run_id"], manifest["origin"]
        identity_parts = [manifest["source_state_key"], manifest["snapshot_sha256"], run_id, at]
        if version == HISTORY_VERSION:
            identity_parts.append(version)
        identity = "state-archive:" + sha256(dumps(identity_parts))
        if identity != manifest.get("archive_id") or manifest_event_id != identity + ":manifest":
            raise ArchiveError("ARCHIVE_MANIFEST_IDENTITY_MISMATCH")
        if not 1 <= len(manifest["chunks"]) <= 256:
            raise ArchiveError("ARCHIVE_CHUNK_LIMIT")
        events, texts = [], []
        for index, entry in enumerate(manifest["chunks"]):
            if entry["event_id"] != identity + f":chunk:{index}":
                raise ArchiveError("ARCHIVE_CHUNK_IDENTITY_MISMATCH")
            chunk = data(one(entry["event_id"]))
            texts.append(chunk["data"])
            body = {k: v for k, v in chunk.items() if k not in {"run_id", "phase", "origin"}}
            events.append(_event(entry["event_id"], "STATE_ARCHIVE_CHUNK", body,
                                 at=at, run_id=run_id, origin=origin, version=version))
        body = {k: v for k, v in manifest.items() if k not in {"run_id", "phase", "origin"}}
        events.append(_event(manifest_event_id, "STATE_ARCHIVE_MANIFEST", body,
                             at=at, run_id=run_id, origin=origin, version=version))
        replay_plan = {"events": events, "manifest_event_id": manifest_event_id,
            "source_text": "".join(texts), "snapshot_sha256": manifest["snapshot_sha256"],
            "source_state_key": manifest["source_state_key"]}
        return verify_archive(layout, replay_plan, rows)
    except (KeyError, TypeError) as exc:
        raise ArchiveError("INVALID_ARCHIVE_MANIFEST:" + str(exc)) from exc


def hydrate_state(head, archives: Mapping[str, VerifiedArchive], *, resolve_only=None):
    """Restore original object shapes for legacy consumers; preserve live fields.

    ``archives`` must contain verified archives for every encountered reference.
    This is a read representation. Persist the fresh compact head, not a hydrated
    historical copy, when changing an unrelated cursor or pending transaction.
    """
    value = _object(head) if isinstance(head, str) else copy.deepcopy(head)
    def walk(node, depth=0):
        if depth > 64:
            raise ArchiveError("ARCHIVE_REFERENCE_DEPTH")
        if isinstance(node, dict) and REF in node:
            ref = node[REF]
            if not isinstance(ref, dict):
                raise ArchiveError("UNKNOWN_ARCHIVE_REFERENCE")
            if resolve_only is not None and ref.get("manifest_event_id") not in resolve_only:
                return copy.deepcopy(node)
            archive = archives.get(ref.get("manifest_event_id"))
            if not isinstance(archive, VerifiedArchive) or sha256(archive.source_text) != archive.snapshot_sha256:
                raise ArchiveError("VERIFIED_ARCHIVE_MISSING_OR_CHANGED")
            original = _at(_object(archive.source_text), ref["path"])
            if sha256(dumps(original)) != ref["record_sha256"]:
                raise ArchiveError("ARCHIVED_RECORD_HASH_MISMATCH")
            if node != _stub(original, ref["path"], archive.manifest_event_id):
                raise ArchiveError("ARCHIVE_STUB_CHANGED")
            return walk(copy.deepcopy(original), depth + 1)
        if isinstance(node, dict):
            return {key: walk(item, depth + 1) for key, item in node.items()}
        if isinstance(node, list):
            return [walk(item, depth + 1) for item in node]
        return node
    return walk(value)


def _maybe_at(state, path):
    try:
        return _at(state, path)
    except ArchiveError:
        return None


def _put(state, path, value):
    parent = state
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value


def _membership_rows(value):
    return (isinstance(value, list) and all(type(row) is int and row >= 2 for row in value)
            and len(set(value)) == len(value))


def _membership_scope(state, path):
    if path[0] == "calibration":
        identity = _maybe_at(state, ("calibration", "id"))
        return dumps(["calibration", identity]) if isinstance(identity, str) and identity else None
    if path in (("reviewed_source_rows",), ("verified_final_source_rows",)) or path[-1] == "current_policy_verified_rows":
        # Target policy is a request, not the scope of already completed work.
        version = state.get("version")
        if not isinstance(version, str) or not version:
            return None
        return dumps(["policy", state.get("policy_source_document"), version,
                      state.get("policy_sha256")])
    return "cumulative"


def _completed_selection(state):
    """Select only recognized completed facts; retain every unknown/live shape."""
    transactions = state.get("transactions", {})
    if not isinstance(transactions, dict):
        raise ArchiveError("TRANSACTIONS_MUST_BE_OBJECT")
    verified, pending_rows = [], set()
    for identity, tx in transactions.items():
        eligible = (isinstance(tx, dict) and tx.get("status") == "VERIFIED"
            and not any(tx.get(key) for key in ("pending", "pending_rows", "pending_transactions"))
            and tx.get("outcome") not in {"UNRESOLVED", "UNKNOWN"} and _row_count(tx) is not None)
        if eligible:
            verified.append(identity)
        elif isinstance(tx, dict) and isinstance(tx.get("rows"), list):
            for row in tx["rows"]:
                number = row.get("row") if isinstance(row, dict) else row
                if type(number) is int:
                    pending_rows.add(number)
    calibration_pending = _maybe_at(state, ("calibration", "pending_rows"))
    if _membership_rows(calibration_pending):
        pending_rows.update(calibration_pending)
    memberships = {}
    completed = _maybe_at(state, ("calibration", "completed_rows"))
    for path in MEMBERSHIP_PATHS:
        rows = _maybe_at(state, path)
        if not _membership_rows(rows) or _membership_scope(state, path) is None:
            continue
        selected = [row for row in rows if row not in pending_rows]
        if path == ("calibration", "selected_rows"):
            selected = [row for row in selected if _membership_rows(completed) and row in completed]
        if selected:
            memberships[path] = selected
    return memberships, verified


def _bounded_head(state, manifest_id, source_key, source_sha, history_keys):
    compact = copy.deepcopy(state)
    paths = []
    # Keep the bounded V1 baseline references compatible. Entire completed
    # transactions now move to the single V2 history reference instead of stubs.
    completed_scope = dict(state, target_policy_version=state.get("version"))
    for path in _selection(completed_scope, history_keys, manifest_id):
        if len(path) == 1:
            compact[path[0]] = _stub(_at(state, path), path, manifest_id)
            paths.append(path)
    memberships, transactions = _completed_selection(state)
    for path, rows in memberships.items():
        archived = set(rows)
        _put(compact, path, [row for row in _at(state, path) if row not in archived])
        paths.append(list(path))
    for identity in transactions:
        del compact["transactions"][identity]
        paths.append(["transactions", identity])
    compact[HISTORY_REF] = {"schema": HISTORY_VERSION, "manifest_event_id": manifest_id,
        "source_state_key": source_key, "snapshot_sha256": source_sha}
    return compact, paths


def prepare_bounded_archive(state_text, **kwargs):
    """Prepare V2 archive-first compaction; no append or head mutation occurs.

    The archive contains this entire exact head, including its previous history
    reference. The next head contains just one new reference and current deltas.
    Prepare from a current snapshot; any later drift rejects the finalizer.
    """
    return prepare_archive(state_text, _bounded_history=True, **kwargs)


def _checked_archive(archives, identity, source_key):
    archive = archives.get(identity)
    if (not isinstance(archive, VerifiedArchive) or archive.manifest_event_id != identity
            or archive.source_state_key != source_key
            or sha256(archive.source_text) != archive.snapshot_sha256):
        raise ArchiveError("VERIFIED_ARCHIVE_MISSING_OR_CHANGED:" + str(identity))
    return archive


def archive_requirements(head, archives=None, *, source_state_key):
    """Return missing manifest IDs, iteratively including older V1/V2 lineage.

    Callers fetch actual immutable manifest/chunk rows and use ``load_archive``;
    an absent record is never an empty history or permission to repeat work.
    """
    archives = archives or {}
    pending = [_object(head) if isinstance(head, str) else copy.deepcopy(head)]
    seen, missing = set(), set()
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for marker in (REF, HISTORY_REF):
                if marker not in node:
                    continue
                ref = node[marker]
                identity = ref.get("manifest_event_id") if isinstance(ref, dict) else None
                if not isinstance(identity, str) or not identity:
                    raise ArchiveError("UNKNOWN_ARCHIVE_REFERENCE")
                if identity in seen:
                    continue
                seen.add(identity)
                if identity not in archives:
                    missing.add(identity)
                else:
                    pending.append(_object(_checked_archive(archives, identity, source_state_key).source_text))
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return sorted(missing)


@dataclass(frozen=True)
class HistoryReadView:
    head_text: str
    source_state_key: str
    archives: Mapping[str, VerifiedArchive]
    _state: dict

    @property
    def state(self):
        """A mutable legacy-shaped copy; this representation must not be saved."""
        return copy.deepcopy(self._state)


def _history_projection(head_text, archives, source_key):
    head = _object(head_text)
    missing = archive_requirements(head, archives, source_state_key=source_key)
    if missing:
        raise ArchiveError("VERIFIED_ARCHIVE_MISSING_OR_CHANGED:" + dumps(missing))
    snapshots, seen, cursor = [], set(), head
    # Iterative lineage, with cycle detection: no fixed 64-generation lifetime.
    while HISTORY_REF in cursor:
        ref = cursor[HISTORY_REF]
        if (not isinstance(ref, dict) or set(ref) != {"schema", "manifest_event_id", "source_state_key", "snapshot_sha256"}
                or ref.get("schema") != HISTORY_VERSION or ref.get("source_state_key") != source_key):
            raise ArchiveError("UNKNOWN_HISTORY_REFERENCE")
        identity = ref["manifest_event_id"]
        if identity in seen:
            raise ArchiveError("HISTORY_REFERENCE_CYCLE")
        seen.add(identity)
        archive = _checked_archive(archives, identity, source_key)
        if archive.schema != HISTORY_VERSION or archive.snapshot_sha256 != ref["snapshot_sha256"]:
            raise ArchiveError("HISTORY_REFERENCE_HASH_OR_SCHEMA_CHANGED")
        cursor = _object(archive.source_text)
        snapshots.append(cursor)
    snapshots.reverse()
    histories, anchors, historic_tx = {}, {}, {}
    for snapshot in snapshots:
        memberships, identities = _completed_selection(snapshot)
        for path, rows in memberships.items():
            if _membership_scope(snapshot, path) != _membership_scope(head, path):
                continue
            histories.setdefault(path, set()).update(rows)
        for path in MEMBERSHIP_PATHS:
            rows = _maybe_at(snapshot, path)
            if (_membership_rows(rows) and _membership_scope(snapshot, path) is not None
                    and _membership_scope(snapshot, path) == _membership_scope(head, path)):
                anchors.setdefault(path, []).extend(rows)
        for identity in identities:
            tx = hydrate_state(snapshot["transactions"][identity], archives)
            if identity in historic_tx and historic_tx[identity] != tx:
                raise ArchiveError("ARCHIVED_TRANSACTION_CHANGED:" + identity)
            historic_tx[identity] = tx
    logical = hydrate_state(head, archives)
    logical.pop(HISTORY_REF, None)
    for path, archived in histories.items():
        live = _maybe_at(head, path)
        if not _membership_rows(live):
            raise ArchiveError("ARCHIVED_MEMBERSHIP_PATH_MISSING_OR_CHANGED:" + dumps(path))
        allowed = archived | set(live)
        # Old arrays are order anchors, not additional members. This preserves
        # an incomplete selection interleaved between completed selections.
        ordered = list(dict.fromkeys(row for row in anchors.get(path, []) + live if row in allowed))
        _put(logical, path, ordered)
    if historic_tx:
        live = logical.get("transactions")
        if not isinstance(live, dict):
            raise ArchiveError("ARCHIVED_TRANSACTIONS_PATH_MISSING")
        for identity, tx in historic_tx.items():
            if identity in live and live[identity] != tx:
                raise ArchiveError("ARCHIVED_TRANSACTION_CHANGED:" + identity)
        logical["transactions"] = dict(historic_tx, **live)
    return logical, histories, historic_tx


def history_read_view(head, archives, *, source_state_key):
    """Reconstruct memberships and full transaction identities for read/compute.

    No counts, cursors, PASS/REVIEW values, unknowns or opposite-lane data are
    inferred. A VERIFIED persistence receipt is not a business PASS decision.
    Use ``merge_next_head`` to serialize any resulting logical transition.
    """
    text = head if isinstance(head, str) else dumps(head)
    logical, _, _ = _history_projection(text, archives, source_state_key)
    return HistoryReadView(text, source_state_key, dict(archives), logical)


def load_history_view(layout, head, observed_rows, *, source_state_key):
    """Build a future worker's read view solely from head and native event rows."""
    archives, rows = {}, [list(row) for row in observed_rows]
    while True:
        missing = archive_requirements(head, archives, source_state_key=source_state_key)
        if not missing:
            return history_read_view(head, archives, source_state_key=source_state_key)
        for identity in missing:
            archives[identity] = load_archive(layout, identity, rows, expected_source_key=source_state_key)


def merge_next_head(view, updated_state, fresh_state_text, *, max_head_units=MAX_HEAD_UNITS):
    """Convert a legacy logical transition into a bounded live-delta head.

    Call under the existing V4 guard with a freshly read exact STATE literal.
    Same-scope archived memberships/transactions are immutable; additions are
    retained only as new deltas. The caller owns validation, counts and business
    decisions. This function does not perform a remote CAS or a write.
    """
    if not isinstance(view, HistoryReadView) or fresh_state_text != view.head_text:
        raise ArchiveError("STATE_SNAPSHOT_CHANGED_NO_WRITE")
    if not 1000 <= max_head_units <= MAX_HEAD_UNITS:
        raise ArchiveError("INVALID_CELL_BUDGET")
    original = _object(view.head_text)
    before, histories, transactions = _history_projection(view.head_text, view.archives, view.source_state_key)
    proposed = _object(dumps(updated_state))
    if HISTORY_REF in proposed:
        raise ArchiveError("HISTORY_METADATA_IS_NOT_LOGICAL_STATE")
    compact = copy.deepcopy(proposed)
    for path, archived in histories.items():
        if _membership_scope(before, path) != _membership_scope(proposed, path):
            raise ArchiveError("HISTORY_SCOPE_CHANGED_REBUILD_VIEW")
        rows = _maybe_at(proposed, path)
        if not _membership_rows(rows) or not archived.issubset(rows):
            raise ArchiveError("ARCHIVED_MEMBERSHIP_REMOVED_OR_CHANGED:" + dumps(path))
        _put(compact, path, [row for row in rows if row not in archived])
    for identity, tx in transactions.items():
        if _maybe_at(proposed, ("transactions", identity)) != tx:
            raise ArchiveError("ARCHIVED_TRANSACTION_REMOVED_OR_CHANGED:" + identity)
        del compact["transactions"][identity]

    def restore_v1(base, read, new):
        if isinstance(base, dict) and REF in base:
            if new != read:
                raise ArchiveError("ARCHIVED_READ_ONLY_FIELD_CHANGED")
            return copy.deepcopy(base)
        if isinstance(base, dict) and isinstance(new, dict):
            result = copy.deepcopy(new)
            for key, value in base.items():
                if key == HISTORY_REF:
                    continue
                if isinstance(value, dict) and REF in value and key not in new:
                    raise ArchiveError("ARCHIVED_READ_ONLY_FIELD_REMOVED")
                if key in new and isinstance(read, dict) and key in read:
                    result[key] = restore_v1(value, read[key], new[key])
            return result
        if isinstance(base, list) and isinstance(new, list) and isinstance(read, list) and len(base) == len(new) == len(read):
            return [restore_v1(b, r, n) for b, r, n in zip(base, read, new)]
        return copy.deepcopy(new)

    compact = restore_v1(original, before, compact)
    if HISTORY_REF in original:
        compact[HISTORY_REF] = copy.deepcopy(original[HISTORY_REF])
    text = dumps(compact)
    if utf16_units(text) > max_head_units:
        raise ArchiveError("NEXT_HEAD_EXCEEDS_BUDGET_NO_WRITE")
    restored, _, _ = _history_projection(text, view.archives, view.source_state_key)
    if restored != proposed:
        raise ArchiveError("NEXT_HEAD_MERGE_NOT_LOSSLESS")
    return text


def compact_verified(plan, verified, fresh_state_text, *, archive_dependencies=None):
    """Compare the exact fresh literal snapshot; failure returns no pruned state."""
    if fresh_state_text != plan["source_text"] or sha256(fresh_state_text) != plan["snapshot_sha256"]:
        raise ArchiveError("STATE_SNAPSHOT_CHANGED_NO_PRUNING")
    if (not isinstance(verified, VerifiedArchive) or verified.source_text != fresh_state_text
            or verified.snapshot_sha256 != plan["snapshot_sha256"]
            or verified.manifest_event_id != plan["manifest_event_id"]
            or verified.source_state_key != plan["source_state_key"]):
        raise ArchiveError("VERIFIED_ARCHIVE_REQUIRED_NO_PRUNING")
    if sha256(plan["compact_text"]) != plan["compact_sha256"]:
        raise ArchiveError("COMPACT_HEAD_CHANGED")
    source = _object(fresh_state_text)
    if plan.get("schema") == HISTORY_VERSION:
        expected_head, expected_paths = _bounded_head(source, plan["manifest_event_id"],
            plan["source_state_key"], plan["snapshot_sha256"], tuple(plan["history_keys"]))
        if expected_paths != plan["paths"] or dumps(expected_head) != plan["compact_text"]:
            raise ArchiveError("COMPACTION_SELECTION_CHANGED_NO_PRUNING")
        archives = dict(archive_dependencies or {})
        before = history_read_view(fresh_state_text, archives, source_state_key=plan["source_state_key"])
        archives[verified.manifest_event_id] = verified
        after = history_read_view(plan["compact_text"], archives, source_state_key=plan["source_state_key"])
        if after.state != before.state:
            raise ArchiveError("COMPACTION_NOT_LOSSLESS")
        if utf16_units(plan["compact_text"]) > plan["max_head_units"]:
            raise ArchiveError("COMPACT_HEAD_EXCEEDS_BUDGET_NO_PRUNING")
        return plan["compact_text"]
    expected_paths = _selection(source, tuple(plan["history_keys"]), plan["manifest_event_id"])
    expected_head = copy.deepcopy(source)
    for path in expected_paths:
        parent = expected_head if len(path) == 1 else expected_head[path[0]]
        parent[path[-1]] = _stub(_at(source, path), path, plan["manifest_event_id"])
    if expected_paths != plan["paths"] or dumps(expected_head) != plan["compact_text"]:
        raise ArchiveError("COMPACTION_SELECTION_CHANGED_NO_PRUNING")
    # Resolve this generation only. Older references remain byte-for-byte equal
    # to the prior head; full audit hydration requires their earlier archives.
    restored = hydrate_state(plan["compact_text"], {verified.manifest_event_id: verified},
                             resolve_only={verified.manifest_event_id})
    if restored != _object(fresh_state_text):
        raise ArchiveError("COMPACTION_NOT_LOSSLESS")
    if utf16_units(plan["compact_text"]) > plan["max_head_units"]:
        raise ArchiveError("COMPACT_HEAD_EXCEEDS_BUDGET_NO_PRUNING")
    return plan["compact_text"]


def compaction_requests(layout, lease, now, *, state_row, state_column,
                        plan, observed_archive_rows, fresh_state_text, archive_dependencies=None):
    """Call only with a fresh STATE read under this exact active V4 guard.

    Re-verifies archive readback, checks the literal snapshot, and returns the
    existing atomic owner-fence/state/event/clear/delete finalizer. It performs
    no acquisition, retries, reads, writes, or external actions itself.
    """
    verified = verify_archive(layout, plan, observed_archive_rows)
    head = compact_verified(plan, verified, fresh_state_text, archive_dependencies=archive_dependencies)
    event = _event(plan["archive_id"] + ":compacted", "STATE_COMPACTED",
        {"source_state_key": plan["source_state_key"], "manifest_event_id": plan["manifest_event_id"],
         "before_sha256": plan["snapshot_sha256"], "after_sha256": sha256(head),
         "before_utf16_units": plan["before_units"], "after_utf16_units": utf16_units(head),
         "archived_paths": plan["paths"], "company_rows_changed": 0, "customer_actions": 0},
        **dict(plan["context"], at=now.isoformat()))
    update = cell_update(layout.config_sheet_id, state_row, state_column, head)
    return commit_and_release_requests(layout, lease, now, [update], [event])


def main(argv=None):
    """Prepare or finalize local JSON artifacts. This CLI has no remote client."""
    import argparse
    from pathlib import Path
    from sheets_persistence import Layout, Lease

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--state-file", required=True)
    prepare.add_argument("--source-state-key", required=True)
    prepare.add_argument("--at", required=True)
    prepare.add_argument("--run-id", required=True)
    prepare.add_argument("--origin", required=True)
    prepare.add_argument("--max-cell-units", type=int, default=MAX_EVENT_CELL_UNITS)
    prepare.add_argument("--bounded-history", action="store_true",
                         help="Use V2 one-reference history and live deltas")
    prepare.add_argument("--layout-file", required=True)
    prepare.add_argument("--output-dir", required=True)
    finish = commands.add_parser("finalize")
    finish.add_argument("--plan-file", required=True)
    finish.add_argument("--layout-file", required=True)
    finish.add_argument("--archive-readback-file", required=True)
    finish.add_argument("--dependency-readback-file",
                        help="Previously fetched native event rows for all older V1/V2 lineage")
    finish.add_argument("--fresh-state-file", required=True)
    finish.add_argument("--lease-file", required=True)
    finish.add_argument("--now", required=True)
    finish.add_argument("--state-row", required=True, type=int)
    finish.add_argument("--state-column", type=int, default=2)
    finish.add_argument("--output-file", required=True)
    args = parser.parse_args(argv)

    def read(path):
        return Path(path).read_bytes().decode("utf-8")
    def write(path, data):
        Path(path).write_bytes((dumps(data) + "\n").encode("utf-8"))
    layout_data = _object(read(args.layout_file))
    layout_data["event_headers"] = tuple(layout_data["event_headers"])
    layout = Layout(**layout_data)
    if args.command == "prepare":
        planner = prepare_bounded_archive if args.bounded_history else prepare_archive
        plan = planner(read(args.state_file), source_state_key=args.source_state_key,
            at=args.at, run_id=args.run_id, origin=args.origin, max_cell_units=args.max_cell_units)
        output = Path(args.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        write(output / "archive_plan.json", plan)
        write(output / "archive_append_requests.json", {"requests": archive_requests(layout, plan)})
        # Explicitly a preview: no STATE update request exists until archive
        # readback and a fresh under-guard snapshot pass the finalize command.
        (output / "compacted_state_preview.json").write_bytes(plan["compact_text"].encode("utf-8"))
        summary = {"status": "PREPARED_ARCHIVE_ONLY", "state_updates_ready": False,
            "snapshot_sha256": plan["snapshot_sha256"], "manifest_event_id": plan["manifest_event_id"],
            "before_utf16_units": plan["before_units"], "after_utf16_units": plan["after_units"],
            "archive_event_count": len(plan["events"]), "archived_paths": plan["paths"],
            "requires_before_state_write": ["exact archive readback and reconstruction",
                "acquire current exact shared guard", "fresh literal STATE under that guard",
                "finalize command with that lease and unchanged snapshot", "post-commit readback"]}
        write(output / "preparation_summary.json", summary)
        print(dumps(summary))
    else:
        plan = _object(read(args.plan_file))
        observed = json.loads(read(args.archive_readback_file))
        if not isinstance(observed, list):
            raise ArchiveError("ARCHIVE_READBACK_MUST_BE_ACTUAL_ROW_ARRAY")
        dependencies = {}
        if plan.get("schema") == HISTORY_VERSION:
            prior_rows = json.loads(read(args.dependency_readback_file)) if args.dependency_readback_file else observed
            if not isinstance(prior_rows, list):
                raise ArchiveError("ARCHIVE_READBACK_MUST_BE_ACTUAL_ROW_ARRAY")
            dependencies = load_history_view(layout, plan["source_text"], prior_rows,
                source_state_key=plan["source_state_key"]).archives
        owner = _object(read(args.lease_file))
        lease = Lease(owner["owner"], owner["token"],
            datetime.fromisoformat(owner["acquired_at"].replace("Z", "+00:00")),
            datetime.fromisoformat(owner["expires_at"].replace("Z", "+00:00")))
        requests = compaction_requests(layout, lease,
            datetime.fromisoformat(args.now.replace("Z", "+00:00")), state_row=args.state_row,
            state_column=args.state_column, plan=plan, observed_archive_rows=observed,
            fresh_state_text=read(args.fresh_state_file), archive_dependencies=dependencies)
        write(args.output_file, {"requests": requests})
        print(dumps({"status": "FENCED_FINALIZER_PREPARED", "request_count": len(requests),
            "snapshot_sha256": plan["snapshot_sha256"], "remote_writes": 0}))


if __name__ == "__main__":
    main()
