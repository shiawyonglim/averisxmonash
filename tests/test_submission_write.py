"""Regression tests for server._write_submission.

Guards the data-loss bug where _write_submission() serialized only the
in-memory SUBMISSION_STORE (empty at boot / partial after a small agent
batch), truncating the on-disk submission.json.

Importing server.py executes _init_verdicts_from_submission(), which only
READS the real submission.json and inbox files — it never writes. The
fixture below repoints SUBMISSION_PATH at a pytest tmp_path and swaps the
in-memory store, so the real file is never touched.
"""

import json
import os
import sys
import importlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
server = importlib.import_module("server")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point SUBMISSION_PATH at a tmp file; swap in a fake in-memory store."""
    target = tmp_path / "submission.json"
    monkeypatch.setattr(server, "SUBMISSION_PATH", str(target))
    saved = dict(server.SUBMISSION_STORE)
    server.SUBMISSION_STORE.clear()
    try:
        yield target
    finally:
        server.SUBMISSION_STORE.clear()
        server.SUBMISSION_STORE.update(saved)


def _write_disk(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_write_submission_merges_disk_entries(isolated):
    # Disk holds 3 entries the store doesn't know about (the bug scenario:
    # SUBMISSION_STORE starts empty at boot).
    on_disk = {f"email_{i:03d}": {"status": "OK", "n": i} for i in range(3)}
    _write_disk(isolated, on_disk)

    server.SUBMISSION_STORE.update({
        "email_001": {"status": "MISMATCH", "n": "fresh"},  # overlaps disk
        "email_010": {"status": "NEEDS_REVIEW"},            # new entry
    })
    server._write_submission()

    merged = json.loads(isolated.read_text(encoding="utf-8"))
    assert len(merged) == 4
    assert merged["email_000"]["status"] == "OK"          # disk-only preserved
    assert merged["email_002"]["status"] == "OK"          # disk-only preserved
    assert merged["email_001"]["status"] == "MISMATCH"    # memory wins overlap
    assert merged["email_001"]["n"] == "fresh"
    assert merged["email_010"]["status"] == "NEEDS_REVIEW"
    # Store and disk now agree on the merged contents.
    assert server.SUBMISSION_STORE["email_000"]["status"] == "OK"


def test_write_submission_allow_shrink_truncates(isolated):
    _write_disk(isolated, {f"email_{i:03d}": {"status": "OK"} for i in range(5)})
    server.SUBMISSION_STORE.update({"email_777": {"status": "OK"}})
    server._write_submission(allow_shrink=True)
    merged = json.loads(isolated.read_text(encoding="utf-8"))
    assert merged == {"email_777": {"status": "OK"}}


def test_write_submission_atomic_and_valid(isolated):
    server.SUBMISSION_STORE.update({"email_001": {"status": "OK"}})
    server._write_submission()
    assert not os.path.exists(str(isolated) + ".tmp")
    data = json.loads(isolated.read_text(encoding="utf-8"))
    assert data["email_001"]["status"] == "OK"
