#!/usr/bin/env python3
"""
agent-memory-hub — copy the whole memory from the Supabase backend into a local SQLite file.

Every table, keeping ids, timestamps and vectors (embeddings, chunks), so the SQLite backend
can be compared with Supabase on the same data (eval_recall, eval_answers) before anyone
switches MEM_BACKEND. Supabase is only read. The destination must be empty: a copy is a
snapshot, and rows deleted in Supabase must not survive in it.

Coverage gate: for each table, the keys read == the keys written; an unknown column (one the
SQLite schema does not have) stops the copy instead of being dropped. Exit 1 on any mismatch.

Usage:  python3 scripts/copy_store.py --db ~/.local/share/agent-memory-hub/mem.db [--dry-run]
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from store.schema import GENERATED, TABLE_ORDER, primary_key  # noqa: E402
from store.sqlite import COLUMNS, SqliteStore  # noqa: E402
from store.supabase import SupabaseStore  # noqa: E402

PAGE = {"sessions": 50}          # content de MBs: página pequena (como o backup)


def _key(table, row):
    return tuple(row[c] for c in primary_key(table))


def copy(src, dst, dry=False, log=print):
    """Copia tabela a tabela. Devolve {tabela: (lidas, gravadas)}; levanta se algo não fecha."""
    if not dry and any(dst.table_rows(t) for t in ("sessions", "facts")):
        raise SystemExit(f"ERRO: {dst.path} já tem dados; a cópia precisa de um arquivo vazio")
    report = {}
    for table in TABLE_ORDER:
        t0 = time.time()
        rows = src.table_rows(table, page=PAGE.get(table, 1000))
        extra = set().union(*(r.keys() for r in rows)) - set(COLUMNS[table]) - set(GENERATED.get(table, ()))
        if extra:
            raise SystemExit(f"ERRO: {table} tem colunas que o SQLite não conhece: {sorted(extra)}")
        keys = {_key(table, r) for r in rows}
        if len(keys) != len(rows):
            raise SystemExit(f"ERRO: {table} veio com chave repetida ({len(rows)} linhas, {len(keys)} chaves)")
        if not dry:
            dst.load_rows(table, rows)
            written = {_key(table, r) for r in dst.table_rows(table, ",".join(primary_key(table)))}
            if written != keys:
                raise SystemExit(f"ERRO: {table}: {len(keys - written)} faltando, {len(written - keys)} a mais")
        report[table] = (len(rows), 0 if dry else len(keys))
        log(f"  {table:20} {len(rows):6} lidas  {report[table][1]:6} gravadas  {time.time() - t0:5.1f}s")
    return report


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, help="arquivo SQLite de destino (vazio ou inexistente)")
    ap.add_argument("--dry-run", action="store_true", help="só lê o Supabase e confere as colunas")
    args = ap.parse_args(argv)
    src = SupabaseStore()
    if not src.configured:
        print("ERRO: SUPABASE_URL/SUPABASE_SECRET_KEY ausentes", file=sys.stderr)
        return 1
    dst = SqliteStore(path=os.path.expanduser(args.db))
    print(f"{'(dry-run) ' if args.dry_run else ''}Supabase -> {dst.path}")
    copy(src, dst, args.dry_run)
    if not args.dry_run:
        print(f"ok; busca vetorial via {dst.vec_backend}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
