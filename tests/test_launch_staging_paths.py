"""Страж staging-каталога запуска против по-целевого суффикса manifest_id.

Разворот манифеста в exact claims (agent/launcher) даёт manifest_id вида
«<staging_uuid>:<ordinal>», а staged-каталог у предложения один — под базовым
uuid. Сверка с полным id не сходилась никогда: ни один запуск не прошёл live
review за всю историю контура (запуски легли LIVE_REVIEW_FAILED одной
волной).
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from services import approval_sources


def _manifest(tmp_path: Path, manifest_id: str, directory_name: str):
    creative = SimpleNamespace(body_staged_relative_path="bodies/body-0.txt")
    destination = SimpleNamespace(creatives=(creative,))
    asset = SimpleNamespace(staged_relative_path="media/video.mp4")
    return SimpleNamespace(
        manifest_id=manifest_id,
        staging_root=str(tmp_path),
        staging_directory=str(tmp_path / directory_name),
        media_assets=(asset,),
        destinations=(destination,),
    )


def test_target_suffixed_manifest_id_matches_base_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "REPORT_CHECKER_STAGING_ROOT", tmp_path)

    paths = approval_sources._launch_paths(
        _manifest(tmp_path, "5a9f90ae-7e13:0", "5a9f90ae-7e13")
    )

    # Пути к файлам тоже строятся от базы: каталога «uuid:0» на диске нет.
    assert paths == (
        "5a9f90ae-7e13/media/video.mp4",
        "5a9f90ae-7e13/bodies/body-0.txt",
    )


def test_plain_manifest_id_still_matches_its_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "REPORT_CHECKER_STAGING_ROOT", tmp_path)

    paths = approval_sources._launch_paths(
        _manifest(tmp_path, "5a9f90ae-7e13", "5a9f90ae-7e13")
    )

    assert paths[0].startswith("5a9f90ae-7e13/")


def test_foreign_directory_is_still_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "REPORT_CHECKER_STAGING_ROOT", tmp_path)

    with pytest.raises(ValueError, match="закрытым root"):
        approval_sources._launch_paths(
            _manifest(tmp_path, "5a9f90ae-7e13:0", "чужая-папка")
        )


@pytest.mark.parametrize("bad_id", [":0", "..:0", "a/b:0"])
def test_unsafe_manifest_id_base_is_rejected(monkeypatch, tmp_path, bad_id):
    monkeypatch.setattr(config, "REPORT_CHECKER_STAGING_ROOT", tmp_path)

    with pytest.raises(ValueError):
        approval_sources._launch_paths(_manifest(tmp_path, bad_id, "любая"))
