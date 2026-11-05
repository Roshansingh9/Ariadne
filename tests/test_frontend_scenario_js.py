"""Scenario rendering (public/app.js renderScenario) on real responses from the local fixture."""

import json
import re
import shutil
import subprocess

import pytest

from ariadne.indexer import build_graph
from ariadne.ingest.fetch import Limits, walk_directory
from ariadne.scenario import analyze
from tests.conftest import FULLSTACK_DIR, ROOT
from tests.memory_queries import MemoryQueries
from tests.test_frontend_impact_js import HARNESS as IMPACT_HARNESS
from tests.test_scenario import FakeMeta

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
HARNESS = IMPACT_HARNESS.replace("globalThis.__render = (r) => r.report ? renderImpact(r) : renderLegacyImpact(r);",
                                 "globalThis.__render = renderScenario;")
PUBLIC = ROOT / "public"


@pytest.fixture(scope="module")
def html(tmp_path_factory):
    g = build_graph(walk_directory(str(FULLSTACK_DIR), Limits()))[0]
    q = lambda: MemoryQueries(g, "local__fullstack")
    responses = {
        "analyzed": analyze(q(), None, "If I change to_cents, what could be affected?"),
        "confirm": analyze(q(), None, "If I change reconcile, what should I inspect?"),
        "model": analyze(q(), FakeMeta('{"targets": [{"ref": "C1", "why": "the retry helper"}]}'),
                         "If I change the retry behavior?"),
        "none": analyze(q(), None, "What happens if authentication middleware changes?"),
        "notes": analyze(q(), None, "If I rename a field in the Order model, which routes depend on it?"),
    }
    responses["analyzed"]["scenario"] = "If I change to_cents <script>alert(1)</script>"      # escaping probe
    d = tmp_path_factory.mktemp("scenario")
    (d / "h.js").write_text(HARNESS, encoding="utf-8")
    (d / "r.json").write_text(json.dumps(responses), encoding="utf-8")
    run = subprocess.run(["node", str(d / "h.js"), str(PUBLIC / "app.js"), str(d / "r.json")],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def titles(page):
    return [t.strip() for t in re.findall(r"<h3>([^<(]+)", page)]


def test_full_scenario_report_sections(html):
    assert titles(html["analyzed"]) == ["Scenario", "Matched targets", "Summary", "Dependency paths", "Architecture &amp; layers",
                                        "User flows / UI impact", "Affected files", "Suggested inspection", "Explanation &amp; evidence"]
    assert "read-only analysis of the indexed graph" in html["analyzed"]


def test_matched_targets_are_checkable_clickable_and_explained(html):
    a = html["analyzed"]
    row = re.search(r'<li class="target-row">.*?</li>', a, re.S).group(0)
    assert 'data-target="backend/utils/money.py::to_cents" checked' in row
    assert 'data-uid="backend/utils/money.py::to_cents" tabindex="0" role="button"' in row
    assert '<span class="conf-badge high">high confidence</span>' in row and "exact match · exact name match for" in row
    assert "backend/utils/money.py:1-2" in row
    assert 'id="impact-reanalyze"' in a


def test_confirmation_state_renders_no_report_and_prechecks_suggestions(html):
    c = html["confirm"]
    assert titles(c) == ["Scenario", "Matched targets"]
    assert "No exact, unambiguous match. Choose the target(s)" in c
    assert 'data-target="backend/services/orders.py::reconcile" checked' in c
    assert 'data-target="backend/services/orders.py::retry_reconcile" checked' not in c


def test_model_suggestions_are_labelled_and_unconfirmed(html):
    m = html["model"]
    assert "model-ranked" in m and "ranked as relevant by the Meta model" in m
    assert "Meta model muse-spark-1.3: ranked 1 of the candidates shown to it" in m
    assert titles(m) == ["Scenario", "Matched targets"]                    # no report until confirmed


def test_no_match_and_limitations(html):
    n = html["none"]
    assert "No matching indexed symbol found" in n and "Analyze selected targets" not in n
    assert "Not found in the graph: <code>authentication</code>, <code>middleware</code>" in n
    assert "Data shapes (fields, attributes" in html["notes"] and "A rename can also break" in html["notes"]


def test_scenario_text_is_escaped(html):
    assert "<script>alert(1)</script>" not in html["analyzed"] and "&lt;script&gt;alert(1)&lt;/script&gt;" in html["analyzed"]
