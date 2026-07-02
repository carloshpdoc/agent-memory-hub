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
