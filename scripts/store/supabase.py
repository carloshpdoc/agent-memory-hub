"""
agent-memory-hub — Supabase backend: PostgREST over HTTP, plus the embed Edge Function.

The only place that talks to Supabase. Modules are moved onto it one at a time (see
store/__init__.py); until then the low-level calls (rest/rest_all/count/rpc/write/embed)
are what memory_client re-exports. Pure stdlib.
"""
import json
import urllib.request

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
    def rest(self, path):
        req = urllib.request.Request(f"{self.url}/rest/v1/{path}", headers=self.headers)
        return json.loads(urllib.request.urlopen(req, timeout=20).read())

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

    def rpc(self, name, body):
        req = urllib.request.Request(f"{self.url}/rest/v1/rpc/{name}",
                                     data=json.dumps(body).encode(), method="POST", headers=self.headers)
        return json.loads(urllib.request.urlopen(req, timeout=30).read())

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
