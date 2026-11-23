#!/usr/bin/env python3
"""
agent-memory-hub — prova de que o backend local (sqlite) nao fala com a rede.

A maquina do trabalho nao deve enviar nada. Esta prova nao infere pelo nome do backend:
ela instala um guarda sobre socket.connect/create_connection, permite so destino local
(AF_UNIX, 127.0.0.1, ::1), e entao exercita o store de verdade (escreve um fato e uma
sessao) e gera um snapshot cifrado com `age`, tudo sob o guarda. Qualquer tentativa de
conexao para um destino nao-local e registrada com o endereco e reprovada a prova.

Medida (nao sinal indireto): numero de connect() tentados, quantos locais, quantos remotos,
linhas realmente gravadas no sqlite, e bytes do arquivo cifrado. Sucesso exige:
remotos == 0, e as linhas gravadas e o arquivo cifrado conferem.

Uso:
  python3 scripts/prove_offline.py
Sai 0 se nada saiu pela rede; 1 caso contrario (listando os destinos tentados).
"""
import os
import socket
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- 1) forcar backend local ANTES de importar o store ------------------------------------
TMP = tempfile.mkdtemp(prefix="amh-prove-")
os.environ["AMH_BACKEND"] = "sqlite"
os.environ["AMH_DB"] = os.path.join(TMP, "mem.db")

# ---- 2) guarda de rede: so local passa; o resto e registrado e bloqueado ------------------
_remote = []   # [(host, port)] tentados para fora
_local = []    # [(host, port)] locais permitidos
_LOCAL_HOSTS = {"127.0.0.1", "::1", "0.0.0.0", "localhost", ""}


def _is_local(address):
    if not isinstance(address, tuple):   # AF_UNIX e afins: caminho local
        return True
    host = address[0]
    return host in _LOCAL_HOSTS or host.startswith("127.")


_orig_connect = socket.socket.connect
_orig_create = socket.create_connection


def _guard_connect(self, address, *a, **k):
    if _is_local(address):
        _local.append(address if isinstance(address, tuple) else ("unix", 0))
        return _orig_connect(self, address, *a, **k)
    _remote.append(address)
    raise RuntimeError(f"PROVA: tentativa de conexao de saida bloqueada -> {address}")


def _guard_create(address, *a, **k):
    if _is_local(address):
        _local.append(address)
        return _orig_create(address, *a, **k)
    _remote.append(address)
    raise RuntimeError(f"PROVA: tentativa de conexao de saida bloqueada -> {address}")


socket.socket.connect = _guard_connect
socket.create_connection = _guard_create

# ---- 3) exercitar o store de verdade, sob o guarda ----------------------------------------
sys.path.insert(0, HERE)
from store import config, get_store  # noqa: E402
from store.schema import TABLE_ORDER  # noqa: E402
import snapshot  # noqa: E402

errors = []


def step(name, fn):
    try:
        return fn()
    except Exception as e:  # erro nunca engolido: vira linha com marcador
        errors.append(f"ERRO em {name}: {type(e).__name__}: {e}")
        return None


store = step("get_store", get_store)
if store is None or getattr(store, "name", None) != "sqlite":
    print("ERRO: store nao e sqlite; abortando", file=sys.stderr)
    print("\n".join(errors), file=sys.stderr)
    sys.exit(1)

step("insert_fact", lambda: store.insert_fact(
    fact="prova offline: fato de teste", kind="note", scope="test",
    source_session_id=None, machine="prova", embedding=None))
step("upsert_session", lambda: store.upsert_session(
    {"id": "prova-session", "session_id": "prova-session", "project": "agent-memory-hub",
     "summary": "prova offline", "content": "prova offline", "started_at": "2026-01-01T00:00:00Z",
     "tool": "test", "machine": "prova"}))

# contar linhas realmente gravadas (medida, nao "o metodo retornou")
import sqlite3  # noqa: E402
conn = sqlite3.connect(os.environ["AMH_DB"])
rows = {}
for t in TABLE_ORDER:
    try:
        rows[t] = conn.execute(f"select count(*) from {t}").fetchone()[0]
    except sqlite3.Error:
        rows[t] = "?"
conn.close()

# gerar recipient efemero e exportar snapshot cifrado, sob o guarda
import subprocess  # noqa: E402
id_path = os.path.join(TMP, "id.txt")
pub = subprocess.run(["age-keygen", "-o", id_path], capture_output=True, text=True)
recipient = ""
for line in (pub.stderr + pub.stdout).splitlines():
    if "age1" in line:
        recipient = line.split()[-1]
out_age = os.path.join(TMP, "snap.age")


class _Args:
    to = out_age
    recipient = recipient


rc = step("export", lambda: snapshot.cmd_export(_Args()))
cipher_bytes = os.path.getsize(out_age) if os.path.exists(out_age) else 0

# ---- 4) veredito por medida ---------------------------------------------------------------
print("== prova offline (backend sqlite) ==")
print(f"  db:                 {os.environ['AMH_DB']}")
print(f"  connect locais:     {len(_local)}")
print(f"  connect remotos:    {len(_remote)}")
print(f"  linhas gravadas:    facts={rows.get('facts')} sessions={rows.get('sessions')}")
print(f"  snapshot cifrado:   {cipher_bytes} bytes")
if errors:
    print("  erros:")
    for e in errors:
        print(f"    - {e}")

ok = (len(_remote) == 0
      and rows.get("facts") == 1
      and rows.get("sessions") == 1
      and cipher_bytes > 0
      and not errors)
if _remote:
    print("  DESTINOS REMOTOS TENTADOS:")
    for a in _remote:
        print(f"    -> {a}")
print("RESULTADO:", "OK — nada saiu pela rede" if ok else "FALHOU")
sys.exit(0 if ok else 1)
