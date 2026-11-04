"""Environment-variable parsing (`load_settings`)."""

import logging
import re
from pathlib import Path

import pytest

from ariadne import config
from ariadne.config import OBSOLETE_ENV, Settings, load_settings

ROOT = Path(__file__).resolve().parent.parent

ALL_NAMES = [
    "VERCEL", "FALKORDB_HOST", "FALKORDB_PORT", "FALKORDB_USERNAME", "FALKORDB_PASSWORD",
    "FALKORDB_SSL", "FALKORDB_GRAPH_NAME", "FALKORDB_TIMEOUT", "META_API_KEY", "MODEL_API_KEY",
    "META_API_BASE_URL", "META_BASE_URL", "META_MODEL", "META_API_TIMEOUT", "ARIADNE_MAX_FILES",
    "ARIADNE_MAX_FILE_KB", "ARIADNE_MAX_ARCHIVE_MB", "ARIADNE_MAX_SOURCE_MB", "ARIADNE_FETCH_TIMEOUT",
    "ARIADNE_INDEX_DEADLINE_SECONDS", "ARIADNE_INDEX_TOKEN", "GITHUB_TOKEN", *OBSOLETE_ENV,
]


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """Clean environment; cwd without a .env so the developer's real .env is never read."""
    for name in ALL_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    def set_(**values):
        for k, v in values.items():
            monkeypatch.setenv(k, v)
    return set_


def test_defaults(env):
    s = load_settings()
    assert s == Settings(warnings=s.warnings)
    assert s.warnings == ()
    assert not s.falkordb_configured and not s.meta_configured
    assert s.meta_base_url == "https://api.meta.ai/v1"


def test_documented_ariadne_limits_are_applied(env):
    env(ARIADNE_MAX_FILES="200", ARIADNE_MAX_FILE_KB="100", ARIADNE_MAX_ARCHIVE_MB="10",
        ARIADNE_MAX_SOURCE_MB="5", ARIADNE_FETCH_TIMEOUT="20", ARIADNE_INDEX_DEADLINE_SECONDS="120")
    s = load_settings()
    assert (s.max_files, s.max_file_kb, s.max_archive_mb, s.max_source_mb) == (200, 100, 10, 5)
    assert (s.fetch_timeout, s.index_deadline) == (20.0, 120.0)
    from ariadne.indexer import limits_from
    lim = limits_from(s)
    assert lim.max_files == 200 and lim.max_file_bytes == 100 * 1024
    assert lim.max_archive_bytes == 10 * 1024 * 1024 and lim.max_source_bytes == 5 * 1024 * 1024


@pytest.mark.parametrize("name,bad", [("ARIADNE_MAX_FILES", "lots"), ("ARIADNE_MAX_FILES", "0"),
                                      ("FALKORDB_PORT", "-1"), ("ARIADNE_INDEX_DEADLINE_SECONDS", "x")])
def test_invalid_numbers_fall_back_with_warning(env, name, bad):
    env(**{name: bad})
    s = load_settings()
    assert s == Settings(warnings=s.warnings)
    assert any(name in w for w in s.warnings)


@pytest.mark.parametrize("raw,expected", [("true", True), ("1", True), ("YES", True),
                                          ("false", False), ("0", False), (" off ", False)])
def test_ssl_flag(env, raw, expected):
    env(FALKORDB_SSL=raw)
    assert load_settings().falkordb_ssl is expected


def test_ssl_flag_garbage_warns(env):
    env(FALKORDB_SSL="maybe")
    s = load_settings()
    assert s.falkordb_ssl is False and any("FALKORDB_SSL" in w for w in s.warnings)


# --- aliases ---------------------------------------------------------------

@pytest.mark.parametrize("canonical,alias,attr", [
    ("META_API_KEY", "MODEL_API_KEY", "meta_api_key"),
    ("META_API_BASE_URL", "META_BASE_URL", "meta_base_url"),
])
class TestAliases:
    def test_alias_alone(self, env, canonical, alias, attr):
        env(**{alias: " https://alias.example/v1 "})          # surrounding whitespace is stripped
        s = load_settings()
        assert getattr(s, attr) == "https://alias.example/v1" and s.warnings == ()

    def test_canonical_alone(self, env, canonical, alias, attr):
        env(**{canonical: "https://canon.example/v1"})
        assert getattr(load_settings(), attr) == "https://canon.example/v1"

    def test_canonical_wins_and_conflict_warns_without_values(self, env, canonical, alias, attr):
        env(**{canonical: "https://canon.example/v1", alias: "https://alias.example/v1"})
        s = load_settings()
        assert getattr(s, attr) == "https://canon.example/v1"
        assert any(canonical in w and alias in w for w in s.warnings)
        assert not any("example" in w for w in s.warnings)          # names only, no values

    def test_same_value_in_both_is_silent(self, env, canonical, alias, attr):
        env(**{canonical: "https://same.example/v1", alias: "https://same.example/v1"})
        assert load_settings().warnings == ()

    def test_blank_canonical_does_not_hide_alias(self, env, canonical, alias, attr):
        env(**{canonical: "   ", alias: "https://alias.example/v1"})
        s = load_settings()
        assert getattr(s, attr) == "https://alias.example/v1" and s.warnings == ()


def test_base_url_trailing_slash_and_default(env):
    env(META_BASE_URL="https://api.meta.ai/v1/")
    assert load_settings().meta_base_url == "https://api.meta.ai/v1"
    env(META_BASE_URL="  ")
    assert load_settings().meta_base_url == "https://api.meta.ai/v1"


def test_blank_secrets_are_unset(env):
    env(META_API_KEY=" ", ARIADNE_INDEX_TOKEN="  ", FALKORDB_HOST="", FALKORDB_PASSWORD=" ")
    s = load_settings()
    assert s.meta_api_key is None and s.index_token is None
    assert s.falkordb_host is None and s.falkordb_password is None


def test_token_is_stripped_password_is_verbatim(env):
    env(ARIADNE_INDEX_TOKEN=" tok-123\n", FALKORDB_PASSWORD=" p w ")
    s = load_settings()
    assert s.index_token == "tok-123"
    assert s.falkordb_password == " p w "


# --- obsolete names ----------------------------------------------------------

def test_obsolete_names_are_ignored_and_reported(env):
    env(INGEST_MAX_FILES="5", INDEX_DEADLINE_SECONDS="5", SNIPPET_MAX_LINES="3",
        MAX_TRAVERSAL_DEPTH="2")
    s = load_settings()
    assert s.max_files == 1500 and s.index_deadline == 270.0     # not silently applied
    joined = " ".join(s.warnings)
    for name in ("INGEST_MAX_FILES", "INDEX_DEADLINE_SECONDS", "SNIPPET_MAX_LINES", "MAX_TRAVERSAL_DEPTH"):
        assert name in joined
    assert "ARIADNE_MAX_FILES" in joined


def test_example_file_lists_only_names_the_app_reads():
    names = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", (ROOT / ".env.example").read_text(), re.M))
    assert names, ".env.example should list variables"
    src = (ROOT / "ariadne" / "config.py").read_text()
    for name in names:
        assert f'"{name}"' in src and name not in OBSOLETE_ENV, name


# --- .env loading / Vercel ---------------------------------------------------

def test_dotenv_loaded_locally_but_not_on_vercel(env, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("FALKORDB_HOST=from-dotenv\n", encoding="utf-8")
    s = load_settings()
    assert s.falkordb_host == "from-dotenv" and s.on_vercel is False
    monkeypatch.delenv("FALKORDB_HOST")
    env(VERCEL="1")
    s = load_settings()
    assert s.falkordb_host is None and s.on_vercel is True


def test_dotenv_never_overrides_real_env(env, tmp_path):
    (tmp_path / ".env").write_text("META_MODEL=from-dotenv\n", encoding="utf-8")
    env(META_MODEL="from-env")
    assert load_settings().meta_model == "from-env"


def test_vercel_without_token_disables_writes(env):
    env(VERCEL="1")
    s = load_settings()
    assert s.writes_enabled is False
    assert s.public_status()["writes_enabled"] is False
    assert any("ARIADNE_INDEX_TOKEN" in w for w in s.warnings)
    env(ARIADNE_INDEX_TOKEN="tok")
    assert load_settings().writes_enabled is True


def test_warnings_and_status_never_contain_secret_values(env, caplog):
    secrets = {"META_API_KEY": "LLM_123456789_aaaaaaaaaaaaaaaaaaaa", "MODEL_API_KEY": "LLM_987654321_bbbb",
               "FALKORDB_PASSWORD": "pw-zzzz-secret", "ARIADNE_INDEX_TOKEN": "tok-yyyy-secret",
               "GITHUB_TOKEN": "ghp_xxxx", "FALKORDB_HOST": "host-secret.example"}
    env(**secrets, VERCEL="1")
    with caplog.at_level(logging.WARNING, logger="ariadne.config"):
        s = load_settings()
    blob = " ".join(s.warnings) + caplog.text + repr(s) + str(s.public_status())
    for value in secrets.values():
        assert value not in blob
