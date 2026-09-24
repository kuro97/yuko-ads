"""
Тесты общего атомарного JSON state-стора (services/state_store.py).
"""

import json

from services.state_store import load_json_state, save_json_state


def test_save_then_load_roundtrip(tmp_path):
    """save({"a": 1}) -> load() возвращает то же самое (happy path)."""
    path = tmp_path / "state.json"

    save_json_state(path, {"a": 1})
    result = load_json_state(path)

    assert result == {"a": 1}


def test_load_missing_file_returns_empty_dict(tmp_path):
    """Несуществующий файл -> {} без исключений."""
    path = tmp_path / "nonexistent.json"

    result = load_json_state(path)

    assert result == {}


def test_load_broken_json_returns_empty_dict(tmp_path):
    """Файл с мусором вместо JSON -> {} без падения."""
    path = tmp_path / "broken.json"
    path.write_text("{not valid json!!!", encoding="utf-8")

    result = load_json_state(path)

    assert result == {}


def test_load_non_dict_json_returns_empty_dict(tmp_path):
    """Валидный JSON, но не объект (напр. список) -> {} (контракт: dict)."""
    path = tmp_path / "list.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    result = load_json_state(path)

    assert result == {}


def test_save_creates_parent_directories(tmp_path):
    """save создаёт недостающие директории по пути."""
    path = tmp_path / "nested" / "dir" / "state.json"

    save_json_state(path, {"ok": True})

    assert path.exists()
    assert load_json_state(path) == {"ok": True}


def test_save_is_atomic_no_tmp_file_left_behind(tmp_path):
    """После успешного save временный .tmp-файл не остаётся (переименован)."""
    path = tmp_path / "state.json"

    save_json_state(path, {"x": 1})

    tmp_file = path.with_suffix(path.suffix + ".tmp")
    assert not tmp_file.exists()
    assert path.exists()


def test_save_overwrites_existing_state(tmp_path):
    """Повторный save полностью заменяет содержимое файла."""
    path = tmp_path / "state.json"

    save_json_state(path, {"a": 1, "b": 2})
    save_json_state(path, {"c": 3})

    assert load_json_state(path) == {"c": 3}


def test_load_survives_directory_not_existing(tmp_path):
    """load на путь, где даже родительской директории нет -> {} (не падает)."""
    path = tmp_path / "missing_dir" / "state.json"

    result = load_json_state(path)

    assert result == {}
