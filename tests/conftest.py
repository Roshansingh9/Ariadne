from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "sample_repo"
FULLSTACK_DIR = ROOT / "tests" / "fixtures" / "fullstack_repo"

# Fixture repositories are data for the indexer (they contain files named test_*.py on purpose).
collect_ignore = ["fixtures"]


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    return FIXTURE_DIR


@pytest.fixture(scope="session")
def fixture_fetch():
    from ariadne.ingest.fetch import Limits, walk_directory
    return walk_directory(str(FIXTURE_DIR), Limits())


@pytest.fixture()
def fixture_graph(fixture_fetch):
    from ariadne.indexer import build_graph
    graph, errors = build_graph(fixture_fetch)
    assert errors == []
    return graph


@pytest.fixture()
def memory_queries(fixture_graph):
    from tests.memory_queries import MemoryQueries
    return MemoryQueries(fixture_graph)


def edge_set(graph, etype=None):
    return {(e.type, e.src, e.dst, e.props.get("confidence")) for e in graph.edges
            if etype is None or e.type == etype}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Unit tests must never pick up real credentials from the developer's shell.
    if not os.getenv("ARIADNE_LIVE_FALKORDB") and not os.getenv("ARIADNE_LIVE_META"):
        for name in ("FALKORDB_HOST", "FALKORDB_PASSWORD", "FALKORDB_USERNAME", "META_API_KEY",
                     "MODEL_API_KEY", "ARIADNE_INDEX_TOKEN", "GITHUB_TOKEN"):
            monkeypatch.delenv(name, raising=False)
