// agent-memory-hub — Phase 2 embedding Edge Function (gte-small, 384-dim).
//
// Two modes (POST JSON):
//   { "text": "..." }   -> returns { embedding } (embed a query at search time)
//   { "limit": N }      -> backfill: embed up to N rows missing an embedding (returns counts)
//
// Auth: a shared-secret header `x-embed-key` (deployed with verify_jwt=false, because the
// new Supabase API keys are not JWTs). Set the secret with:
//   supabase secrets set EMBED_KEY=<random>
// and deploy with: supabase functions deploy embed --no-verify-jwt
//
// The function returns NO session content (only vectors / counts).

import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

const model = new Supabase.ai.Session("gte-small");
const EMBED_KEY = Deno.env.get("EMBED_KEY") ?? "";
const MAX_CHARS = 2000;          // gte-small caps ~512 tokens; keeps compute light
const MAX_BATCH = 10;            // Edge free-tier compute is tight; keep batches small

Deno.serve(async (req) => {
  if (!EMBED_KEY || req.headers.get("x-embed-key") !== EMBED_KEY) {
    return new Response(JSON.stringify({ error: "unauthorized" }), { status: 401 });
  }

  let body: any = {};
  try { body = await req.json(); } catch { /* empty body */ }

  if (typeof body.text === "string" && body.text.length > 0) {
    const embedding = await model.run(body.text.slice(0, MAX_CHARS), { mean_pool: true, normalize: true });
    return Response.json({ embedding });
  }

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );
  const limit = Math.min(typeof body.limit === "number" ? body.limit : MAX_BATCH, MAX_BATCH);
  const { data: rows, error } = await supabase
    .from("sessions").select("id, content").is("embedding", null).limit(limit);
  if (error) return Response.json({ error: error.message }, { status: 500 });

  let embedded = 0;
  for (const r of rows ?? []) {
    const emb = await model.run((r.content ?? "").slice(0, MAX_CHARS), { mean_pool: true, normalize: true });
    const { error: upErr } = await supabase.from("sessions").update({ embedding: JSON.stringify(emb) }).eq("id", r.id);
    if (!upErr) embedded++;
  }
  return Response.json({ embedded, scanned: rows?.length ?? 0 });
});
