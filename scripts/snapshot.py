#!/usr/bin/env python3
"""
agent-memory-hub — snapshot cifrado de uma máquina para outra, por transporte manual.

Para a máquina do trabalho, cuja rede não deve enviar nada: ela roda o backend local
(AMH_BACKEND=sqlite), gera aqui um snapshot cifrado com `age` e você o leva por iCloud ou
AirDrop. Na máquina pessoal, `merge` decifra e junta ao hub, sem duplicar o que já existe e
sem apagar nada. Nenhum dado sai de uma máquina sem você rodar o comando.

  export  (na origem)  snapshot consistente do SQLite local -> arquivo .age
  merge   (no destino) decifra o .age e junta ao backend atual, tabela a tabela

Porta de cobertura (merge): para cada tabela, lidas == novas + já_existentes, e, depois de
gravar, as chaves do snapshot estão todas no destino e o destino cresceu exatamente o número
de novas. Se não fechar, levanta — o resultado não vale. Nada é deletado nunca.

Chaves (env ou .env):
  SNAPSHOT_AGE_RECIPIENT  destinatário age ("age1…") ou caminho de um arquivo de recipients
  SNAPSHOT_AGE_IDENTITY   caminho do arquivo de identidade age (chave privada), para o merge

Uso:
  python3 scripts/snapshot.py export --to ~/trabalho.age [--recipient age1… | arquivo]
  python3 scripts/snapshot.py merge ~/trabalho.age [--identity arquivo] [--dry-run]
"""
import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from store import config, get_store  # noqa: E402
from store.schema import GENERATED, TABLE_ORDER, primary_key  # noqa: E402
from store.sqlite import COLUMNS, SqliteStore  # noqa: E402


def _key(table, row):
    return tuple(row[c] for c in primary_key(table))


def _put(store, table, rows):
    """Grava linhas no destino, seja ele qual for. Só recebe linhas novas (chave inédita)."""
    if not rows:
        return
    if getattr(store, "name", None) == "sqlite":
        store.load_rows(table, rows)
    else:
        on_conflict = ",".join(primary_key(table))
        for r in rows:
            store.upsert(table, r, on_conflict)


def merge_tables(src, dst, dry=False, log=print):
    """Junta o snapshot (src) no destino (dst). Devolve {tabela: (lidas, novas, já, gravadas)};
    levanta se a cobertura não fechar. Não sobrescreve nem apaga linha do destino."""
    report = {}
    for table in TABLE_ORDER:
        snap = src.table_rows(table, page=1000)
        extra = set().union(*(r.keys() for r in snap)) - set(COLUMNS[table]) - set(GENERATED.get(table, ())) if snap else set()
        if extra:
            raise SystemExit(f"ERRO: {table} tem colunas que o destino não conhece: {sorted(extra)}")
        snap_keys = {_key(table, r) for r in snap}
        if len(snap_keys) != len(snap):
            raise SystemExit(f"ERRO: {table} no snapshot tem chave repetida ({len(snap)} linhas, {len(snap_keys)} chaves)")
        pk = ",".join(primary_key(table))
        before = {_key(table, r) for r in dst.table_rows(table, fields=pk, page=1000)}
        novas = [r for r in snap if _key(table, r) not in before]
        ja = len(snap) - len(novas)
        if len(snap) != len(novas) + ja:          # porta de cobertura: lidas == novas + já
            raise SystemExit(f"ERRO: {table}: {len(snap)} lidas != {len(novas)} novas + {ja} já existentes")
        gravadas = 0
        if not dry and novas:
            _put(dst, table, novas)
            after = {_key(table, r) for r in dst.table_rows(table, fields=pk, page=1000)}
            if not snap_keys <= after:
                faltam = len(snap_keys - after)
                raise SystemExit(f"ERRO: {table}: {faltam} chaves do snapshot não entraram no destino")
            if len(after) != len(before) + len(novas):
                raise SystemExit(f"ERRO: {table}: destino foi de {len(before)} para {len(after)} "
                                 f"linhas; esperado +{len(novas)} (não sobrescrever nem apagar)")
            gravadas = len(novas)
        report[table] = (len(snap), len(novas), ja, gravadas)
        log(f"  {table:20} {len(snap):6} lidas  {len(novas):6} novas  {ja:6} já  {gravadas:6} gravadas")
    return report


def _consistent_copy(src_path, dst_path):
    """Cópia consistente do SQLite vivo (API de backup), mesmo com escrita em curso. Confere que
    cada tabela tem o mesmo número de linhas nos dois lados; levanta se divergir."""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
        counts = {}
        for table in TABLE_ORDER:
            a = src.execute(f"select count(*) from {table}").fetchone()[0]
            b = dst.execute(f"select count(*) from {table}").fetchone()[0]
            if a != b:
                raise SystemExit(f"ERRO: cópia de {table} divergiu: origem {a} != cópia {b}")
            counts[table] = a
        return counts
    finally:
        src.close()
        dst.close()


def _age_recipient_args(recipient):
    if os.path.exists(os.path.expanduser(recipient)):
        return ["-R", os.path.expanduser(recipient)]
    return ["-r", recipient]


def cmd_export(args):
    src = get_store()
    if getattr(src, "name", None) != "sqlite":
        print("ERRO: snapshot é do backend local; defina AMH_BACKEND=sqlite nesta máquina", file=sys.stderr)
        return 1
    if not os.path.exists(src.path):
        print(f"ERRO: banco local não existe: {src.path}", file=sys.stderr)
        return 1
    recipient = args.recipient or config.get("SNAPSHOT_AGE_RECIPIENT")
    if not recipient:
        print("ERRO: falta o destinatário age (--recipient ou SNAPSHOT_AGE_RECIPIENT no .env)", file=sys.stderr)
        return 1
    out = os.path.expanduser(args.to)
    tmpdir = tempfile.mkdtemp(prefix="amh-snap-")
    plain = os.path.join(tmpdir, "snapshot.db")
    try:
        counts = _consistent_copy(src.path, plain)
        print(f"snapshot de {src.path}")
        for table in TABLE_ORDER:
            print(f"  {table:20} {counts[table]:6} linhas")
        plain_bytes = os.path.getsize(plain)
        cmd = ["age", "--encrypt", *_age_recipient_args(recipient), "-o", out, plain]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"ERRO: age falhou: {r.stderr.strip()}", file=sys.stderr)
            return 1
        cipher_bytes = os.path.getsize(out)
        if cipher_bytes == 0:
            print("ERRO: arquivo cifrado ficou vazio", file=sys.stderr)
            return 1
        print(f"cifrado -> {out}  ({plain_bytes} bytes claros -> {cipher_bytes} bytes cifrados)")
        print("leve por iCloud ou AirDrop; no destino: amh merge <arquivo>")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)   # não deixar cópia em claro


def cmd_merge(args):
    src_file = os.path.expanduser(args.file)
    if not os.path.exists(src_file):
        print(f"ERRO: arquivo não existe: {src_file}", file=sys.stderr)
        return 1
    identity = args.identity or config.get("SNAPSHOT_AGE_IDENTITY")
    if not identity:
        print("ERRO: falta a identidade age (--identity ou SNAPSHOT_AGE_IDENTITY no .env)", file=sys.stderr)
        return 1
    identity = os.path.expanduser(identity)
    if not os.path.exists(identity):
        print(f"ERRO: identidade age não existe: {identity}", file=sys.stderr)
        return 1
    dst = get_store()
    if not getattr(dst, "configured", False):
        print("ERRO: backend de destino não configurado", file=sys.stderr)
        return 1
    tmpdir = tempfile.mkdtemp(prefix="amh-merge-")
    plain = os.path.join(tmpdir, "snapshot.db")
    try:
        r = subprocess.run(["age", "--decrypt", "-i", identity, "-o", plain, src_file],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"ERRO: age falhou ao decifrar: {r.stderr.strip()}", file=sys.stderr)
            return 1
        snap = SqliteStore(path=plain)
        print(f"{'(dry-run) ' if args.dry_run else ''}merge {src_file} -> backend {getattr(dst, 'name', '?')}")
        report = merge_tables(snap, dst, dry=args.dry_run)
        novas = sum(v[1] for v in report.values())
        if args.dry_run:
            print(f"(dry-run) entrariam {novas} linhas novas; nada gravado")
        else:
            print(f"ok; {novas} linhas novas. Gere embeddings do que entrou: amh extract --embed")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)   # não deixar cópia em claro


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export", help="snapshot cifrado do SQLite local")
    ex.add_argument("--to", required=True, help="arquivo .age de saída")
    ex.add_argument("--recipient", help="destinatário age ou arquivo de recipients (senão SNAPSHOT_AGE_RECIPIENT)")
    mg = sub.add_parser("merge", help="decifra e junta ao backend atual")
    mg.add_argument("file", help="arquivo .age a juntar")
    mg.add_argument("--identity", help="identidade age (senão SNAPSHOT_AGE_IDENTITY)")
    mg.add_argument("--dry-run", action="store_true", help="só conta o que entraria; não grava")
    args = ap.parse_args(argv)
    return cmd_export(args) if args.cmd == "export" else cmd_merge(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
