"""
agent-memory-hub — storage interface (one place that knows where the memory lives).

get_store() returns the backend chosen by AMH_BACKEND (ou o antigo MEM_BACKEND): supabase (default, shared by
every machine) or sqlite (one local file per machine, AMH_DB/MEM_DB; no sync between machines yet). Modules move onto the store one at a time; a module that
still builds its own Supabase URLs is a migration to do, not a pattern to copy.
"""
from . import config

BACKENDS = ("supabase", "sqlite")
_store = None


def get_store():
    """O backend configurado, criado uma vez por processo."""
    global _store
    if _store is None:
        name = (config.get("AMH_BACKEND") or config.get("MEM_BACKEND") or "supabase").lower()
        if name not in BACKENDS:
            raise ValueError(f"AMH_BACKEND={name!r} desconhecido; opções: {', '.join(BACKENDS)}")
        if name == "sqlite":
            from .sqlite import SqliteStore
            _store = SqliteStore()
        else:
            from .supabase import SupabaseStore
            _store = SupabaseStore()
    return _store
