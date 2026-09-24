from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from services import approval_source_media
from services.approval_checker_models import EvidenceRequest, SourceSystem


def _request(paths: tuple[str, ...]) -> EvidenceRequest:
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    return EvidenceRequest(
        request_id="media-test",
        purpose="ACTION",
        action_kind=None,
        generated_at=now,
        subjects=(),
        claims=(),
        required_sources=(SourceSystem.MEDIA_BYTES,),
        windows=(),
        account_ids=(),
        adset_ids=(),
        ad_ids=(),
        card_ids=(),
        staged_relative_paths=paths,
        include_full_inventory=False,
        force_live=True,
        max_age_seconds=60,
    )


def test_media_hashes_actual_bytes_and_preserves_order(tmp_path, monkeypatch):
    root = tmp_path / "staging"
    manifest = root / "manifest-1"
    manifest.mkdir(parents=True)
    (manifest / "second.bin").write_bytes(b"second")
    (manifest / "first.bin").write_bytes(b"first")
    monkeypatch.setattr(approval_source_media, "REPORT_CHECKER_STAGING_ROOT", root)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)

    evidence = approval_source_media.load_media_evidence(
        _request(("manifest-1/second.bin", "manifest-1/first.bin")), now
    )

    assert evidence.complete is True
    assert evidence.records[0].value == hashlib.sha256(b"second").hexdigest()
    assert evidence.records[0].entity_ids[1] == "order:0"
    assert evidence.records[1].value == hashlib.sha256(b"first").hexdigest()
    assert evidence.records[1].entity_ids[1] == "order:1"


def test_media_rejects_symlink(tmp_path, monkeypatch):
    root = tmp_path / "staging"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    (root / "linked.bin").symlink_to(outside)
    monkeypatch.setattr(approval_source_media, "REPORT_CHECKER_STAGING_ROOT", root)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)

    evidence = approval_source_media.load_media_evidence(_request(("linked.bin",)), now)

    assert evidence.complete is False
    assert evidence.error_code == "MEDIA_FILE_UNSAFE"


@pytest.mark.parametrize("unsafe", ("../outside.bin", "/tmp/outside.bin", "a\\b.bin"))
def test_media_rejects_path_escape(tmp_path, monkeypatch, unsafe):
    root = tmp_path / "staging"
    root.mkdir()
    monkeypatch.setattr(approval_source_media, "REPORT_CHECKER_STAGING_ROOT", root)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)

    evidence = approval_source_media.load_media_evidence(_request((unsafe,)), now)

    assert evidence.complete is False


def test_text_hash_normalizes_line_endings_and_unicode(tmp_path, monkeypatch):
    root = tmp_path / "staging"
    root.mkdir()
    (root / "body.txt").write_bytes("Cafe\u0301\r\nline".encode())
    monkeypatch.setattr(approval_source_media, "REPORT_CHECKER_STAGING_ROOT", root)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)

    evidence = approval_source_media.load_media_evidence(_request(("body.txt",)), now)

    expected = hashlib.sha256("Café\nline".encode()).hexdigest()
    assert evidence.complete is True
    assert evidence.records[1].value == expected

