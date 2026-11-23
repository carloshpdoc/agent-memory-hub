#!/usr/bin/env python3
"""
agent-memory-hub — fluxo da máquina do trabalho num comando só.

Captura as sessões locais no SQLite e gera o snapshot cifrado (`age`), TUDO sob
`sandbox-exec` com a rede negada. A garantia de que nada saiu não é inferência: é do
sistema operacional, que nega rede a este processo e a toda a sua descendência — inclusive
os subprocessos de captura. Se qualquer um tentar conectar, falha na hora.

Aborta antes de capturar se AMH_BACKEND não for sqlite, para a captura nunca ir ao Supabase
por engano. Mede o que importa: linhas de facts/sessions realmente gravadas no banco e os
bytes do arquivo cifrado.

Uso:
  python3 scripts/work_snapshot.py [--to ARQUIVO.age]   (default ~/Documents/trabalho-AAAAMMDD.age)
  amh work-export [--to ARQUIVO.age]
"""
import argparse
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SANDBOX_PROFILE = "(version 1)(allow default)(deny network*)"


def _store():
    sys.path.insert(0, HERE)
    from store import get_store
    return get_store()


def _counts(db_path):
    import sqlite3
    if not os.path.exists(db_path):
        return {"facts": 0, "sessions": 0}
    conn = sqlite3.connect(db_path)
    try:
        out = {}
        for t in ("facts", "sessions"):
            try:
                out[t] = conn.execute(f"select count(*) from {t}").fetchone()[0]
            except sqlite3.Error:
                out[t] = 0
        return out
    finally:
        conn.close()


def _reexec_sandboxed(argv):
    """Substitui este processo por ele mesmo dentro do sandbox (rede negada). Volta só se já
    estivermos dentro. Sem sandbox-exec, aborta: não dá para garantir ausência de rede."""
    if os.environ.get("AMH_SANDBOXED") == "1":
        return
    sx = shutil.which("sandbox-exec") or ("/usr/bin/sandbox-exec"
                                          if os.path.exists("/usr/bin/sandbox-exec") else None)
    if not sx:
        print("ERRO: sandbox-exec não encontrado; não consigo garantir que nada sai pela rede. "
              "Abortando (nada foi capturado).", file=sys.stderr)
        sys.exit(3)
    env = dict(os.environ, AMH_SANDBOXED="1")
    cmd = [sx, "-p", SANDBOX_PROFILE, sys.executable, os.path.abspath(__file__), *argv]
    os.execve(sx, cmd, env)   # não retorna


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--to", default=None, help="arquivo .age de saída")
    args = ap.parse_args(argv)

    # 1) porta de segurança: sqlite obrigatório, senão a captura iria pro Supabase
    store = _store()
    if getattr(store, "name", None) != "sqlite":
        print(f"ERRO: AMH_BACKEND={getattr(store, 'name', None)!r}; aqui precisa ser 'sqlite'. "
              "Ajuste o .env e rode de novo.", file=sys.stderr)
        return 2
    db = store.path

    # 2) daqui pra baixo, rede negada pelo SO para todo o processo e seus filhos
    _reexec_sandboxed(argv)

    before = _counts(db)
    print(f"== fluxo do trabalho (rede negada pelo SO) ==\n  banco: {db}")
    print(f"  antes: facts={before['facts']} sessions={before['sessions']}")

    import backfill_sessions
    import snapshot

    print("== 1/2 captura local ==")
    cap_rc = backfill_sessions.main([])

    after = _counts(db)
    print(f"  depois: facts={after['facts']} sessions={after['sessions']} "
          f"(+{after['sessions'] - before['sessions']} sessões, +{after['facts'] - before['facts']} facts)")

    out = os.path.expanduser(args.to) if args.to else os.path.expanduser(
        time.strftime("~/Documents/trabalho-%Y%m%d.age"))
    print(f"== 2/2 export cifrado -> {out} ==")

    class _ExpArgs:
        pass
    ea = _ExpArgs()
    ea.to, ea.recipient = out, None
    exp_rc = snapshot.cmd_export(ea)

    cipher = os.path.getsize(out) if os.path.exists(out) else 0
    print("== resultado ==")
    print(f"  captura: {'ok' if cap_rc == 0 else 'FALHOU (veja acima)'}")
    print(f"  cifrado: {cipher} bytes em {out}")
    ok = cap_rc == 0 and exp_rc == 0 and cipher > 0
    if ok:
        print(f"RESULTADO: OK — leve {out} por iCloud/AirDrop; na máquina pessoal: amh merge <arquivo>")
    else:
        print("RESULTADO: FALHOU", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
