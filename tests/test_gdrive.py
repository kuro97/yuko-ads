"""Тесты Google Drive интеграции (парсинг URL)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from integrations.gdrive import extract_file_id


def test_extract_file_id_standard():
    """Стандартная ссылка /file/d/{id}/view."""
    url = "https://drive.google.com/file/d/1AbCdEfG_HiJk/view"
    assert extract_file_id(url) == "1AbCdEfG_HiJk"


def test_extract_file_id_with_query():
    """Ссылка с id= параметром."""
    url = "https://drive.google.com/open?id=1XyZ_AbCdEf"
    assert extract_file_id(url) == "1XyZ_AbCdEf"


def test_extract_file_id_folder():
    """Ссылка на папку."""
    url = "https://drive.google.com/drive/folders/1FolDerId123"
    assert extract_file_id(url) == "1FolDerId123"


def test_extract_file_id_with_params():
    """Ссылка с дополнительными параметрами."""
    url = "https://drive.google.com/file/d/1Test_File-ID/view?usp=sharing"
    assert extract_file_id(url) == "1Test_File-ID"


def test_extract_file_id_invalid():
    """Невалидная ссылка — None."""
    assert extract_file_id("https://example.com/not-a-drive-link") is None


def test_extract_file_id_empty():
    """Пустая строка — None."""
    assert extract_file_id("") is None


# ---------------------------------------------------------------------------
# Фолбэк прямого скачивания при падении gdown (rate limit «many accesses»,
# без фолбэка — PREFLIGHT_INVALID на каждом запуске карточки)
# ---------------------------------------------------------------------------

from collections import namedtuple
from unittest import mock

from integrations import gdrive

_Entry = namedtuple("GoogleDriveFileToDownload", ["id", "path", "local_path"])


def test_folder_fallback_downloads_each_file_direct(tmp_path):
    """gdown.download_folder упал → листинг + прямое скачивание каждого файла."""
    folder = str(tmp_path / "out")
    entries = [
        _Entry("id1", "1", str(tmp_path / "out" / "1")),
        _Entry("id2", "2", str(tmp_path / "out" / "2")),
    ]
    calls = []

    def fake_download_folder(id=None, output=None, quiet=None, skip_download=False):
        if not skip_download:
            raise RuntimeError("Cannot retrieve the public link of the file.")
        return entries

    def fake_direct(file_id, output):
        calls.append((file_id, output))
        with open(output, "wb") as fh:
            fh.write(b"\xff\xd8\xffdata")

    with mock.patch.object(gdrive.gdown, "download_folder", fake_download_folder), \
         mock.patch.object(gdrive, "_download_file_direct", fake_direct):
        gdrive._download_folder_with_fallback("folder-id", folder)

    assert calls == [("id1", entries[0].local_path), ("id2", entries[1].local_path)]
    import os
    assert all(os.path.exists(e.local_path) for e in entries)


def test_folder_fallback_raises_when_listing_empty(tmp_path):
    """Листинг пуст → честная ошибка, а не тихий успех без файлов."""
    def fake_download_folder(id=None, output=None, quiet=None, skip_download=False):
        if not skip_download:
            raise RuntimeError("boom")
        return []

    with mock.patch.object(gdrive.gdown, "download_folder", fake_download_folder):
        try:
            gdrive._download_folder_with_fallback("folder-id", str(tmp_path))
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("ожидали FileNotFoundError при пустом листинге")


def test_single_file_fallback_direct(tmp_path, monkeypatch):
    """gdown.download упал → файл качается напрямую, тип по magic bytes."""
    def fake_download(id=None, output=None, quiet=None):
        raise RuntimeError("Cannot retrieve the public link of the file.")

    def fake_direct(file_id, output):
        with open(output, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\ndata")

    monkeypatch.setattr(gdrive.gdown, "download", fake_download)
    monkeypatch.setattr(gdrive, "_download_file_direct", fake_direct)
    media = gdrive.download_media("https://drive.google.com/file/d/1SomeFileId/view")
    assert media["type"] == "image"
    assert len(media["paths"]) == 1
