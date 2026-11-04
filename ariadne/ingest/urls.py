"""Repository URL validation and normalization.

Only public HTTPS URLs on an allow-listed set of forges are accepted. The
normalized result is assembled from validated components, so nothing from
the raw input (credentials, ports, query strings, option-like refs) can
reach a subprocess or an outbound request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

ALLOWED_HOSTS = {
    "github.com": "github.com",
    "www.github.com": "github.com",
    "gitlab.com": "gitlab.com",
    "bitbucket.org": "bitbucket.org",
    "codeberg.org": "codeberg.org",
}

_SEGMENT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$")
_REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


class InvalidRepoURL(ValueError):
    pass


@dataclass(frozen=True)
class RepoRef:
    host: str
    owner: str
    name: str
    ref: Optional[str] = None

    @property
    def clone_url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.name}.git"

    @property
    def web_url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.name}"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def repo_id(self) -> str:
        """Stable identifier used in graph names and API paths."""
        host = self.host.split(".")[0]
        return f"{host}__{self.owner}__{self.name}".lower()


def _check_ref(ref: str) -> str:
    if (not _REF.match(ref) or ref.startswith(("-", "/")) or ref.endswith(("/", ".lock"))
            or ".." in ref or "//" in ref or "@{" in ref):
        raise InvalidRepoURL("Invalid branch/tag name.")
    return ref


def parse_repo_url(raw: str, ref: Optional[str] = None) -> RepoRef:
    if not isinstance(raw, str):
        raise InvalidRepoURL("Repository URL must be a string.")
    raw = raw.strip()
    if not raw or len(raw) > 300:
        raise InvalidRepoURL("Repository URL is empty or too long.")
    if any(c.isspace() or ord(c) < 32 for c in raw):
        raise InvalidRepoURL("Repository URL contains whitespace or control characters.")

    # Allow "github.com/owner/repo" shorthand.
    if "://" not in raw:
        raw = "https://" + raw

    parts = urlsplit(raw)
    if parts.scheme.lower() != "https":
        raise InvalidRepoURL("Only https:// repository URLs are supported.")
    if parts.username or parts.password or "@" in parts.netloc:
        raise InvalidRepoURL("URLs with embedded credentials are not allowed.")
    if parts.port is not None:
        raise InvalidRepoURL("Custom ports are not allowed.")
    if parts.query or parts.fragment:
        raise InvalidRepoURL("Query strings and fragments are not allowed.")

    host = ALLOWED_HOSTS.get((parts.hostname or "").lower())
    if not host:
        allowed = ", ".join(sorted(set(ALLOWED_HOSTS.values())))
        raise InvalidRepoURL(f"Unsupported host. Allowed: {allowed}.")

    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        raise InvalidRepoURL("Expected a URL like https://github.com/owner/repo.")

    owner, name = segments[0], segments[1]
    if name.endswith(".git"):
        name = name[:-4]
    for seg in (owner, name):
        if not _SEGMENT.match(seg) or seg in {".", ".."}:
            raise InvalidRepoURL("Invalid owner or repository name.")

    url_ref: Optional[str] = None
    rest = segments[2:]
    if rest:
        # Accept GitHub-style /tree/<ref> (and GitLab /-/tree/<ref>).
        if rest[0] == "-":
            rest = rest[1:]
        if len(rest) >= 2 and rest[0] == "tree":
            url_ref = "/".join(rest[1:])
        else:
            raise InvalidRepoURL("Unsupported repository URL path.")

    final_ref = ref.strip() if isinstance(ref, str) and ref.strip() else url_ref
    if final_ref:
        final_ref = _check_ref(final_ref)

    return RepoRef(host=host, owner=owner, name=name, ref=final_ref)
