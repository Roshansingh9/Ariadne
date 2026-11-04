import io
import tarfile

import httpx
import pytest

from ariadne.ingest.fetch import (FetchError, FetchResult, Limits, _CappedStream, fetch_github,
                                  fetch_git, read_tar_stream)
from ariadne.ingest.urls import parse_repo_url

SHA = "0123456789abcdef0123456789abcdef01234567"


def make_tar(members: list[tuple], pax_commit: str | None = SHA) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": pax_commit} if pax_commit else None) as tf:
        for name, kind, payload in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = payload
                tf.addfile(info)
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = payload
                tf.addfile(info)
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
    return buf.getvalue()


HOSTILE = [
    ("repo-sha/", "dir", None),
    ("repo-sha/src/app.py", "file", b"def run():\n    return 1\n"),
    ("repo-sha/web/ui.ts", "file", b"export function ui() { return 1; }\n"),
    ("repo-sha/../../etc/evil.py", "file", b"def evil(): pass\n"),
    ("repo-sha//abs.py", "file", b"def a(): pass\n"),
    ("repo-sha/link.py", "symlink", "/etc/passwd"),
    ("repo-sha/hard.py", "hardlink", "repo-sha/src/app.py"),
    ("repo-sha/.env.py", "file", b"X=1\n"),
    ("repo-sha/node_modules/lib/index.js", "file", b"module.exports=1\n"),
    ("repo-sha/README.md", "file", b"# readme\n"),
    ("repo-sha/src/huge.py", "file", b"x = 1\n" * 30000),
]


def test_tar_reader_only_accepts_safe_regular_files():
    data = make_tar(HOSTILE)
    res = read_tar_stream(io.BytesIO(data), Limits(max_file_bytes=64 * 1024), FetchResult())
    assert [p for p, _ in res.files] == ["src/app.py", "web/ui.ts"]
    assert res.commit == SHA
    assert res.skipped["unsafe_path"] == 2          # ../../etc/evil.py and the absolute /abs.py
    assert res.skipped["too_large"] == 1
    assert "secret_name" in res.skipped and "ignored_dir" in res.skipped


def test_capped_stream_enforces_archive_limit():
    stream = _CappedStream(iter([b"x" * 600, b"y" * 600]), cap=1000)
    with pytest.raises(FetchError, match="exceeds"):
        stream.read()


def test_corrupt_archive_raises_fetch_error():
    with pytest.raises(FetchError):
        read_tar_stream(io.BytesIO(b"not a tarball at all" * 10), Limits(), FetchResult())


def _github_transport(tar_bytes: bytes, *, repo_status=200, private=False, rate_limited=False):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        url = str(request.url)
        if rate_limited:
            return httpx.Response(403, json={"message": "API rate limit exceeded"})
        if url == "https://api.github.com/repos/acme/shop":
            return httpx.Response(repo_status, json={"default_branch": "main", "private": private})
        if url == "https://api.github.com/repos/acme/shop/commits/main":
            assert request.headers["accept"] == "application/vnd.github.sha"
            return httpx.Response(200, text=SHA)
        if url == f"https://codeload.github.com/acme/shop/tar.gz/{SHA}":
            return httpx.Response(200, content=tar_bytes)
        return httpx.Response(404)
    return httpx.MockTransport(handler), seen


def test_fetch_github_streams_archive_by_commit():
    transport, seen = _github_transport(make_tar(HOSTILE[:3]))
    with httpx.Client(transport=transport) as client:
        res = fetch_github(parse_repo_url("https://github.com/acme/shop"), Limits(), client=client)
    assert res.ref == "main" and res.commit == SHA and res.source == "github_archive"
    assert [p for p, _ in res.files] == ["src/app.py", "web/ui.ts"]
    assert seen[-1].startswith("https://codeload.github.com/acme/shop/tar.gz/" + SHA)


@pytest.mark.parametrize("kwargs,msg", [
    ({"repo_status": 404}, "not found"),
    ({"private": True}, "Private"),
    ({"rate_limited": True}, "rate limit"),
])
def test_fetch_github_errors(kwargs, msg):
    transport, _ = _github_transport(b"", **kwargs)
    with httpx.Client(transport=transport) as client, pytest.raises(FetchError, match=msg):
        fetch_github(parse_repo_url("https://github.com/acme/shop"), Limits(), client=client)


def test_fetch_git_without_git_binary_fails_clearly(monkeypatch):
    monkeypatch.setattr("ariadne.ingest.fetch.shutil.which", lambda _: None)
    with pytest.raises(FetchError, match="requires git"):
        fetch_git(parse_repo_url("https://gitlab.com/group/proj"), Limits())


def test_fetch_git_builds_safe_command(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        if "clone" in cmd:
            dest = cmd[-1]
            import os
            os.makedirs(dest)
            with open(os.path.join(dest, "m.py"), "w") as fh:
                fh.write("def f():\n    pass\n")

        class R:
            stdout = SHA + "\n"
        return R()

    monkeypatch.setattr("ariadne.ingest.fetch.shutil.which", lambda _: "git")
    monkeypatch.setattr("ariadne.ingest.fetch.subprocess.run", fake_run)
    monkeypatch.setattr("ariadne.ingest.fetch.tempfile.mkdtemp", lambda prefix: str(tmp_path / "w"))
    (tmp_path / "w").mkdir()
    res = fetch_git(parse_repo_url("https://gitlab.com/group/proj", ref="dev"), Limits())
    clone_cmd, kw = calls[0]
    assert clone_cmd[-3] == "--" and clone_cmd[-2] == "https://gitlab.com/group/proj.git"
    assert "--depth" in clone_cmd and ["--branch", "dev"] == clone_cmd[clone_cmd.index("--branch"):clone_cmd.index("--branch") + 2]
    assert "shell" not in kw and kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert [p for p, _ in res.files] == ["m.py"] and res.commit == SHA
    assert not (tmp_path / "w").exists()       # temp workspace removed


def test_archive_download_respects_deadline():
    import time
    lim = Limits(deadline=time.monotonic() - 1)
    stream = _CappedStream(iter([b"x" * 10]), cap=1000, limits=lim)
    with pytest.raises(FetchError, match="time budget"):
        stream.read()
