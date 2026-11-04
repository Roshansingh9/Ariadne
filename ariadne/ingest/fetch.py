"""Obtain repository source files safely.

GitHub: the tarball is streamed over HTTPS and read **in memory**. Nothing
is written to disk, and no `git` binary is needed, so this path works in
Vercel's serverless runtime.

Other allow-listed hosts: a shallow `git clone` into a temporary directory
that is always removed. If `git` is not installed, the caller gets a clear
error.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Optional

import httpx

from .filters import content_decision, normalize_path, path_decision
from .urls import RepoRef

USER_AGENT = "Ariadne-CodeGraph/0.1"


class FetchError(RuntimeError):
    """User-facing ingestion failure (message is safe to display)."""


@dataclass
class FetchResult:
    files: list[tuple[str, bytes]] = field(default_factory=list)
    ref: Optional[str] = None
    commit: Optional[str] = None
    skipped: dict[str, int] = field(default_factory=dict)
    skipped_samples: list[dict] = field(default_factory=list)
    truncated: bool = False
    source: str = ""

    def skip(self, path: str, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1
        if reason != "unsupported" and len(self.skipped_samples) < 40:
            self.skipped_samples.append({"path": path, "reason": reason})


@dataclass
class Limits:
    max_files: int = 1500
    max_file_bytes: int = 400 * 1024
    max_archive_bytes: int = 60 * 1024 * 1024
    max_source_bytes: int = 25 * 1024 * 1024
    timeout: float = 60.0
    deadline: Optional[float] = None     # time.monotonic() value; whole fetch must finish by then

    def remaining(self) -> float:
        if self.deadline is None:
            return self.timeout
        return self.deadline - time.monotonic()

    def check_deadline(self) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise FetchError("Downloading the repository exceeded the indexing time budget. "
                             "Try a smaller repository.")


def collect(entries: Iterable[tuple[str, int, Callable[[], bytes]]], limits: Limits,
            result: FetchResult) -> FetchResult:
    """Apply filters to (path, size, reader) entries; read only accepted files."""
    total = 0
    for raw_path, size, reader in entries:
        path = normalize_path(raw_path)
        if path is None:
            result.skip(raw_path, "unsafe_path")
            continue
        reason = path_decision(path)
        if reason:
            result.skip(path, reason)
            continue
        if size > limits.max_file_bytes:
            result.skip(path, "too_large")
            continue
        if len(result.files) >= limits.max_files or total + size > limits.max_source_bytes:
            result.truncated = True
            result.skip(path, "limit_reached")
            continue
        data = reader()
        reason = content_decision(data, limits.max_file_bytes)
        if reason:
            result.skip(path, reason)
            continue
        total += len(data)
        result.files.append((path, data))
    result.files.sort(key=lambda f: f[0])
    return result


# --------------------------------------------------------------------------
# Tar archives (GitHub)
# --------------------------------------------------------------------------

class _CappedStream(io.RawIOBase):
    """File-like wrapper over an iterator of byte chunks with a hard size cap."""

    def __init__(self, chunks: Iterator[bytes], cap: int, limits: Optional["Limits"] = None):
        self._limits = limits
        self._chunks = chunks
        self._buf = b""
        self._cap = cap
        self.read_total = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while not self._buf:
            try:
                chunk = next(self._chunks)
            except StopIteration:
                return 0
            self.read_total += len(chunk)
            if self._limits is not None:
                self._limits.check_deadline()
            if self.read_total > self._cap:
                raise FetchError(
                    f"Repository archive exceeds the {self._cap // (1024 * 1024)} MB limit.")
            self._buf = chunk
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


def read_tar_stream(fileobj, limits: Limits, result: FetchResult) -> FetchResult:
    """Read a gzipped tar in streaming mode. Only regular files are considered."""
    try:
        tf = tarfile.open(fileobj=fileobj, mode="r|gz")
    except tarfile.TarError as exc:
        raise FetchError(f"Could not read repository archive: {exc}") from exc

    def entries():
        for member in tf:
            if not member.isfile():          # skips dirs, symlinks, hardlinks, devices
                continue
            # GitHub archives wrap everything in one top-level directory.
            name = member.name.split("/", 1)[1] if "/" in member.name else member.name
            if not name:
                continue

            def reader(m=member):
                fh = tf.extractfile(m)
                return fh.read(limits.max_file_bytes + 1) if fh else b""
            yield name, member.size, reader

    try:
        collect(entries(), limits, result)
        comment = tf.pax_headers.get("comment") if tf.pax_headers else None
        if comment and not result.commit and len(comment) == 40:
            result.commit = comment
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise FetchError(f"Repository archive is corrupt or incomplete: {exc}") from exc
    finally:
        tf.close()
    return result


def _github_headers(token: Optional[str]) -> dict:
    h = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _github_error(resp: httpx.Response, repo: RepoRef) -> FetchError:
    if resp.status_code == 404:
        return FetchError(f"Repository {repo.slug} (or ref) was not found, or it is private.")
    if resp.status_code in (403, 429):
        return FetchError("GitHub API rate limit reached. Try again later or set GITHUB_TOKEN.")
    return FetchError(f"GitHub responded with HTTP {resp.status_code}.")


def fetch_github(repo: RepoRef, limits: Limits, token: Optional[str] = None,
                 client: Optional[httpx.Client] = None) -> FetchResult:
    own = client is None
    client = client or httpx.Client(timeout=limits.timeout, follow_redirects=False)
    try:
        api = f"https://api.github.com/repos/{repo.owner}/{repo.name}"
        meta = client.get(api, headers=_github_headers(token))
        if meta.status_code != 200:
            raise _github_error(meta, repo)
        info = meta.json()
        if info.get("private"):
            raise FetchError("Private repositories are not supported.")
        ref = repo.ref or info.get("default_branch") or "HEAD"

        commit = None
        c = client.get(f"{api}/commits/{ref}",
                       headers={**_github_headers(token), "Accept": "application/vnd.github.sha"})
        if c.status_code == 200 and len(c.text.strip()) == 40:
            commit = c.text.strip()
        elif c.status_code in (404, 422):
            raise FetchError(f"Ref '{ref}' was not found in {repo.slug}.")

        target = commit or ref
        url = f"https://codeload.github.com/{repo.owner}/{repo.name}/tar.gz/{target}"
        result = FetchResult(ref=ref, commit=commit, source="github_archive")
        with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as resp:
            if resp.status_code != 200:
                raise _github_error(resp, repo)
            stream = _CappedStream(resp.iter_bytes(64 * 1024), limits.max_archive_bytes, limits)
            read_tar_stream(io.BufferedReader(stream, 256 * 1024), limits, result)
        return result
    except httpx.TimeoutException as exc:
        raise FetchError("Timed out downloading the repository.") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"Network error while downloading the repository: {type(exc).__name__}") from exc
    finally:
        if own:
            client.close()


# --------------------------------------------------------------------------
# Local directories and git clone fallback
# --------------------------------------------------------------------------

def walk_directory(root: str, limits: Limits, result: Optional[FetchResult] = None) -> FetchResult:
    """Collect files from a local directory without following symlinks."""
    from .filters import IGNORED_DIRS

    result = result or FetchResult(source="directory")
    root = os.path.realpath(root)

    def entries():
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames
                                 if d.lower() not in IGNORED_DIRS
                                 and not os.path.islink(os.path.join(dirpath, d)))
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                rel = os.path.relpath(full, root).replace(os.sep, "/")

                def reader(p=full):
                    with open(p, "rb") as fh:
                        return fh.read(limits.max_file_bytes + 1)
                yield rel, os.path.getsize(full), reader

    return collect(entries(), limits, result)


def fetch_git(repo: RepoRef, limits: Limits) -> FetchResult:
    git = shutil.which("git")
    if not git:
        raise FetchError(
            f"Indexing {repo.host} repositories requires git, which is not available in this "
            "runtime. GitHub repositories are supported without git.")
    tmp = tempfile.mkdtemp(prefix="ariadne-")
    try:
        dest = os.path.join(tmp, "repo")
        cmd = [git, "-c", "core.symlinks=false", "-c", "protocol.file.allow=never",
               "-c", "protocol.ext.allow=never", "clone", "--depth", "1", "--single-branch",
               "--no-tags"]
        if repo.ref:
            cmd += ["--branch", repo.ref]
        cmd += ["--", repo.clone_url, dest]
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo",
               "GIT_LFS_SKIP_SMUDGE": "1"}
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=max(5.0, min(limits.timeout, limits.remaining())), env=env)
        except subprocess.TimeoutExpired as exc:
            raise FetchError("Timed out cloning the repository.") from exc
        except subprocess.CalledProcessError as exc:
            raise FetchError("git clone failed: the repository may not exist, be private, "
                             "or the ref may be wrong.") from exc
        commit = None
        try:
            out = subprocess.run([git, "-C", dest, "rev-parse", "HEAD"], capture_output=True,
                                 text=True, timeout=15, check=True)
            commit = out.stdout.strip() or None
        except (subprocess.SubprocessError, OSError):
            pass
        result = FetchResult(ref=repo.ref or "HEAD", commit=commit, source="git_clone")
        return walk_directory(dest, limits, result)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def fetch_repository(repo: RepoRef, limits: Limits, github_token: Optional[str] = None) -> FetchResult:
    if repo.host == "github.com":
        return fetch_github(repo, limits, token=github_token)
    return fetch_git(repo, limits)
