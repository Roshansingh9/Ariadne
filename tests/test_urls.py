import pytest

from ariadne.ingest.urls import InvalidRepoURL, parse_repo_url


@pytest.mark.parametrize("raw,owner,name,ref", [
    ("https://github.com/FalkorDB/code-graph", "FalkorDB", "code-graph", None),
    ("https://github.com/FalkorDB/code-graph.git", "FalkorDB", "code-graph", None),
    ("github.com/pallets/flask", "pallets", "flask", None),
    ("https://www.github.com/pallets/flask/", "pallets", "flask", None),
    ("https://github.com/pallets/flask/tree/3.0.x", "pallets", "flask", "3.0.x"),
    ("https://gitlab.com/group/proj/-/tree/feature/x", "group", "proj", "feature/x"),
])
def test_valid_urls(raw, owner, name, ref):
    r = parse_repo_url(raw)
    assert (r.owner, r.name, r.ref) == (owner, name, ref)
    assert r.clone_url == f"https://{r.host}/{owner}/{name}.git"


def test_repo_id_is_stable_and_safe():
    r = parse_repo_url("https://github.com/FalkorDB/Code-Graph")
    assert r.repo_id == "github__falkordb__code-graph"


def test_explicit_ref_overrides_url_ref():
    assert parse_repo_url("https://github.com/a/b/tree/main", ref="dev").ref == "dev"


@pytest.mark.parametrize("raw", [
    "http://github.com/a/b",                       # not https
    "https://user:pass@github.com/a/b",            # credentials
    "https://token@github.com/a/b",
    "https://github.com:8443/a/b",                 # port
    "https://github.com/a/b?x=1",                  # query
    "https://github.com/a/b#frag",
    "https://evil.example.com/a/b",                # host not allowed
    "https://github.com.evil.com/a/b",
    "https://169.254.169.254/latest/meta-data",    # SSRF attempt
    "file:///etc/passwd",
    "ssh://git@github.com/a/b",
    "git@github.com:a/b.git",
    "https://github.com/a",                        # no repo
    "https://github.com/-a/b",                     # option-like owner
    "https://github.com/a/--upload-pack=x",        # option injection
    "https://github.com/a/b c",                    # whitespace
    "https://github.com/a/b;rm -rf",
    "https://github.com/../b",
    "https://github.com/a/b/blob/main/x.py",       # unsupported path
    "",
])
def test_rejected_urls(raw):
    with pytest.raises(InvalidRepoURL):
        parse_repo_url(raw)


@pytest.mark.parametrize("ref", ["--upload-pack=evil", "-x", "a..b", "a b", "a;b", "/abs", "x.lock", "a@{1}"])
def test_rejected_refs(ref):
    with pytest.raises(InvalidRepoURL):
        parse_repo_url("https://github.com/a/b", ref=ref)
