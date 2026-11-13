"""Tool semantics: units, pagination, explicit bounds, errors and search behaviour."""

import json
import re

import pytest

from ctxlab import artifacts as A
from ctxlab.artifacts import ArtifactStore
from ctxlab.paths import FIXTURE_DIR


@pytest.fixture(scope="module")
def store():
    return ArtifactStore(FIXTURE_DIR)


def _lines(text):
    return [int(m.group(1)) for m in re.finditer(r"^(\d+)\| ", text, re.MULTILINE)]


def test_list_includes_every_artifact(store):
    out = store.list_artifacts()
    for aid in store.artifacts:
        assert f"- {aid} |" in out.text


def test_read_defaults_and_one_based_offset(store):
    out = store.read_artifact("ticket")
    assert _lines(out.text)[0] == 1
    assert out.text.split("\n")[1] == "1| # INC-4821: Checkout requests slow"
    assert out.meta["bounded_by"] == "end_of_artifact" and "has_more=false" in out.text


def test_read_limit_is_capped_and_reported(store):
    out = store.read_artifact("checkout-metrics", 1, 500)
    assert len(_lines(out.text)) == A.MAX_LIMIT
    assert f"applied {A.MAX_LIMIT}" in out.text
    assert out.meta["bounded_by"] == "limit" and f"next_offset={A.MAX_LIMIT + 1}" in out.text


def test_char_budget_bounds_read_and_paging_covers_every_line_once(store):
    total = len(store.artifacts["traces-2026-10-06"].lines)
    seen, offset, budget_hits = [], 1, 0
    while True:
        out = store.read_artifact("traces-2026-10-06", offset, 200)
        got = _lines(out.text)
        body_chars = sum(len(l.split("| ", 1)[1]) for l in out.text.split("\n")[1:])
        assert body_chars <= A.READ_CHAR_BUDGET
        seen += got
        if out.meta["bounded_by"] == "char_budget":
            budget_hits += 1
        if "has_more=false" in out.text:
            break
        offset = int(re.search(r"next_offset=(\d+)", out.text).group(1))
    assert seen == list(range(1, total + 1))
    assert budget_hits > 0


def test_reads_are_repeatable_for_recovery(store):
    assert store.read_artifact("config-diff", 20, 15).text == store.read_artifact("config-diff", 20, 15).text


def test_offset_past_end_and_invalid_arguments(store):
    out = store.read_artifact("ticket", 10_000)
    assert out.ok and "returned 0 lines" in out.text
    for bad in (0, -1, "3", True, 1.5):
        assert not store.read_artifact("ticket", bad).ok
    assert not store.read_artifact("ticket", 1, 0).ok
    assert not store.read_artifact("nope").ok


def test_long_line_is_clipped_explicitly(tmp_path):
    fx = tmp_path / "fx"
    (fx / "artifacts").mkdir(parents=True)
    text = "short\n" + "x" * (A.MAX_LINE_CHARS + 50) + "\n"
    (fx / "artifacts" / "a.txt").write_text(text, encoding="utf-8", newline="\n")
    import hashlib
    (fx / "manifest.json").write_text(json.dumps({"fixture": "t", "artifacts": [{
        "id": "a", "file": "artifacts/a.txt", "title": "t", "kind": "text", "description": "d",
        "sha256": hashlib.sha256(text.encode()).hexdigest()}]}), encoding="utf-8")
    out = ArtifactStore(fx).read_artifact("a")
    assert "[line clipped:" in out.text and out.meta["clipped_lines"] == [2] and "clipped_lines=[2]" in out.text


def test_manifest_path_outside_artifacts_is_rejected(tmp_path):
    fx = tmp_path / "fx"
    (fx / "artifacts").mkdir(parents=True)
    (fx / "secret.txt").write_text("x\n", encoding="utf-8")
    (fx / "manifest.json").write_text(json.dumps({"fixture": "t", "artifacts": [{
        "id": "a", "file": "secret.txt", "title": "t", "kind": "text", "description": "d", "sha256": "0"}]}),
        encoding="utf-8")
    with pytest.raises(A.FixtureError):
        ArtifactStore(fx)


def test_search_and_semantics_filter_and_cap(store):
    out = store.search_artifacts("timeout_ms=8000 in:payment-client-log")
    assert out.ok and out.meta["total_hits"] > A.SEARCH_MAX_HITS
    assert out.meta["shown"] == A.SEARCH_MAX_HITS and "refine the query" in out.text
    assert all(aid == "payment-client-log" for aid, _ in out.meta["hits"])
    both = store.search_artifacts("cfgctl rollback")
    assert both.meta["total_hits"] == 1 and both.meta["hits"][0][0] == "runbook-payments"
    phrase = store.search_artifacts('"Post-deploy checks passed"')
    assert phrase.meta["total_hits"] == 1
    # A trace id finds the whole sampled trace in one search.
    first = json.loads(store.artifacts["traces-2026-10-06"].lines[0])["trace"]
    spans = store.search_artifacts(f"{first} in:traces-2026-10-06")
    assert spans.meta["total_hits"] >= 5


def test_search_errors(store):
    assert not store.search_artifacts("").ok
    assert not store.search_artifacts("in:ticket").ok
    assert not store.search_artifacts("x in:nope").ok
    assert not store.search_artifacts('"unclosed').ok


def test_dispatch_never_raises_on_bad_calls(store):
    assert not store.call("read_artifact", "{not json").ok
    assert not store.call("read_artifact", "[1,2]").ok
    assert not store.call("read_artifact", json.dumps({"id": "ticket", "path": "x"})).ok
    assert not store.call("delete_artifact", "{}").ok
    assert store.call("list_artifacts", "").ok
    assert store.call("read_artifact", json.dumps({"id": "ticket", "offset": 3, "limit": 2})).meta["last_line"] == 4


def test_tool_schemas_match_dispatch():
    names = [t["function"]["name"] for t in A.TOOL_SCHEMAS]
    assert names == ["list_artifacts", "search_artifacts", "read_artifact"]
    read = A.TOOL_SCHEMAS[2]["function"]["parameters"]
    assert set(read["properties"]) == {"id", "offset", "limit"} and read["properties"]["limit"]["maximum"] == A.MAX_LIMIT
