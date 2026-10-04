'use strict';

const TOKEN_KEY = 'amh-dashboard-token';
const STALE_HOURS = 36;           // mesmo limite do `mem health` para o nightly
const SNAPSHOT_STALE_HOURS = 24;  // ops_status: o hook empurra a cada 30 min enquanto ha uso
const SERIES_COLORS = ['var(--bar)', 'var(--bar-2)', 'var(--bar-3)'];
const FACT_RENDER_LIMIT = 300;
const state = { overview: null, facts: null, profile: null, recall: null };

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtInt = (n) => (n ?? 0).toLocaleString('pt-BR');

function getToken() { try { return localStorage.getItem(TOKEN_KEY); } catch { return null; } }
function setToken(t) { try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch {} }

function ago(iso) {
  if (!iso) return '—';
  const h = (Date.now() - new Date(iso).getTime()) / 36e5;
  if (h < 1) return `há ${Math.max(1, Math.round(h * 60))} min`;
  if (h < 48) return `há ${Math.round(h)} h`;
  return `há ${Math.round(h / 24)} d`;
}
const hoursSince = (iso) => (iso ? (Date.now() - new Date(iso).getTime()) / 36e5 : Infinity);
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' }) : '—');
const fmtSecs = (s) => (s == null ? '' : s >= 60 ? `${Math.round(s / 60)} min` : `${s}s`);

class AuthError extends Error {}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { Authorization: `Bearer ${getToken()}`, 'Content-Type': 'application/json', ...(opts.headers || {}) },
  });
  if (res.status === 401) throw new AuthError('token inválido');
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

/* ---------------- login ---------------- */
function showLogin(message) {
  $('#app').hidden = true;
  $('#login').hidden = false;
  $('#login-error').hidden = !message;
  $('#login-error').textContent = message || '';
  $('#token').focus();
}

$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  setToken($('#token').value.trim());
  await boot();
});

function handleError(err, target) {
  if (err instanceof AuthError) { setToken(null); showLogin('Token inválido.'); return; }
  if (target) target.innerHTML = `<div class="card error">Erro: ${esc(err.message)}</div>`;
  console.error(err);
}

/* ---------------- tabs ---------------- */
const loaders = { ops: loadOps, memory: () => loadFacts(), profile: loadProfile, recall: loadRecall };
let currentTab = 'ops';

function selectTab(name) {
  currentTab = name;
  document.querySelectorAll('.tabs button').forEach((b) => b.setAttribute('aria-selected', b.dataset.tab === name));
  document.querySelectorAll('.tab').forEach((s) => { s.hidden = s.id !== `tab-${name}`; });
  try { localStorage.setItem('amh-dashboard-tab', name); } catch {}
  loaders[name]();
}
document.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => selectTab(b.dataset.tab)));
$('#refresh').addEventListener('click', () => {
  Object.keys(state).forEach((k) => { state[k] = null; });
  loaders[currentTab](true);
});

document.querySelectorAll('.subtabs button').forEach((b) => b.addEventListener('click', () => {
  document.querySelectorAll('.subtabs button').forEach((x) => x.setAttribute('aria-selected', x === b));
  $('#sub-facts').hidden = b.dataset.sub !== 'facts';
  $('#sub-sessions').hidden = b.dataset.sub !== 'sessions';
  if (b.dataset.sub === 'sessions') loadSessions();
}));

/* ---------------- operação ---------------- */
function stackedBars(days, byMachine, machines) {
  const totals = days.map((d) => machines.reduce((acc, m) => acc + (byMachine[m]?.[d] || 0), 0));
  const max = Math.max(1, ...totals);
  const cols = days.map((d, i) => {
    const segs = machines.map((m, j) => {
      const v = byMachine[m]?.[d] || 0;
      return v ? `<div class="seg" style="height:${(v / max) * 84}px;background:${SERIES_COLORS[j % SERIES_COLORS.length]}"></div>` : '';
    }).join('');
    return `<div class="col" title="${esc(d)}: ${totals[i]} sessões">${segs}</div>`;
  }).join('');
  const legend = machines.map((m, j) =>
    `<span><i style="background:${SERIES_COLORS[j % SERIES_COLORS.length]}"></i>${esc(m)}</span>`).join('');
  return `<div class="chart">${cols}</div>
    <div class="chart-axis"><span>${esc(days[0].slice(5))}</span><span>máx ${max}/dia</span><span>${esc(days[days.length - 1].slice(5))}</span></div>
    <div class="legend">${legend}</div>`;
}

function nightlyCard(m) {
  const n = m.nightly;
  if (!n) return '';
  const failed = (n.steps || []).filter((s) => !s.ok).map((s) => s.step);
  const stale = hoursSince(n.finished_at) > STALE_HOURS;
  const pill = failed.length ? `<span class="pill bad">falhou: ${esc(failed.join(', '))}</span>`
    : stale ? '<span class="pill warn">atrasado</span>' : '<span class="pill ok">ok</span>';
  const steps = (n.steps || []).map((s) => `
    <div class="step ${s.ok ? '' : 'fail'}">
      <span class="dot"></span><strong>${esc(s.step)}</strong>
      <span class="line" title="${esc(s.error || s.last_line)}">${esc(s.error || s.last_line)}</span>
      <span class="muted secs">${fmtSecs(s.seconds)}</span>
    </div>`).join('');
  return `<div class="card">
    <h3>Nightly · ${esc(m.machine)} ${pill}</h3>
    <p class="muted" style="margin:0 0 8px;font-size:13px">última execução ${ago(n.finished_at)}${n.weekly ? ' · semanal' : ''}</p>
    <div class="steps">${steps}</div></div>`;
}

function machineCard(m) {
  const c = m.capture || {};
  const snapStale = hoursSince(m.updated_at) > SNAPSHOT_STALE_HOURS;
  const pill = c.err_24h ? `<span class="pill bad">${c.err_24h} erro(s) 24h</span>`
    : snapStale ? '<span class="pill warn">sem sinal</span>' : '<span class="pill ok">capturando</span>';
  const lastErr = c.last_err ? `<dt>último erro</dt><dd class="mono" title="${esc(c.last_err.line)}">${ago(c.last_err.ts)}</dd>` : '';
  return `<div class="card">
    <h3>${esc(m.machine)} ${pill}</h3>
    <dl class="kv">
      <dt>capturas 24h</dt><dd>${fmtInt(c.ok_24h)}</dd>
      <dt>última captura</dt><dd>${ago(c.last_ok)}</dd>
      ${lastErr}
      <dt>snapshot</dt><dd>${ago(m.updated_at)}</dd>
      <dt>skills</dt><dd>${m.skills.length}</dd>
      <dt>nightly</dt><dd>${m.nightly ? 'roda aqui' : '—'}</dd>
    </dl></div>`;
}

function skillsCard(machines) {
  const rows = machines.filter((m) => m.skills.length).map((m) => `
    <div class="card"><h3>Skills · ${esc(m.machine)} <span class="pill">${m.skills.length}</span></h3>
    ${m.skills.map((s) => `<div class="skill"><div><strong>${esc(s.name)}</strong>
      <div class="desc">${esc(s.description || '')}</div></div>
      <span class="muted" style="font-size:12px;white-space:nowrap">${esc(s.dir.replace('~/.', ''))} · ${ago(s.modified)}</span></div>`).join('')}
    </div>`).join('');
  return rows ? `<h2>Skills instaladas</h2><div class="grid wide">${rows}</div>` : '';
}

async function loadOps(force) {
  const root = $('#tab-ops');
  if (!state.overview || force) root.innerHTML = '<div class="empty">Carregando…</div>';
  try {
    const o = state.overview || (state.overview = await api('/api/overview'));
    const t = o.totals;
    const machines = Object.keys(o.daily.by_machine).sort();
    root.innerHTML = `
      <div class="grid">
        <div class="card stat"><div class="label">Sessões</div><div class="value">${fmtInt(t.sessions)}</div></div>
        <div class="card stat"><div class="label">Fila de extração</div><div class="value">${fmtInt(t.pending)}</div></div>
        <div class="card stat"><div class="label">Fatos válidos</div><div class="value">${fmtInt(t.facts)}</div></div>
        <div class="card stat"><div class="label">Padrões a revisar</div><div class="value">${fmtInt(t.patterns_proposed)}</div></div>
      </div>
      <h2>Máquinas</h2>
      <div class="grid wide">${o.machines.map(machineCard).join('') || '<div class="card muted">Nenhum snapshot ainda (ops_status vazio).</div>'}
        ${o.machines.map(nightlyCard).join('')}</div>
      <h2>Sessões por dia · ${o.daily.days.length} dias</h2>
      <div class="card">${stackedBars(o.daily.days, o.daily.by_machine, machines)}</div>
      <h2>Captura por máquina e ferramenta</h2>
      <div class="table-wrap"><table>
        <thead><tr><th>Máquina</th><th>Ferramenta</th><th class="num">Sessões</th><th class="num">Sem extração</th><th>Última</th></tr></thead>
        <tbody>${o.groups.map((g) => `<tr><td>${esc(g.machine)}</td><td>${esc(g.tool)}</td>
          <td class="num">${fmtInt(g.sessions)}</td><td class="num">${fmtInt(g.pending)}</td><td>${ago(g.last)}</td></tr>`).join('')}</tbody>
      </table></div>
      ${skillsCard(o.machines)}
      <p class="muted" style="font-size:12px;margin-top:16px">gerado ${fmtDate(o.generated_at)}</p>`;
  } catch (err) { handleError(err, root); }
}

/* ---------------- memória: fatos ---------------- */
async function loadFacts(force) {
  const root = $('#sub-facts');
  if (!state.facts || force) root.innerHTML = '<div class="empty">Carregando…</div>';
  try {
    const facts = state.facts || (state.facts = await api('/api/facts'));
    const scopes = [...new Set(facts.map((f) => f.scope || 'global'))].sort();
    const kinds = [...new Set(facts.map((f) => f.kind || 'fact'))].sort();
    root.innerHTML = `
      <div class="toolbar">
        <input id="fact-q" type="search" placeholder="Buscar em ${fmtInt(facts.length)} fatos…">
        <select id="fact-scope"><option value="">Todos os projetos</option>${scopes.map((s) => `<option>${esc(s)}</option>`).join('')}</select>
        <select id="fact-kind"><option value="">Todos os tipos</option>${kinds.map((k) => `<option>${esc(k)}</option>`).join('')}</select>
        <select id="fact-sort"><option value="recent">Mais recentes</option><option value="conf">Maior confiança efetiva</option><option value="fading">Desbotando</option></select>
      </div>
      <div id="fact-list" class="list"></div>`;
    const render = () => {
      const q = $('#fact-q').value.trim().toLowerCase();
      const scope = $('#fact-scope').value;
      const kind = $('#fact-kind').value;
      const sort = $('#fact-sort').value;
      let rows = facts.filter((f) => (!scope || (f.scope || 'global') === scope)
        && (!kind || (f.kind || 'fact') === kind) && (!q || f.fact.toLowerCase().includes(q)));
      if (sort === 'conf') rows = rows.slice().sort((a, b) => (b.effective ?? 0) - (a.effective ?? 0));
      if (sort === 'fading') rows = rows.slice().sort((a, b) => ((a.effective ?? 0) / (a.confidence || 1)) - ((b.effective ?? 0) / (b.confidence || 1)));
      const shown = rows.slice(0, FACT_RENDER_LIMIT);
      $('#fact-list').innerHTML = shown.map((f) => {
        const eff = f.effective ?? 0;
        return `<div class="fact"><div>${esc(f.fact)}</div>
          <div class="meta"><span class="pill">${esc(f.kind)}</span><span>${esc(f.scope || 'global')}</span>
          <span title="confiança base ${f.confidence?.toFixed(2)} → efetiva ${eff.toFixed(2)}">conf ${eff.toFixed(2)}
            <span class="meter" style="display:inline-block;width:60px;vertical-align:middle"><span style="width:${Math.round(eff * 100)}%"></span></span></span>
          <span>desde ${esc((f.valid_from || '').slice(0, 10))}</span>
          ${f.source_session_id ? `<a href="#" data-session="${esc(f.source_session_id)}">sessão ${esc(f.source_session_id.slice(0, 8))}</a>` : ''}</div></div>`;
      }).join('') + (rows.length > shown.length ? `<div class="empty">mostrando ${shown.length} de ${fmtInt(rows.length)} — refine a busca</div>` : '')
        || '<div class="empty">Nenhum fato.</div>';
    };
    ['#fact-q', '#fact-scope', '#fact-kind', '#fact-sort'].forEach((s) => $(s).addEventListener('input', render));
    render();
  } catch (err) { handleError(err, root); }
}

/* ---------------- memória: sessões ---------------- */
let sessionsInit = false;
async function loadSessions() {
  const root = $('#sub-sessions');
  if (!sessionsInit) {
    sessionsInit = true;
    root.innerHTML = `<form id="sess-form" class="toolbar">
        <input id="sess-q" type="search" placeholder="Busca full-text no conteúdo das sessões…">
        <input id="sess-project" placeholder="projeto (opcional)" style="flex:0 1 200px">
        <button class="primary" type="submit">Buscar</button>
      </form><div id="sess-list"></div>`;
    $('#sess-form').addEventListener('submit', (e) => { e.preventDefault(); searchSessions(); });
  }
  searchSessions();
}

async function searchSessions() {
  const list = $('#sess-list');
  list.innerHTML = '<div class="empty">Carregando…</div>';
  const params = new URLSearchParams({ q: $('#sess-q').value.trim(), project: $('#sess-project').value.trim(), limit: '100' });
  try {
    const rows = await api(`/api/sessions?${params}`);
    list.innerHTML = rows.length ? `<div class="table-wrap"><table>
      <thead><tr><th>Quando</th><th>Projeto</th><th>Máquina · ferramenta</th><th>Resumo</th></tr></thead>
      <tbody>${rows.map((s) => `<tr class="click" data-session="${esc(s.session_id)}">
        <td style="white-space:nowrap">${fmtDate(s.started_at)}</td><td>${esc(s.project || '—')}</td>
        <td style="white-space:nowrap">${esc((s.machine || '').replace('.local', ''))} · ${esc(s.tool)}</td>
        <td>${esc((s.summary || '').slice(0, 220))}</td></tr>`).join('')}</tbody></table></div>`
      : '<div class="empty">Nenhuma sessão.</div>';
  } catch (err) { handleError(err, list); }
}

async function openSession(id) {
  const dlg = $('#session-dialog');
  $('#session-title').textContent = `sessão ${id.slice(0, 8)}`;
  $('#session-body').innerHTML = '<div class="empty">Carregando…</div>';
  dlg.showModal();
  try {
    const s = await api(`/api/sessions/${encodeURIComponent(id)}`);
    const facts = (s.facts || []).map((f) => `<div class="fact"><div>${esc(f.fact)}</div>
      <div class="meta"><span class="pill">${esc(f.kind)}</span>${f.valid_until ? '<span class="pill warn">invalidado</span>' : ''}</div></div>`).join('');
    $('#session-body').innerHTML = `
      <dl class="kv"><dt>projeto</dt><dd>${esc(s.project || '—')}</dd>
        <dt>máquina</dt><dd>${esc(s.machine)} · ${esc(s.tool)}</dd>
        <dt>período</dt><dd>${fmtDate(s.started_at)} → ${fmtDate(s.ended_at)}</dd>
        <dt>extração</dt><dd>${s.facts_extracted_at ? fmtDate(s.facts_extracted_at) : 'pendente'}</dd>
        <dt>id</dt><dd class="mono">${esc(s.session_id)}</dd></dl>
      <h2>Resumo</h2><p>${esc(s.summary || '—')}</p>
      <h2>Fatos extraídos (${(s.facts || []).length})</h2>
      ${facts ? `<div class="list">${facts}</div>` : '<p class="muted">nenhum</p>'}
      <h2>Transcript${s.content_chars > s.content.length ? ` · primeiros ${fmtInt(s.content.length)} de ${fmtInt(s.content_chars)} caracteres` : ''}</h2>
      <pre class="transcript">${esc(s.content)}</pre>`;
  } catch (err) { handleError(err, $('#session-body')); }
}

document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-session]');
  if (el) { e.preventDefault(); openSession(el.dataset.session); }
  if (e.target.closest('[data-close]')) $('#session-dialog').close();
});

/* ---------------- perfil ---------------- */
async function loadProfile(force) {
  const root = $('#tab-profile');
  if (!state.profile || force) root.innerHTML = '<div class="empty">Carregando…</div>';
  try {
    const rows = state.profile || (state.profile = await api('/api/profile'));
    const section = (status, title) => {
      const items = rows.filter((p) => p.status === status);
      if (!items.length) return status === 'proposed' ? `<h2>${title}</h2><div class="card muted">Nada aguardando revisão.</div>` : '';
      return `<h2>${title} · ${items.length}</h2><div class="grid wide">${items.map((p) => {
        const projects = (p.evidence && p.evidence.projects) || [];
        const actions = status === 'proposed'
          ? `<button class="approve" data-review="${p.id}" data-status="approved">Aprovar</button>
             <button class="reject" data-review="${p.id}" data-status="rejected">Rejeitar</button>`
          : `<button data-review="${p.id}" data-status="proposed">Reabrir</button>`;
        return `<div class="card pattern">
          <div class="meta muted" style="font-size:12px;display:flex;gap:8px;flex-wrap:wrap">
            <span class="pill">${esc(p.category)}</span><span>conf ${(p.confidence ?? 0).toFixed(2)}</span>
            <span>${projects.length} projeto(s)${projects.length ? ': ' + esc(projects.join(', ')) : ''}</span></div>
          <div>${esc(p.pattern)}</div>
          ${p.proposed_rule ? `<div class="rule"><span class="muted" style="font-size:12px">regra proposta</span><br>${esc(p.proposed_rule)}</div>` : ''}
          <div class="actions">${actions}</div></div>`;
      }).join('')}</div>`;
    };
    root.innerHTML = section('proposed', 'Aguardando revisão') + section('approved', 'Aprovados (viram regra)') + section('rejected', 'Rejeitados')
      + '<p class="muted" style="font-size:12px;margin-top:16px">Aprovar aqui equivale a <span class="mono">mem profile approve</span>. A regra entra no <span class="mono">profile-rules.md</span> quando o <span class="mono">apply_profile_rules.py</span> rodar na máquina.</p>';
  } catch (err) { handleError(err, root); }
}

document.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-review]');
  if (!btn) return;
  btn.disabled = true;
  try {
    await api(`/api/profile/${btn.dataset.review}`, { method: 'POST', body: JSON.stringify({ status: btn.dataset.status }) });
    state.profile = null;
    state.overview = null;
    loadProfile(true);
  } catch (err) { btn.disabled = false; handleError(err, null); alertInline(btn, err.message); }
});

function alertInline(btn, msg) {
  const p = document.createElement('p');
  p.className = 'error';
  p.textContent = msg;
  btn.parentElement.after(p);
}

/* ---------------- recall ---------------- */
async function loadRecall(force) {
  const root = $('#tab-recall');
  if (!state.recall || force) root.innerHTML = '<div class="empty">Carregando…</div>';
  try {
    const events = state.recall || (state.recall = await api('/api/recall'));
    if (!events.length) { root.innerHTML = '<div class="empty">Sem injeções registradas.</div>'; return; }
    const tokens = events.map((e) => e.est_tokens || 0);
    const avg = Math.round(tokens.reduce((a, b) => a + b, 0) / tokens.length);
    const cut = events.filter((e) => (e.dropped_facts || 0) + (e.dropped_sessions || 0) > 0).length;
    const index = events.filter((e) => e.style === 'index').length;
    const recent = events.slice(0, 60).reverse();
    const max = Math.max(1, ...recent.map((e) => e.est_tokens || 0));
    root.innerHTML = `
      <div class="grid">
        <div class="card stat"><div class="label">Injeções registradas</div><div class="value">${fmtInt(events.length)}</div></div>
        <div class="card stat"><div class="label">Tokens médios</div><div class="value">${fmtInt(avg)}</div></div>
        <div class="card stat"><div class="label">Com cortes</div><div class="value">${fmtInt(cut)}</div></div>
        <div class="card stat"><div class="label">Modo índice</div><div class="value">${fmtInt(index)}</div></div>
      </div>
      <h2>Tokens por injeção · últimas ${recent.length}</h2>
      <div class="card"><div class="chart">${recent.map((e) => `<div class="col" title="${esc(fmtDate(e.ts))} · ${esc(e.project || '—')} · ${e.est_tokens} tokens">
        <div class="seg" style="height:${((e.est_tokens || 0) / max) * 84}px;background:${(e.dropped_facts || e.dropped_sessions) ? 'var(--warn)' : 'var(--bar)'}"></div></div>`).join('')}</div>
        <div class="legend"><span><i style="background:var(--bar)"></i>completa</span><span><i style="background:var(--warn)"></i>com cortes</span><span>máx ${fmtInt(max)} tokens</span></div></div>
      <h2>Injeções</h2>
      <div class="table-wrap"><table>
        <thead><tr><th>Quando</th><th>Máquina</th><th>Projeto</th><th>Origem</th><th>Modo</th><th class="num">Tokens</th><th class="num">Fatos</th><th class="num">Sessões</th><th class="num">Cortados</th></tr></thead>
        <tbody>${events.slice(0, 200).map((e) => `<tr><td style="white-space:nowrap">${fmtDate(e.ts)}</td>
          <td>${esc((e.machine || '').replace('.local', ''))}</td><td>${esc(e.project || '—')}</td><td>${esc(e.source || '')}</td><td>${esc(e.style || '')}</td>
          <td class="num">${fmtInt(e.est_tokens)}</td><td class="num">${e.facts}</td><td class="num">${e.sessions}</td>
          <td class="num">${(e.dropped_facts || 0) + (e.dropped_sessions || 0) || ''}</td></tr>`).join('')}</tbody>
      </table></div>`;
  } catch (err) { handleError(err, root); }
}

/* ---------------- boot ---------------- */
async function boot() {
  if (!getToken()) { showLogin(); return; }
  try {
    state.overview = await api('/api/overview');
  } catch (err) {
    if (err instanceof AuthError) { setToken(null); showLogin('Token inválido.'); return; }
    showLogin(`Erro ao conectar: ${err.message}`);
    return;
  }
  $('#login').hidden = true;
  $('#app').hidden = false;
  let tab = 'ops';
  try { tab = localStorage.getItem('amh-dashboard-tab') || 'ops'; } catch {}
  selectTab(loaders[tab] ? tab : 'ops');
}

if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
boot();
