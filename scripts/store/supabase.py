"""
agent-memory-hub — Supabase backend: PostgREST over HTTP, plus the embed Edge Function.

The only place that talks to Supabase. Modules are moved onto it one at a time (see
store/__init__.py); until then the low-level calls (rest/rest_all/count/rpc/write/embed)
are what memory_client re-exports. Pure stdlib.
"""
import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import config


class SupabaseStore:
    name = "supabase"

    def __init__(self, url=None, key=None, embed_key=None):
        self.url = url if url is not None else config.get("SUPABASE_URL")
        self.key = key if key is not None else config.get("SUPABASE_SECRET_KEY")
        self.embed_key = embed_key if embed_key is not None else config.get("EMBED_KEY")
        self.headers = {"apikey": self.key or "", "Authorization": f"Bearer {self.key or ''}",
                        "Content-Type": "application/json"}

    @property
    def configured(self):
        return bool(self.url and self.key)

    # ---- PostgREST -----------------------------------------------------------------------
    def rest(self, path, timeout=20):
        req = urllib.request.Request(f"{self.url}/rest/v1/{path}", headers=self.headers)
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

    def rest_all(self, path, page=1000):
        """rest() paginado: o PostgREST corta cada resposta em 1000 linhas (max-rows)."""
        rows, offset = [], 0
        sep = "&" if "?" in path else "?"
        while True:
            chunk = self.rest(f"{path}{sep}limit={page}&offset={offset}")
            rows += chunk
            if len(chunk) < page:
                return rows
            offset += page

    def count(self, path):
        """Total exato de linhas via Content-Range, sem baixar as linhas."""
        sep = "&" if "?" in path else "?"
        req = urllib.request.Request(f"{self.url}/rest/v1/{path}{sep}limit=1",
                                     headers={**self.headers, "Prefer": "count=exact"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return int(r.headers["Content-Range"].rsplit("/", 1)[1])

    def rpc(self, name, body, timeout=30):
        req = urllib.request.Request(f"{self.url}/rest/v1/rpc/{name}",
                                     data=json.dumps(body).encode(), method="POST", headers=self.headers)
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

    def parallel(self, calls, timeout=10):
        """Roda consultas independentes em paralelo (cada uma é um round-trip HTTPS de ~300 ms).
        calls: {nome: ("rest", path) | ("rpc", (função, corpo)) | None}. Devolve {nome: resultado};
        uma consulta que falha vira None, sem derrubar as outras."""
        def one(spec):
            kind, arg = spec
            return self.rpc(*arg, timeout=timeout) if kind == "rpc" else self.rest(arg, timeout=timeout)

        jobs = {k: v for k, v in calls.items() if v}
        out = {}
        with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
            futures = {k: pool.submit(one, v) for k, v in jobs.items()}
            for k, f in futures.items():
                try:
                    out[k] = f.result()
                except Exception:
                    out[k] = None
        return out

    def write(self, path, body, method="PATCH", prefer="return=minimal"):
        req = urllib.request.Request(f"{self.url}/rest/v1/{path}", data=json.dumps(body).encode(),
                                     method=method, headers={**self.headers, "Prefer": prefer})
        urllib.request.urlopen(req, timeout=20).read()

    # ---- embeddings (Edge Function gte-small, 384 dims) ------------------------------------
    def embed(self, text):
        req = urllib.request.Request(f"{self.url}/functions/v1/embed",
                                     data=json.dumps({"text": text}).encode(), method="POST",
                                     headers={"x-embed-key": self.embed_key or "",
                                              "Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=20).read())["embedding"]

    # ---- operações de domínio --------------------------------------------------------------
    RECALL_SESSION_FIELDS = "session_id,started_at,ended_at,machine,tool,project,summary"

    def recall_bundle(self, project):
        """O que o recall do SessionStart injeta, numa ida só (consultas em paralelo):
          proj     sessões recentes do projeto (6)         recent   recentes no geral (4)
          facts    fatos válidos do projeto + globais (12)  pending  padrões de perfil propostos (3)
          last     a última ATIVIDADE (ended_at) do projeto: decide se há passagem de contexto
        Só o resumo, nunca o content: sessões longas têm MBs. Chave que falhou vem None."""
        sel = f"select={self.RECALL_SESSION_FIELDS}"
        q = urllib.parse.quote(project) if project else None
        scope = f"or=(scope.eq.{q},scope.is.null)" if project else "scope=is.null"
        return self.parallel({
            "proj": ("rest", f"sessions?project=eq.{q}&order=started_at.desc&limit=6&{sel}") if project else None,
            "recent": ("rest", f"sessions?order=started_at.desc&limit=4&{sel}"),
            "facts": ("rest", f"facts?valid_until=is.null&{scope}&order=created_at.desc&limit=12"
                              "&select=fact,kind,scope,confidence,valid_from,code_check"),
            "pending": ("rest", "profile_patterns?status=eq.proposed&order=confidence.desc&limit=3"
                                "&select=pattern,confidence,evidence"),
            "last": ("rest", f"sessions?project=eq.{q}&order=ended_at.desc.nullslast&limit=1&{sel}")
                    if project else None,
        })

    def handoff_detail(self, session_id):
        """{"content": fim da conversa principal, "metadata": {...}} da sessão (handoff.py)."""
        res = self.parallel({
            "tail": ("rpc", ("session_main_tail", {"p_session_id": session_id})),
            "meta": ("rest", f"sessions?session_id=eq.{urllib.parse.quote(session_id)}&select=metadata"),
        })
        return {"content": res.get("tail") or "",
                "metadata": ((res.get("meta") or [{}])[0] or {}).get("metadata") or {}}
