from __future__ import annotations


import hashlib
from dataclasses import dataclass




@dataclass(frozen=True)
class GateDefinition:
    doc_id: str
    text: str
    sha256: str
    modified_time: str


    @property
    def version(self) -> str:
        return f"sha256:{self.sha256}"




class GateLoader:
    """Loads the authoritative human-editable screening gate from Google Drive.


    The worker must not contain an independent copy of G1-G6. The latest Google
    Doc content is loaded for every company evaluation. If the document cannot
    be read or is structurally incomplete, evaluation fails closed.
    """


    REQUIRED_MARKERS = (
        "G1 Geography",
        "G2 AUMS Fit",
        "G3 Commercial Proof",
        "G4 Economic Capacity",
        "G5 Cross-Cultural Expansion",
        "G6 Japan Whitespace",
        "GO",
        "NO-GO",
    )


    def __init__(self, sheets_repo, drive_repo):
        self.sheets = sheets_repo
        self.drive = drive_repo


    def load(self) -> GateDefinition:
        config = self.sheets.get_config()
        source = config.get("TARGET_SCREENING_GATE_SOURCE", "").strip().upper()
        doc_id = config.get("TARGET_SCREENING_GATE_DOC_ID", "").strip()
        read_mode = config.get("TARGET_SCREENING_GATE_READ_MODE", "").strip().upper()
        fail_closed = config.get("TARGET_SCREENING_GATE_FAIL_CLOSED", "TRUE").strip().upper() == "TRUE"


        if source != "GOOGLE_DOC":
            raise RuntimeError(f"unsupported_gate_source:{source or 'EMPTY'}")
        if not doc_id:
            raise RuntimeError("missing_gate_doc_id")
        if read_mode != "LIVE_PER_EVALUATION":
            raise RuntimeError(f"unsafe_gate_read_mode:{read_mode or 'EMPTY'}")


        try:
            text, meta = self.drive.read_plain_text(doc_id)
        except Exception as exc:
            if fail_closed:
                raise RuntimeError(f"gate_doc_read_failed:{type(exc).__name__}:{exc}") from exc
            raise


        text = (text or "").strip()
        if not text:
            raise RuntimeError("gate_doc_empty")


        missing = [marker for marker in self.REQUIRED_MARKERS if marker not in text]
        if missing:
            raise RuntimeError("gate_doc_incomplete:" + ",".join(missing))


        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return GateDefinition(
            doc_id=doc_id,
            text=text,
            sha256=digest,
            modified_time=str(meta.get("modifiedTime", "")),
        )