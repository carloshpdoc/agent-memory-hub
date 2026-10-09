"""Unit tests for the recall eval harness — the pure scoring logic (no network)."""
import eval_recall as ev


# ---- query_from_summary --------------------------------------------------------

def test_query_from_summary_strips_counter():
    assert ev.query_from_summary("set up backups on the EC2 host  (2q/5r)") == "set up backups on the EC2 host"


def test_query_from_summary_keeps_theme_drops_arc():
    s = "first question about caching [...] final unrelated thing  (3q/7r)"
    assert ev.query_from_summary(s) == "first question about caching"


def test_query_from_summary_empty():
    assert ev.query_from_summary("") == ""
    assert ev.query_from_summary(None) == ""


def test_query_from_summary_truncates():
    assert len(ev.query_from_summary("x " * 200)) <= 120


# ---- rank_of -------------------------------------------------------------------

def test_rank_of_found():
    results = [{"session_id": "a"}, {"session_id": "b"}, {"session_id": "c"}]
    assert ev.rank_of("a", results) == 1
    assert ev.rank_of("c", results) == 3


def test_rank_of_missing():
    assert ev.rank_of("z", [{"session_id": "a"}]) is None
    assert ev.rank_of("z", []) is None


# ---- metrics -------------------------------------------------------------------

def test_metrics_all_hit_at_one():
    m = ev.metrics([1, 1, 1], ks=(1, 3, 5))
    assert m["hit@1"] == 1.0
    assert m["hit@3"] == 1.0
    assert m["mrr"] == 1.0


def test_metrics_mixed_ranks():
    # ranks: 1, 2, miss, 5
    m = ev.metrics([1, 2, None, 5], ks=(1, 3, 5))
    assert m["hit@1"] == 0.25          # only the rank-1
    assert m["hit@3"] == 0.5           # ranks 1 and 2
    assert m["hit@5"] == 0.75          # ranks 1, 2, 5
    # MRR = (1/1 + 1/2 + 0 + 1/5) / 4
    assert abs(m["mrr"] - (1 + 0.5 + 0 + 0.2) / 4) < 1e-9


def test_metrics_all_miss():
    m = ev.metrics([None, None], ks=(1, 5))
    assert m["hit@1"] == 0.0
    assert m["hit@5"] == 0.0
    assert m["mrr"] == 0.0


def test_metrics_empty_no_div_by_zero():
    m = ev.metrics([], ks=(1,))
    assert m["hit@1"] == 0.0
    assert m["mrr"] == 0.0


# ---- sample_sessions: modo spread (numeros publicados) --------------------------

def test_sample_sessions_spread_reads_whole_corpus_and_hashes(monkeypatch):
    import eval_recall as ev
    captured = {}
    rows = [{"session_id": f"s{i}", "summary": f"question number {i} about x"} for i in range(30)]

    def fake_rest_all(path):
        captured["all"] = path
        return rows

    def fake_rest(path):
        captured["path"] = path
        return []

    monkeypatch.setattr(ev, "rest_all", fake_rest_all)
    monkeypatch.setattr(ev, "rest", fake_rest)
    picked = ev.sample_sessions(10, spread=True)
    assert "order=" not in captured["all"]                 # whole corpus, ordered locally
    assert picked == ev.spread_pick(rows, 10)              # deterministico e reprodutivel
    ev.sample_sessions(10, spread=False)
    assert "order=started_at.desc" in captured["path"]  # default: regressao (recentes)


# ---- spread_pick ---------------------------------------------------------------

def test_spread_pick_is_deterministic_and_bounded():
    rows = [{"session_id": f"s{i}"} for i in range(50)]
    a, b = ev.spread_pick(rows, 10), ev.spread_pick(list(reversed(rows)), 10)
    assert a == b and len(a) == 10


def test_spread_pick_does_not_follow_id_order():
    # time-ordered IDs (UUIDv7-like) must not all land first just because they sort low
    v7 = [{"session_id": f"019f{i:04d}-0000"} for i in range(30)]
    v4 = [{"session_id": f"{h}{i:03d}-rand"} for i, h in enumerate("89abcdef" * 4)]
    picked = ev.spread_pick(v7 + v4, 20)
    n_v7 = sum(r["session_id"].startswith("019f") for r in picked)
    assert 0 < n_v7 < 20


# ---- ambiguous_queries -----------------------------------------------------------

def test_ambiguous_queries_flags_only_repeated_themes():
    s = ["monitor my PRs (1q/2r)", "monitor my PRs [...] other end (3q/4r)",
         "unique question here (1q/1r)", None, ""]
    assert ev.ambiguous_queries(s) == {"monitor my PRs"}


def test_sample_sessions_skips_ambiguous(monkeypatch):
    rows = [{"session_id": "a", "summary": "repeated (1q/1r)"},
            {"session_id": "b", "summary": "repeated (1q/1r)"},
            {"session_id": "c", "summary": "only one (1q/1r)"}]
    monkeypatch.setattr(ev, "rest_all", lambda path: rows)
    picked = ev.sample_sessions(10, spread=True, skip={"repeated"})
    assert [r["session_id"] for r in picked] == ["c"]
