"""Runtime configuration, read from environment variables.

Secret values are never logged or returned; `public_status()` only reports
whether each setting is present. Configuration warnings name variables,
never their values.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace

log = logging.getLogger("ariadne.config")

DEFAULT_META_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_META_MODEL = "muse-spark-1.3"

# Names from earlier drafts of Ariadne that are NOT read. They are reported at
# startup so a stale .env copied into Vercel does not silently fall back to defaults.
OBSOLETE_ENV = {
    "INGEST_MAX_FILES": "ARIADNE_MAX_FILES",
    "INGEST_MAX_FILE_BYTES": "ARIADNE_MAX_FILE_KB (kilobytes)",
    "INGEST_MAX_ARCHIVE_BYTES": "ARIADNE_MAX_ARCHIVE_MB (megabytes)",
    "INGEST_MAX_TOTAL_BYTES": "ARIADNE_MAX_SOURCE_MB (megabytes)",
    "INGEST_GIT_TIMEOUT_SECONDS": "ARIADNE_FETCH_TIMEOUT",
    "INDEX_DEADLINE_SECONDS": "ARIADNE_INDEX_DEADLINE_SECONDS",
    "INGEST_SUPPORTED_EXTENSIONS": None,
    "INGEST_EXTRA_EXCLUDES": None,
    "MAX_TRAVERSAL_DEPTH": None,
    "MAX_TRAVERSAL_NODES": None,
    "SNIPPET_MAX_CHARS": None,
    "SNIPPET_MAX_LINES": None,
    "ARIADNE_ALLOWED_LOCAL_ROOT": None,
    "CODE_GRAPH_SERVICE_ROOT": None,
}


def _env(*names: str, strip: bool = True, warnings: list | None = None) -> str | None:
    """First non-blank value among `names` (canonical name first, then aliases).

    A blank or whitespace-only value counts as unset, so an empty canonical
    variable never hides a filled-in alias. If several names are set to
    different values, the first wins and a warning names the variables.
    """
    found: list[tuple[str, str]] = []
    for name in names:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            continue
        found.append((name, raw.strip() if strip else raw))
    if not found:
        return None
    if warnings is not None and len({v for _, v in found}) > 1:
        warnings.append(f"{found[0][0]} and {', '.join(n for n, _ in found[1:])} are both set "
                        f"with different values; using {found[0][0]}.")
    return found[0][1]


def _num(name: str, default, cast, warnings: list, minimum=None):
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = cast(raw)
    except ValueError:
        warnings.append(f"{name} is not a valid number; using the default ({default}).")
        return default
    if minimum is not None and value < minimum:
        warnings.append(f"{name} must be at least {minimum}; using the default ({default}).")
        return default
    return value


def _bool(name: str, default: bool, warnings: list) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    low = raw.lower()
    if low in {"1", "true", "yes", "on"}:
        return True
    if low in {"0", "false", "no", "off"}:
        return False
    warnings.append(f"{name} is not a boolean (true/false); using the default ({default}).")
    return default


@dataclass(frozen=True)
class Settings:
    # FalkorDB Cloud
    falkordb_host: str | None = field(default=None, repr=False)
    falkordb_port: int = 6379
    falkordb_username: str | None = field(default=None, repr=False)
    falkordb_password: str | None = field(default=None, repr=False)
    falkordb_ssl: bool = False
    graph_prefix: str = "ariadne"
    falkordb_timeout: float = 30.0

    # Meta Model API
    meta_api_key: str | None = field(default=None, repr=False)
    meta_base_url: str = DEFAULT_META_BASE_URL
    meta_model: str = DEFAULT_META_MODEL
    meta_timeout: float = 45.0

    # Ingestion limits (bounded MVP for serverless)
    max_files: int = 1500
    max_file_kb: int = 400
    max_archive_mb: int = 60
    max_source_mb: int = 25
    fetch_timeout: float = 60.0
    index_deadline: float = 270.0      # keep below vercel.json maxDuration (300)
    github_token: str | None = field(default=None, repr=False)

    # Shared secret protecting write endpoints (index / delete)
    index_token: str | None = field(default=None, repr=False)
    # True when running on Vercel. There, write endpoints fail closed without index_token.
    on_vercel: bool = False

    warnings: tuple[str, ...] = ()

    @property
    def falkordb_configured(self) -> bool:
        return bool(self.falkordb_host)

    @property
    def meta_configured(self) -> bool:
        return bool(self.meta_api_key)

    @property
    def writes_enabled(self) -> bool:
        """Index/delete need a configured token on Vercel; local development may run without one."""
        return bool(self.index_token) or not self.on_vercel

    def public_status(self) -> dict:
        """Presence-only view of configuration — safe to return to clients."""
        return {
            "falkordb_configured": self.falkordb_configured,
            "falkordb_ssl": self.falkordb_ssl,
            "graph_prefix": self.graph_prefix,
            "meta_configured": self.meta_configured,
            "meta_model": self.meta_model,
            "index_token_required": bool(self.index_token),
            "writes_enabled": self.writes_enabled,
            "limits": {
                "max_files": self.max_files,
                "max_file_kb": self.max_file_kb,
                "max_archive_mb": self.max_archive_mb,
            },
        }


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader for local development. Never overrides real env vars."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def load_settings() -> Settings:
    on_vercel = _env("VERCEL") is not None
    if not on_vercel:
        load_dotenv()
    w: list[str] = []
    for old, new in OBSOLETE_ENV.items():
        if _env(old) is not None:
            w.append(f"{old} is not used by Ariadne" + (f"; set {new} instead." if new else "; remove it."))

    s = Settings(
        falkordb_host=_env("FALKORDB_HOST"),
        falkordb_port=_num("FALKORDB_PORT", 6379, int, w, minimum=1),
        falkordb_username=_env("FALKORDB_USERNAME"),
        # Passwords are used verbatim (whitespace may be significant); blank means unset.
        falkordb_password=_env("FALKORDB_PASSWORD", strip=False),
        falkordb_ssl=_bool("FALKORDB_SSL", False, w),
        graph_prefix=_env("FALKORDB_GRAPH_NAME") or "ariadne",
        falkordb_timeout=_num("FALKORDB_TIMEOUT", 30.0, float, w, minimum=1),
        # META_API_KEY is canonical; MODEL_API_KEY is the name Meta's own docs use
        # for the same Meta Model API key (same provider — not a fallback).
        meta_api_key=_env("META_API_KEY", "MODEL_API_KEY", warnings=w),
        meta_base_url=(_env("META_API_BASE_URL", "META_BASE_URL", warnings=w)
                       or DEFAULT_META_BASE_URL).rstrip("/"),
        meta_model=_env("META_MODEL") or DEFAULT_META_MODEL,
        meta_timeout=_num("META_API_TIMEOUT", 45.0, float, w, minimum=1),
        max_files=_num("ARIADNE_MAX_FILES", 1500, int, w, minimum=1),
        max_file_kb=_num("ARIADNE_MAX_FILE_KB", 400, int, w, minimum=1),
        max_archive_mb=_num("ARIADNE_MAX_ARCHIVE_MB", 60, int, w, minimum=1),
        max_source_mb=_num("ARIADNE_MAX_SOURCE_MB", 25, int, w, minimum=1),
        fetch_timeout=_num("ARIADNE_FETCH_TIMEOUT", 60.0, float, w, minimum=1),
        index_deadline=_num("ARIADNE_INDEX_DEADLINE_SECONDS", 270.0, float, w, minimum=10),
        github_token=_env("GITHUB_TOKEN"),
        index_token=_env("ARIADNE_INDEX_TOKEN"),
        on_vercel=on_vercel,
    )
    if on_vercel and s.index_deadline >= 300:
        w.append("ARIADNE_INDEX_DEADLINE_SECONDS should stay below the 300 s Vercel maxDuration.")
    if not s.writes_enabled:
        w.append("ARIADNE_INDEX_TOKEN is not set; indexing and deletion are disabled on Vercel.")
    for msg in w:
        log.warning("config: %s", msg)
    return replace(s, warnings=tuple(w))
