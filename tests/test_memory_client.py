"""Unit tests for memory_client.collapse_repeats — pure, no network."""
import memory_client as mc


def _row(sid, summary):
    return {"session_id": sid, "summary": summary, "content": "x"}


def test_collapse_repeats_keeps_best_of_each_topic():
    rows = [_row("a", "You are monitoring my PRs (1q/1r)"), _row("b", "fix the build (2q/3r)"),
            _row("c", "You are monitoring my PRs (1q/1r)"), _row("d", "write the docs (1q/1r)")]
    assert [r["session_id"] for r in mc.collapse_repeats(rows, 8)] == ["a", "b", "d"]


def test_collapse_repeats_respects_limit_and_order():
    rows = [_row(str(i), f"topic {i}") for i in range(10)]
    assert [r["session_id"] for r in mc.collapse_repeats(rows, 3)] == ["0", "1", "2"]


def test_collapse_repeats_falls_back_to_content_and_is_case_insensitive():
    rows = [{"session_id": "a", "summary": None, "content": "[user] Same   Prompt"},
            {"session_id": "b", "summary": None, "content": "[user] same prompt"}]
    assert [r["session_id"] for r in mc.collapse_repeats(rows, 5)] == ["a"]


def test_collapse_repeats_keeps_requests_that_differ_late():
    base = "[imagem: content/carousels/memoria-agente-ia-verificavel/slide-{}.png] Generate a slide"
    rows = [_row("a", base.format(3) + " (1q/1r)"), _row("b", base.format(5) + " (1q/1r)")]
    assert [r["session_id"] for r in mc.collapse_repeats(rows, 5)] == ["a", "b"]


def test_collapse_repeats_ignores_counter_and_arc():
    rows = [_row("a", "run the daily check (1q/1r)"),
            _row("b", "run the daily check [...] something else (3q/2r)")]
    assert [r["session_id"] for r in mc.collapse_repeats(rows, 5)] == ["a"]
