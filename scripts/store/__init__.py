"""
agent-memory-hub — storage interface (one place that knows where the memory lives).

get_store() returns the backend chosen by MEM_BACKEND (default: supabase). Today only the
Supabase backend exists; a local SQLite backend with log-based sync between machines comes
next (plan: local-first store). Modules move onto the store one at a time; a module that
still builds its own Supabase URLs is a migration to do, not a pattern to copy.
"""
from . import config

BACKENDS = ("supabase",)
_store = None


def get_store():
    """O backend configurado, criado uma vez por processo."""
    global _store
    if _store is None:
        name = (config.get("MEM_BACKEND") or "supabase").lower()
        if name not in BACKENDS:
            raise ValueError(f"MEM_BACKEND={name!r} desconhecido; opções: {', '.join(BACKENDS)}")
        from .supabase import SupabaseStore
        _store = SupabaseStore()
    return _store
