"""Unit tests for eval_answers (outcome eval) — parsing, scoring, sign test. No network."""
import pytest

import ask
import eval_answers as ea


def test_json_object_prefers_the_object_over_a_list_inside_it():
    txt = 'Aqui:\n{"question": "q", "answer": "a", "key_points": ["x", "y"]}\n'
    assert ask.json_object(txt)["key_points"] == ["x", "y"]
    assert ask.json_object("[1, 2]") is None and ask.json_object("nada") is None


def test_parse_case_skips_and_requires_question_and_answer():
    assert ea.parse_case('{"skip": true}') is None
    assert ea.parse_case('{"question": "q"}') is None
    assert ea.parse_case('{"question": " q ", "answer": "a", "key_points": ["k", ""]}') == \
        {"question": "q", "answer": "a", "key_points": ["k"]}


def test_parse_verdict_only_known_values():
    assert ea.parse_verdict('{"verdict": "partial"}') == "partial"
    assert ea.parse_verdict('{"verdict": "great"}') is None
    assert ea.parse_verdict("sem json") is None


def test_denied_by_project_scope_or_machine_term():
    assert ask.denied({"project": "acme-ios"}, ["acme"])
    assert ask.denied({"project": None, "machine": "ACME-MBP123"}, ["acme"])
    assert not ask.denied({"project": None, "machine": "home-mac"}, ["acme"])
    assert ask.denied({"scope": "acme-ios", "machine": None}, ["acme"])
    assert not ask.denied({"project": "x"}, [])


def test_sign_test():
    assert ea.sign_test(0, 0) == 1.0
    assert ea.sign_test(5, 0) == pytest.approx(2 / 32)
    assert ea.sign_test(3, 3) == 1.0


def test_summarize_scores_pairs_and_skips_errors():
    results = [
        {"session_id": "a", "verdict_memory": "correct", "verdict_baseline": "abstained",
         "source_retrieved": True, "source_cited": True},
        {"session_id": "b", "verdict_memory": "partial", "verdict_baseline": "correct",
         "source_retrieved": False, "source_cited": False},
        {"session_id": "c", "error": "Timeout"},
    ]
    s = ea.summarize(results)
    assert (s["graded"], s["errors"]) == (2, 1)
    assert s["memory"]["score"] == 0.75 and s["baseline"]["score"] == 0.5
    assert s["baseline"]["abstained"] == 0.5
    assert s["paired"] == {"wins": 1, "losses": 1, "ties": 0, "p": 1.0}
    assert s["source_retrieved"] == 0.5 and s["source_cited"] == 0.5


def test_run_case_records_error_instead_of_raising(monkeypatch):
    monkeypatch.setattr(ea, "answer_with_memory", lambda q, c, g: (_ for _ in ()).throw(TimeoutError("x")))
    r = ea.run_case({"session_id": "abcd1234-z", "question": "q"}, None, None)
    assert r["error"].startswith("TimeoutError")


def test_run_case_marks_source_retrieved_and_cited(monkeypatch):
    monkeypatch.setattr(ea, "answer_with_memory", lambda q, c, g: ("feito [abcd1234]", ["abcd1234-z"]))
    monkeypatch.setattr(ea, "answer_baseline", lambda q, c, g: "não sei")
    monkeypatch.setattr(ea, "judge", lambda case, cand, c, g: "correct" if "feito" in cand else "abstained")
    r = ea.run_case({"session_id": "abcd1234-z", "question": "q"}, None, None)
    assert r["source_retrieved"] and r["source_cited"]
    assert (r["verdict_memory"], r["verdict_baseline"]) == ("correct", "abstained")
