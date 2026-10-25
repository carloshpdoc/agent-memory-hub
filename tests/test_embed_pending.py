"""Unit tests for session chunking in embed_pending — pure logic, network mocked."""
import io
import json
import urllib.error

import pytest

import embed_pending as ep


# ---- chunk_spans ---------------------------------------------------------------

def _texts(text, spans):
    return [text[a:b] for a, b in spans]


def test_chunk_spans_short_text_is_one_chunk():
    assert ep.chunk_spans("[user]\nhello") == [(0, 12)]


def test_chunk_spans_empty_and_blank():
    assert ep.chunk_spans("") == []
    assert ep.chunk_spans("   \n\n  ") == []


def test_chunk_spans_cover_text_contiguously_within_size():
    text = ("[user]\n" + "word " * 120 + "\n\n" + "line\n" * 40 + "\n[assistant]\n" + "x" * 900) * 5
    spans = ep.chunk_spans(text, size=500)
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert all(b - a <= 500 for a, b in spans)
    assert all(spans[i][1] == spans[i + 1][0] for i in range(len(spans) - 1))


def test_chunk_spans_prefers_turn_boundary():
    text = "[user]\n" + "a" * 300 + "\n\n" + "b" * 200 + "\n[assistant]\n" + "c" * 400
    first, second = _texts(text, ep.chunk_spans(text, size=600))[:2]
    assert second.startswith("[assistant]")
    assert first.endswith("b\n")


def test_chunk_spans_falls_back_to_word_then_hard_cut():
    words = ep.chunk_spans("abc " * 200, size=100)
    assert all(t.endswith(" ") for t in _texts("abc " * 200, words)[:-1])
    hard = ep.chunk_spans("x" * 250, size=100)
    assert hard == [(0, 100), (100, 200), (200, 250)]


# ---- sample_spans --------------------------------------------------------------

def test_sample_spans_under_cap_keeps_all_with_indices():
    spans = [(0, 1), (1, 2), (2, 3)]
    assert ep.sample_spans(spans, cap=5) == [(0, (0, 1)), (1, (1, 2)), (2, (2, 3))]


def test_sample_spans_over_cap_keeps_first_last_and_cap():
    spans = [(i, i + 1) for i in range(100)]
    picked = ep.sample_spans(spans, cap=10)
    idxs = [i for i, _ in picked]
    assert len(picked) == 10
    assert idxs[0] == 0 and idxs[-1] == 99
    assert idxs == sorted(set(idxs))
    assert all(spans[i] == span for i, span in picked)


def test_sample_spans_rejects_tiny_cap():
    with pytest.raises(ValueError):
        ep.sample_spans([(0, 1)], cap=1)


# ---- embed_texts ---------------------------------------------------------------

def _http_error(code):
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(b""))


def _store():
    from store.supabase import SupabaseStore
    return SupabaseStore(url="u", key="k", embed_key="e")


def test_embed_texts_batches_in_order(monkeypatch):
    st, calls = _store(), []

    def fake_send(url, body, headers, method="POST", timeout=60):
        calls.append(body["texts"])
        return json.dumps({"embeddings": [[float(len(t))] for t in body["texts"]]}).encode()

    monkeypatch.setattr(st, "_send", fake_send)
    out = st.embed_texts(["a", "bb", "ccc"], batch=2)
    assert calls == [["a", "bb"], ["ccc"]]
    assert out == [[1.0], [2.0], [3.0]]


def test_embed_texts_halves_batch_on_compute_limit(monkeypatch):
    st, sizes = _store(), []

    def fake_send(url, body, headers, method="POST", timeout=60):
        sizes.append(len(body["texts"]))
        if len(body["texts"]) > 2:
            raise _http_error(546)
        return json.dumps({"embeddings": [[0.0]] * len(body["texts"])}).encode()

    monkeypatch.setattr(st, "_send", fake_send)
    out = st.embed_texts(["t"] * 5, batch=8)
    assert len(out) == 5
    assert sizes[:2] == [5, 4] and all(s <= 2 for s in sizes[2:])


def test_embed_texts_gives_up_after_max_fails(monkeypatch):
    import store.supabase as sb
    st = _store()
    monkeypatch.setattr(sb.time, "sleep", lambda s: None)
    monkeypatch.setattr(st, "_send", lambda *a, **k: (_ for _ in ()).throw(_http_error(503)))
    with pytest.raises(urllib.error.HTTPError):
        st.embed_texts(["t"], max_fails=2)


def test_embed_texts_rejects_count_mismatch(monkeypatch):
    st = _store()
    monkeypatch.setattr(st, "_send", lambda *a, **k: json.dumps({"embeddings": []}).encode())
    with pytest.raises(RuntimeError):
        st.embed_texts(["t"])


# ---- chunk_session (através do store Supabase) ---------------------------------

def _recording_store(monkeypatch, log, fail_insert=False):
    st = _store()

    def fake_send(url, body, headers, method="POST", timeout=60):
        path = url.split("/rest/v1/")[-1].split("/functions/v1/")[-1]
        if fail_insert and method == "POST" and path == "session_chunks":
            raise _http_error(500)
        log.append((method, path, body))
        if isinstance(body, dict) and "texts" in body:
            return json.dumps({"embeddings": [[0.5]] * len(body["texts"])}).encode()
        return b""
    monkeypatch.setattr(st, "_send", fake_send)
    return st


def test_chunk_session_replaces_chunks_then_marks_length(monkeypatch):
    log = []
    st = _recording_store(monkeypatch, log)
    content = "[user]\nhi\n[assistant]\nhello"
    row = {"id": "s1", "content": content, "content_len": len(content)}
    assert ep.chunk_session(st, row) == 1
    methods = [(m, path) for m, path, _ in log]
    assert methods == [("POST", "embed"), ("DELETE", "session_chunks?session_fk=eq.s1"),
                       ("POST", "session_chunks"), ("PATCH", "sessions?id=eq.s1")]
    inserted = log[2][2]
    assert inserted == [{"session_fk": "s1", "chunk_idx": 0, "char_start": 0,
                         "char_end": len(content), "embedding": "[0.5]"}]
    assert log[3][2] == {"chunked_len": len(content)}


def test_chunk_session_empty_content_only_clears_and_marks(monkeypatch):
    log = []
    st = _recording_store(monkeypatch, log)
    assert ep.chunk_session(st, {"id": "s2", "content": "", "content_len": 0}) == 0
    assert [m for m, _, _ in log] == ["DELETE", "PATCH"]


def test_chunk_session_does_not_mark_when_insert_fails(monkeypatch):
    log = []
    st = _recording_store(monkeypatch, log, fail_insert=True)
    with pytest.raises(urllib.error.HTTPError):
        ep.chunk_session(st, {"id": "s3", "content": "[user]\nx", "content_len": 8})
    assert "PATCH" not in [m for m, _, _ in log]
