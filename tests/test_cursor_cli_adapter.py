"""
Cursor CLI (`agent`) adapter: reads ~/.cursor/chats/<md5(cwd)>/<chat>/ (meta.json + store.db)
into the shared session shape. The store is built here in the CLI's format (content-addressed
blobs, a protobuf root listing the message ids), so no Cursor install and no network.
"""
import hashlib
import io
import json
import sqlite3

import capture_session
import continuar
import cursor_capture
import cursor_cli
import install_hooks
from capture_session import INTERNAL_PROMPT_MARKER

CWD = "/Users/dev/app-ios"


def _varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _ld(field, payload):
    return _varint((field << 3) | 2) + _varint(len(payload)) + payload


def make_chat(root, cwd, chat_id, messages, updated_ms=1_793_903_221_799, with_meta_json=True):
    """Chat no formato do CLI; devolve a pasta. `messages` são dicts no formato do AI SDK."""
    d = root / hashlib.md5(cwd.encode()).hexdigest() / chat_id
    d.mkdir(parents=True)
    con = sqlite3.connect(d / "store.db")
    con.execute("create table blobs (id text primary key, data blob)")
    con.execute("create table meta (key text primary key, value text)")
    root_blob = b""
    for m in messages:
        data = json.dumps(m).encode()
        bid = hashlib.sha256(data).hexdigest()
        con.execute("insert into blobs values (?, ?)", (bid, data))
        root_blob += _ld(1, bytes.fromhex(bid))
    root_blob += _ld(9, f"file://{cwd}".encode()) + _varint((10 << 3) | 0) + _varint(1)
    root_id = hashlib.sha256(root_blob).hexdigest()
    con.execute("insert into blobs values (?, ?)", (root_id, root_blob))
    # blob binário que não é mensagem: tem que ser ignorado
    con.execute("insert into blobs values (?, ?)", ("ff" * 32, b"\x0a\x20" + b"\x00" * 32))
    meta = {"agentId": chat_id, "latestRootBlobId": root_id, "createdAt": updated_ms - 30_000}
    con.execute("insert into meta values ('0', ?)", (json.dumps(meta).encode().hex(),))
    con.commit()
    con.close()
    if with_meta_json:
        (d / "meta.json").write_text(json.dumps(
            {"cwd": cwd, "title": "Fix login", "createdAtMs": updated_ms - 30_000, "updatedAtMs": updated_ms}))
    return d


def convo(query="corrija o login que falha com senha vazia", answer="Corrigi o Login.swift."):
    return [
        {"role": "system", "content": "You are an interactive CLI tool..."},
        {"role": "user", "content": f"<user_info>\nOS Version: darwin\nWorkspace Path: {CWD}\n</user_info>"},
        {"role": "user", "content": [{"type": "text", "text":
            f"<timestamp>Thursday</timestamp>\n<user_query>\n{query}\n</user_query>"}]},
        {"role": "assistant", "content": [{"type": "reasoning", "text": "pensando alto"},
                                          {"type": "tool-call", "toolName": "ReadFile"}]},
        {"role": "tool", "content": [{"type": "tool-result", "toolName": "ReadFile"}]},
        {"role": "assistant", "content": [{"type": "text", "text": answer}]},
    ]


class FakeStore:
    configured = True

    def __init__(self, seen=(), lose=()):
        self.rows, self.seen, self.lose = [], set(seen), set(lose)

    def upsert_session(self, row, timeout=15):
        self.rows.append(row)
        if row["session_id"] not in self.lose:   # `lose`: aceita sem erro e não grava
            self.seen.add(row["session_id"])

    def session_ids(self):
        return self.seen


def test_parse_keeps_the_query_and_the_answer_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_CHATS", str(tmp_path))
    d = make_chat(tmp_path, CWD, "chat-1", convo())
    sid, cwd, content, uts, nu, na, started, ended = cursor_cli.parse(str(d))
    assert (sid, cwd, nu, na) == ("chat-1", CWD, 1, 1)
    assert content == "[user]\ncorrija o login que falha com senha vazia\n\n[assistant]\nCorrigi o Login.swift."
    assert uts == ["corrija o login que falha com senha vazia"]
    assert "user_info" not in content and "pensando alto" not in content and "interactive CLI" not in content
    assert started < ended and ended.startswith("2026-11-05")


def test_cwd_falls_back_to_the_workspace_uri_without_meta_json(tmp_path):
    d = make_chat(tmp_path, CWD, "chat-2", convo(), with_meta_json=False)
    assert cursor_cli.parse(str(d))[1] == CWD


def test_parse_redacts_secrets_like_the_other_captures(tmp_path):
    d = make_chat(tmp_path, CWD, "chat-3", convo(query="deploy com API_KEY=abcdefghijklmnop1234"))
    content = cursor_cli.parse(str(d))[2]
    assert "abcdefghijklmnop1234" not in content and "[REDACTED:assignment]" in content


def test_unreadable_store_is_skipped(tmp_path):
    d = tmp_path / "x" / "broken"
    d.mkdir(parents=True)
    (d / "store.db").write_bytes(b"not sqlite")
    assert cursor_cli.parse(str(d)) is None


def test_workspace_dir_and_latest_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_CHATS", str(tmp_path))
    make_chat(tmp_path, CWD, "old", convo(), updated_ms=1_000)
    new = make_chat(tmp_path, CWD, "new", convo(), updated_ms=2_000)
    make_chat(tmp_path, "/outro/repo", "other", convo(), updated_ms=9_000)
    assert cursor_cli.workspace_dir(CWD).endswith(hashlib.md5(CWD.encode()).hexdigest())
    assert cursor_cli.latest_chat(CWD) == str(new)
    assert cursor_cli.find_chat("other").endswith("/other")
    assert cursor_cli.find_chat("../etc") is None and cursor_cli.find_chat(None) is None


def test_build_row_is_cursor_with_cli_source(tmp_path):
    d = make_chat(tmp_path, CWD, "chat-4", convo())
    row = cursor_cli.build_row(cursor_cli.parse(str(d)), str(d), {}, {"hook_reason": "stop"})
    assert row["tool"] == "cursor" and row["session_id"] == "chat-4"
    assert row["metadata"]["source"] == "cursor-cli" and row["metadata"]["cwd"] == CWD
    assert row["metadata"]["title"] == "Fix login" and row["metadata"]["hook_reason"] == "stop"


def test_capture_chat_skips_the_hubs_own_sessions(tmp_path):
    d = make_chat(tmp_path, CWD, "internal", convo(query=f"{INTERNAL_PROMPT_MARKER}\nextract facts"))
    store = FakeStore()
    assert cursor_cli.capture_chat(store, str(d), {}) is None and not store.rows


def test_backfill_skips_saved_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_CHATS", str(tmp_path))
    make_chat(tmp_path, CWD, "saved", convo())
    make_chat(tmp_path, CWD, "fresh", convo())
    store = FakeStore(seen={"saved"})
    monkeypatch.setattr(cursor_cli, "get_store", lambda: store)
    assert cursor_cli.main([]) == 0
    assert [r["session_id"] for r in store.rows] == ["fresh"]


def test_backfill_counts_every_chat_by_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CURSOR_CHATS", str(tmp_path))
    make_chat(tmp_path, CWD, "saved", convo())
    make_chat(tmp_path, CWD, "fresh", convo())
    make_chat(tmp_path, CWD, "internal", convo(query=f"{INTERNAL_PROMPT_MARKER}\nextract facts"))
    make_chat(tmp_path, CWD, "empty", [])
    broken = tmp_path / "x" / "broken"
    broken.mkdir(parents=True)
    (broken / "store.db").write_bytes(b"isto nao e sqlite")
    monkeypatch.setattr(cursor_cli, "get_store", lambda: FakeStore(seen={"saved"}))
    assert cursor_cli.main([]) == 0
    out = capsys.readouterr().out
    assert ("5 chats do Cursor CLI; 1 ilegível; 1 vazio; 1 já no banco; 1 interno do hub; 0 pasta temporária; 0 pasta ignorada (CAPTURE_SKIP_PATHS); 1 a enviar") in out
    assert "enviadas 1 sessões do Cursor CLI; 0 falharam" in out


def test_backfill_fails_when_the_row_is_not_in_the_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CURSOR_CHATS", str(tmp_path))
    make_chat(tmp_path, CWD, "lost", convo())
    monkeypatch.setattr(cursor_cli, "get_store", lambda: FakeStore(lose={"lost"}))
    assert cursor_cli.main([]) == 1
    assert "FALHOU lost" in capsys.readouterr().err


def test_refresh_cwd_rewrites_the_latest_chat_with_git_now(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_CHATS", str(tmp_path))
    make_chat(tmp_path, CWD, "latest", convo())
    import handoff
    monkeypatch.setattr(handoff, "git_snapshot", lambda cwd: {"branch": "fix/login", "head": "1a2b"})
    store = FakeStore(seen={"latest"})   # já salvo: o refresh regrava mesmo assim
    row = cursor_cli.refresh_cwd(store, CWD, env={})
    assert row["session_id"] == "latest" and row["metadata"]["git"]["branch"] == "fix/login"
    assert cursor_cli.refresh_cwd(store, "/sem/chats", env={}) is None


def _quiet(monkeypatch):
    """Os hooks escrevem no hooks/capture.log real: nos testes, o log vai para uma lista."""
    logged = []
    monkeypatch.setattr(cursor_capture, "log", logged.append)
    monkeypatch.setattr(capture_session, "log", logged.append)
    return logged


def test_hook_captures_the_conversation_by_id(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_CHATS", str(tmp_path))
    monkeypatch.setenv("AMH_CURSOR_FOREGROUND", "1")
    _quiet(monkeypatch)
    make_chat(tmp_path, CWD, "conv-9", convo())
    store = FakeStore()
    monkeypatch.setattr(cursor_capture, "get_store", lambda: store)
    monkeypatch.setattr(cursor_capture, "git_snapshot", lambda cwd: {"branch": "main", "head": "abc"})
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"conversation_id": "conv-9", "hook_event_name": "stop", "workspace_roots": [CWD]})))
    assert cursor_capture.main() == 0
    assert store.rows[0]["session_id"] == "conv-9"
    assert store.rows[0]["metadata"]["git"] == {"branch": "main", "head": "abc"}


def test_hook_ignores_ide_conversations(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_CHATS", str(tmp_path))
    monkeypatch.setenv("AMH_CURSOR_FOREGROUND", "1")
    _quiet(monkeypatch)
    store = FakeStore()
    monkeypatch.setattr(cursor_capture, "get_store", lambda: store)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"conversation_id": "ide-composer"})))
    assert cursor_capture.main() == 0 and not store.rows


def test_install_adds_stop_and_keeps_the_guard():
    guard = {"command": "python3 /x/agent-memory-hub/hooks/guard.py --tool cursor"}
    cfg = {"version": 1, "hooks": {"beforeShellExecution": [guard]}}
    cfg, changes = install_hooks.merge_cursor(cfg, install_hooks.CURSOR_HOOKS)
    assert changes == ["stop (novo)"]
    assert cfg["hooks"]["beforeShellExecution"] == [guard]
    assert install_hooks.CURSOR_CAPTURE in cfg["hooks"]["stop"][0]["command"]
    _cfg, again = install_hooks.merge_cursor(cfg, install_hooks.CURSOR_HOOKS)
    assert again == []


def test_install_fixes_a_stale_capture_command():
    cfg = {"hooks": {"stop": [{"command": "python3 /old/agent-memory-hub/hooks/cursor_capture.py"}]}}
    cfg, changes = install_hooks.merge_cursor(cfg, install_hooks.CURSOR_HOOKS)
    assert changes == ["stop (corrigido)"] and len(cfg["hooks"]["stop"]) == 1
    assert cfg["version"] == 1


def test_continuar_opens_cursor_with_agent_or_the_old_name(monkeypatch):
    monkeypatch.setattr(continuar.shutil, "which", lambda c: "/bin/" + c)
    assert continuar.resolve_cmd(["agent"]) == ["agent"]
    monkeypatch.setattr(continuar.shutil, "which", lambda c: "/bin/x" if c == "cursor-agent" else None)
    assert continuar.resolve_cmd(["agent"]) == ["cursor-agent"]
    assert continuar.resolve_cmd(["claude"]) == ["claude"]


def test_continuar_refreshes_cursor_cli_before_looking(monkeypatch):
    calls = []
    monkeypatch.setattr(cursor_cli, "refresh_cwd", lambda store, d: calls.append(d))
    monkeypatch.setattr(continuar, "_repo_top", lambda cwd: "/repo")
    continuar.refresh_cursor_cli(object(), "/repo/sub", "claude")
    assert calls == ["/repo/sub", "/repo"]
    calls.clear()
    continuar.refresh_cursor_cli(object(), "/repo", "cursor")   # destino é o próprio Cursor
    assert calls == []


def _transcript(tmp_path, chat_id, query="explique o AdViewViewModel", answer="Ele formata o preço."):
    d = tmp_path / ".cursor" / "projects" / "Users-dev-app-ios" / "agent-transcripts" / chat_id
    d.mkdir(parents=True)
    f = d / f"{chat_id}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in [
        {"role": "user", "message": {"content": [{"type": "text", "text":
            f"<timestamp>Thursday</timestamp>\n<user_query>\n{query}\n</user_query>"}]}},
        {"role": "assistant", "message": {"content": [{"type": "text", "text": answer}]}},
        {"type": "turn_ended", "status": "success"},
    ]))
    return str(f)


def test_hook_payload_with_session_id_uses_the_chat_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_CHATS", str(tmp_path / "chats"))
    make_chat(tmp_path / "chats", CWD, "sess-1", convo())
    store = FakeStore()
    row = cursor_cli.capture_from_hook(store, {"session_id": "sess-1", "hook_event_name": "Stop"}, {}, {"branch": "main"})
    assert row["session_id"] == "sess-1" and row["metadata"]["source"] == "cursor-cli"
    assert row["metadata"]["git"] == {"branch": "main"} and row["metadata"]["hook_reason"] == "Stop"


def test_hook_falls_back_to_the_jsonl_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_CHATS", str(tmp_path / "vazio"))
    path = _transcript(tmp_path, "sess-2")
    assert cursor_cli.is_cli_transcript(path)
    store = FakeStore()
    row = cursor_cli.capture_from_hook(store, {"session_id": "sess-2", "transcript_path": path, "cwd": CWD}, {})
    assert row["content"] == "[user]\nexplique o AdViewViewModel\n\n[assistant]\nEle formata o preço."
    assert row["metadata"]["cwd"] == CWD and row["metadata"]["transcript"] == path
    assert row["tool"] == "cursor"


def test_capture_session_hands_cursor_cli_turns_to_the_adapter(tmp_path, monkeypatch):
    """O Cursor CLI roda os hooks do Claude Code: o Stop do Claude recebe o turno do agent."""
    monkeypatch.setenv("CURSOR_CHATS", str(tmp_path / "vazio"))
    logged = _quiet(monkeypatch)
    path = _transcript(tmp_path, "sess-3")
    store = FakeStore()
    monkeypatch.setattr(capture_session, "get_store", lambda: store)
    import handoff
    monkeypatch.setattr(handoff, "git_snapshot", lambda cwd: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "sess-3", "transcript_path": path, "cwd": CWD, "hook_event_name": "Stop"})))
    assert capture_session.main() == 0
    assert store.rows[0]["session_id"] == "sess-3" and store.rows[0]["tool"] == "cursor"
    assert any("OK cursor-cli sessao sess-3" in m for m in logged)


def test_claude_transcripts_are_not_cursor_cli():
    assert not capture_session.is_cursor_cli("/Users/x/.claude/projects/-Users-x/abc.jsonl")
    assert not cursor_cli.is_cli_transcript("/Users/x/.claude/projects/-Users-x/abc.jsonl")


def test_skips_temporary_and_configured_folders(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CURSOR_CHATS", str(tmp_path / "chats"))
    monkeypatch.setenv("CAPTURE_SKIP_PATHS", "/Users/dev/bench/work:")
    make_chat(tmp_path / "chats", "/private/tmp/scratchpad/smoke", "tmp-chat", convo())
    make_chat(tmp_path / "chats", "/Users/dev/bench/work", "bench-chat", convo())
    make_chat(tmp_path / "chats", "/Users/dev/bench/workshop", "not-under", convo())
    make_chat(tmp_path / "chats", CWD, "real", convo())
    store = FakeStore()
    monkeypatch.setattr(cursor_cli, "get_store", lambda: store)
    assert cursor_cli.main([]) == 0
    out = capsys.readouterr().out
    assert "4 chats do Cursor CLI;" in out and "1 pasta temporária" in out
    assert "1 pasta ignorada (CAPTURE_SKIP_PATHS)" in out and "2 a enviar" in out
    assert sorted(r["session_id"] for r in store.rows) == ["not-under", "real"]
    # o hook passa pelo mesmo filtro
    d = cursor_cli.find_chat("bench-chat")
    assert cursor_cli.capture_chat(FakeStore(), d, {}) is None
