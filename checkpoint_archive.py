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
HISTORY_KEYS = ("baseline_v5", "baseline_v6", "baseline_v7", "baseline_v8",
                "baseline_v9", "calibration_previous")
COMPACT_AT_UNITS = 20_000
MAX_HEAD_UNITS = 40_000
MAX_EVENT_CELL_UNITS = 20_000
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_TX_KEEP = ("status", "lane", "rows", "identity", "decision_id", "packet_sha",
            "next_cursor_candidate", "policy_version", "verified_at", "row_count")


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


def _event(identity, action, payload, *, at, run_id, origin):
    return {"event_id": identity, "occurred_at": at, "recorded_at": at,
            "action_type": action, "source": "CHECKPOINT_ARCHIVE",
            "writer": "checkpoint_archive", "code_version": VERSION,
            "idempotency_key": identity, "canonical_action_id": identity,
            "reason": "Lossless verified-history archival; zero customer actions",
            "crm_payload": dict(payload, run_id=run_id, phase=action, origin=origin),
            "crm_result": "RECORDED"}


def prepare_archive(state_text, *, source_state_key, at, run_id, origin,
                    history_keys=HISTORY_KEYS, max_cell_units=MAX_EVENT_CELL_UNITS,
                    max_head_units=MAX_HEAD_UNITS):
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
    archive_id = "state-archive:" + sha256(dumps([source_state_key, source_sha, run_id, at]))
    manifest_id = archive_id + ":manifest"
    paths = _selection(state, tuple(history_keys), manifest_id)
    if not paths:
        raise ArchiveError("NO_ELIGIBLE_HISTORY_TO_COMPACT")
    common = dict(at=at, run_id=run_id, origin=origin)
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
    manifest = {"schema": VERSION, "archive_id": archive_id, "source_state_key": source_state_key,
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
    compact = copy.deepcopy(state)
    for path in paths:
        parent = compact if len(path) == 1 else compact[path[0]]
        parent[path[-1]] = _stub(_at(state, path), path, manifest_id)
    text = dumps(compact)
    if utf16_units(text) > max_head_units:
        raise ArchiveError("COMPACT_HEAD_EXCEEDS_BUDGET_NO_PRUNING")
    return {"schema": VERSION, "source_state_key": source_state_key, "source_text": state_text,
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
    return VerifiedArchive(plan["manifest_event_id"], sha256(source), plan["source_state_key"], source)


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
        if manifest.get("schema") != VERSION or not isinstance(manifest.get("chunks"), list):
            raise ArchiveError("UNKNOWN_ARCHIVE_SCHEMA")
        if expected_source_key is not None and manifest.get("source_state_key") != expected_source_key:
            raise ArchiveError("ARCHIVE_SOURCE_KEY_MISMATCH")
        at, run_id, origin = manifest_row["occurred_at"], manifest["run_id"], manifest["origin"]
        identity = "state-archive:" + sha256(dumps([manifest["source_state_key"],
            manifest["snapshot_sha256"], run_id, at]))
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
                                 at=at, run_id=run_id, origin=origin))
        body = {k: v for k, v in manifest.items() if k not in {"run_id", "phase", "origin"}}
        events.append(_event(manifest_event_id, "STATE_ARCHIVE_MANIFEST", body,
                             at=at, run_id=run_id, origin=origin))
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


def compact_verified(plan, verified, fresh_state_text):
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
                        plan, observed_archive_rows, fresh_state_text):
    """Call only with a fresh STATE read under this exact active V4 guard.

    Re-verifies archive readback, checks the literal snapshot, and returns the
    existing atomic owner-fence/state/event/clear/delete finalizer. It performs
    no acquisition, retries, reads, writes, or external actions itself.
    """
    verified = verify_archive(layout, plan, observed_archive_rows)
    head = compact_verified(plan, verified, fresh_state_text)
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
    prepare.add_argument("--layout-file", required=True)
    prepare.add_argument("--output-dir", required=True)
    finish = commands.add_parser("finalize")
    finish.add_argument("--plan-file", required=True)
    finish.add_argument("--layout-file", required=True)
    finish.add_argument("--archive-readback-file", required=True)
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
        plan = prepare_archive(read(args.state_file), source_state_key=args.source_state_key,
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
        owner = _object(read(args.lease_file))
        lease = Lease(owner["owner"], owner["token"],
            datetime.fromisoformat(owner["acquired_at"].replace("Z", "+00:00")),
            datetime.fromisoformat(owner["expires_at"].replace("Z", "+00:00")))
        requests = compaction_requests(layout, lease,
            datetime.fromisoformat(args.now.replace("Z", "+00:00")), state_row=args.state_row,
            state_column=args.state_column, plan=plan, observed_archive_rows=observed,
            fresh_state_text=read(args.fresh_state_file))
        write(args.output_file, {"requests": requests})
        print(dumps({"status": "FENCED_FINALIZER_PREPARED", "request_count": len(requests),
            "snapshot_sha256": plan["snapshot_sha256"], "remote_writes": 0}))


if __name__ == "__main__":
    main()
