"""Facts a ``verify --json`` line reports from a bundle as claimed (not verified), split out of
cli.py for the 400-line cap. Any unreadable value is reported as null."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from cairnwork_attest.statement import TEST_SOURCES

_OID = re.compile(r"^[0-9a-f]{40}$")


def bundle_facts(path: Path) -> dict[str, Any]:
    nulls: dict[str, Any] = {
        "base": None,
        "statement_sha256": None,
        "core_status": None,
        "keyid": None,
        "test_source": None,
    }
    try:
        content = path.read_bytes()
        envelope = json.loads(content)
        raw_payload = base64.b64decode(envelope["payload"], validate=True)
        statement = json.loads(raw_payload)
        base = None
        core_status = None
        keyid_val = None
        source = None
        if isinstance(envelope, dict):
            sigs = envelope.get("signatures")
            if isinstance(sigs, list) and sigs and isinstance(sigs[0], dict):
                k = sigs[0].get("keyid")
                if isinstance(k, str):
                    keyid_val = k
        if isinstance(statement, dict):
            pred = statement.get("predicate")
            if isinstance(pred, dict):
                b = pred.get("base_commit")
                if isinstance(b, str) and _OID.fullmatch(b):
                    base = b
                st = pred.get("status")
                if isinstance(st, str):
                    core_status = st
                if pred.get("test_source") in TEST_SOURCES:
                    source = pred["test_source"]
        return {
            "test_source": source,
            "base": base,
            "statement_sha256": hashlib.sha256(raw_payload).hexdigest(),
            "core_status": core_status,
            "keyid": keyid_val,
        }
    except (OSError, ValueError, UnicodeDecodeError, KeyError, TypeError, binascii.Error):
        return nulls
