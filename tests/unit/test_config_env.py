from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.utils.config import ConfigError, load_config, load_env_file


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "settings.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


class TestLoadEnvFile:
    def test_reads_key_value_pairs(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("TEST_VAR_ONE", raising=False)
        monkeypatch.delenv("TEST_VAR_TWO", raising=False)
        path = _write_env(tmp_path, "TEST_VAR_ONE=hello\nTEST_VAR_TWO=world\n")
        read = load_env_file(path)
        assert read == {"TEST_VAR_ONE": "hello", "TEST_VAR_TWO": "world"}
        assert os.environ.get("TEST_VAR_ONE") == "hello"

    def test_does_not_override_existing_env(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("TEST_VAR_EXISTING", "from_process")
        path = _write_env(tmp_path, "TEST_VAR_EXISTING=from_file\n")
        load_env_file(path)
        assert os.environ["TEST_VAR_EXISTING"] == "from_process"

    def test_strips_comments_and_blank_lines(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("TEST_CLEAN", raising=False)
        path = _write_env(
            tmp_path,
            "# a comment\n\nTEST_CLEAN=ok\n# trailing comment\n",
        )
        read = load_env_file(path)
        assert read == {"TEST_CLEAN": "ok"}

    def test_strips_quotes(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("DQ", raising=False)
        monkeypatch.delenv("SQ", raising=False)
        path = _write_env(tmp_path, 'DQ="double quoted"\nSQ=\'single quoted\'\n')
        read = load_env_file(path)
        assert read["DQ"] == "double quoted"
        assert read["SQ"] == "single quoted"

    def test_handles_export_prefix(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("EXPORTED", raising=False)
        path = _write_env(tmp_path, "export EXPORTED=yes\n")
        read = load_env_file(path)
        assert read == {"EXPORTED": "yes"}

    def test_ignores_malformed_lines(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("ONLY_VALID", raising=False)
        path = _write_env(
            tmp_path,
            "not a valid line\nroot@host:/path# cat .env\nONLY_VALID=1\n",
        )
        read = load_env_file(path)
        assert read == {"ONLY_VALID": "1"}

    def test_ignores_keys_with_bad_names(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("GOOD", raising=False)
        path = _write_env(tmp_path, "3_BAD=val\nbad_lower=val\nGOOD=1\n")
        read = load_env_file(path)
        assert read == {"GOOD": "1"}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_env_file(tmp_path / "does-not-exist") == {}


class TestLoadConfigSubstitution:
    def test_substitutes_set_variable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("MY_SECRET", "from_env")
        path = _write_yaml(tmp_path, "mariadb:\n  password: ${MY_SECRET}\n")
        cfg = load_config(path, load_env=False)
        assert cfg["mariadb"]["password"] == "from_env"

    def test_default_when_variable_unset(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("MY_MISSING", raising=False)
        path = _write_yaml(tmp_path, "mariadb:\n  port: ${MY_MISSING:-3306}\n")
        cfg = load_config(path, load_env=False)
        assert cfg["mariadb"]["port"] == "3306"

    def test_raises_when_required_variable_missing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("MY_REQUIRED", raising=False)
        path = _write_yaml(tmp_path, "mariadb:\n  password: ${MY_REQUIRED}\n")
        with pytest.raises(ConfigError, match="MY_REQUIRED"):
            load_config(path, load_env=False)

    def test_substitutes_within_quoted_strings(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("UA_CONTACT", "dev@example.com")
        path = _write_yaml(
            tmp_path,
            'ingestion:\n  user_agent: "trackmania/0.1 (contact: ${UA_CONTACT})"\n',
        )
        cfg = load_config(path, load_env=False)
        assert cfg["ingestion"]["user_agent"] == "trackmania/0.1 (contact: dev@example.com)"

    def test_substitutes_recursively(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("HOST_A", "a.example")
        monkeypatch.setenv("HOST_B", "b.example")
        monkeypatch.delenv("HOST_C", raising=False)
        path = _write_yaml(
            tmp_path,
            "hosts:\n"
            "  - ${HOST_A}\n"
            "  - ${HOST_B}\n"
            "  - ${HOST_C:-c.default}\n",
        )
        cfg = load_config(path, load_env=False)
        assert cfg["hosts"] == ["a.example", "b.example", "c.default"]

    def test_non_string_values_passthrough(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        path = _write_yaml(
            tmp_path, "numbers:\n  ttl: 30\n  rate: 1.5\n  enabled: true\n"
        )
        cfg = load_config(path, load_env=False)
        assert cfg["numbers"] == {"ttl": 30, "rate": 1.5, "enabled": True}

    def test_empty_default_is_empty_string(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # The scaffold uses ${MARIADB_UNIX_SOCKET:-} for "leave unset".
        monkeypatch.delenv("EMPTY_DEFAULT", raising=False)
        path = _write_yaml(tmp_path, "x: ${EMPTY_DEFAULT:-}\n")
        cfg = load_config(path, load_env=False)
        assert cfg["x"] == ""

    def test_loads_env_file_before_substitution(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("FROM_ENVFILE", raising=False)
        env_path = _write_env(tmp_path, "FROM_ENVFILE=loaded\n")
        yaml_path = _write_yaml(tmp_path, "x: ${FROM_ENVFILE}\n")
        cfg = load_config(yaml_path, env_file=env_path)
        assert cfg["x"] == "loaded"

    def test_raises_on_non_mapping_yaml(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        path = _write_yaml(tmp_path, "- 1\n- 2\n")
        with pytest.raises(ConfigError, match="mapping"):
            load_config(path, load_env=False)
