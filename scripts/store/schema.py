"""
agent-memory-hub — the tables every backend holds, in foreign-key order, with their keys.
Shared by the backends and by the copy between them (scripts/copy_store.py).
"""
TABLE_ORDER = ("sessions", "session_chunks", "facts", "profile_patterns", "ops_status",
               "config_items", "config_assignments", "skill_promotions")
PRIMARY_KEY = {"ops_status": ("machine",), "skill_promotions": ("fact_id",), "session_chunks": ("session_fk", "chunk_idx"),
               "config_assignments": ("item_id", "machine"),
               # sessions: a identidade entre máquinas é o session_id (unique), não o id surrogate
               # gerado por máquina — senão o merge duplica/colide (409) com o que já está no hub.
               "sessions": ("session_id",)}
GENERATED = {"sessions": ("content_tsv",)}   # colunas que o Postgres calcula: não se copiam


def primary_key(table):
    return PRIMARY_KEY.get(table, ("id",))
