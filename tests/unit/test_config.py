from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from src.utils.config import code_version, load_config, resolve_config_hash


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_load_config_roundtrip(tmp_path: Path) -> None:
    path = _write(tmp_path, {"app": {"env": "test"}, "storage": {"mariadb": {"host": "h"}}})
    cfg = load_config(path)
    assert cfg["app"]["env"] == "test"
    assert cfg["storage"]["mariadb"]["host"] == "h"


def test_load_empty_yaml_returns_empty_dict(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    assert load_config(path) == {}


def test_load_non_mapping_rejected(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_config(path)


def test_config_hash_deterministic_across_key_order() -> None:
    a = {"x": 1, "y": 2, "z": {"a": 1, "b": 2}}
    b = {"z": {"b": 2, "a": 1}, "y": 2, "x": 1}
    assert resolve_config_hash(a) == resolve_config_hash(b)


def test_config_hash_differs_on_value_change() -> None:
    a = {"x": 1}
    b = copy.deepcopy(a)
    b["x"] = 2
    assert resolve_config_hash(a) != resolve_config_hash(b)


def test_config_hash_is_hex() -> None:
    h = resolve_config_hash({"x": 1})
    assert len(h) == 64
    int(h, 16)


def test_code_version_returns_string() -> None:
    # Running in the repo, this should be a 12-char git SHA or "unknown".
    v = code_version()
    assert isinstance(v, str) and v != ""
