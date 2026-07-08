"""
Tests for `mem skills` (procedures -> SKILL.md, roadmap item 8): slug, rendering
and the write/never-overwrite behavior, with the REST layer mocked (offline).
"""
import memory as mem

ROW = {"fact": "Para fazer deploy do mysite: rodar npm run build e depois npm run deploy:apply",
       "scope": "mysite", "source_session_id": "abcd1234-9999", "valid_from": "2026-07-08T12:00:00Z"}


# ---- slug -----------------------------------------------------------------------

def test_slugify_ascii_kebab_and_skips_preamble():
    assert mem._slugify("Para fazer deploy do mysite: rodar npm") == "fazer-deploy-do-mysite-rodar-npm"


def test_slugify_strips_accents():
    assert mem._slugify("configuração de sessões") == "configuracao-de-sessoes"


def test_slugify_empty_fallback():
    assert mem._slugify("") == "procedure"
    assert mem._slugify("çãõ!!!") == "cao"


# ---- SKILL.md --------------------------------------------------------------------

def test_skill_md_has_frontmatter_body_and_provenance():
    md = mem._skill_md(ROW)
    assert md.startswith("---\n")
    assert "name: fazer-deploy-do-mysite" in md
    assert 'description: "Para fazer deploy do mysite' in md  # aspas: ':' quebraria o YAML
    assert "npm run deploy:apply" in md            # corpo completo
    assert "projeto mysite" in md and "sessão abcd1234" in md  # proveniência
    assert "edite à vontade" in md                 # a skill passa a ser do humano


def test_skill_md_title_stops_at_colon():
    md = mem._skill_md(ROW)
    assert "# Para fazer deploy do mysite\n" in md


# ---- cmd_skills: dry-run, write, nunca sobrescreve -----------------------------------

def _patch_rest(monkeypatch, rows):
    monkeypatch.setattr(mem, "rest", lambda path: rows)


def test_skills_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    _patch_rest(monkeypatch, [ROW])
    mem.cmd_skills([str(tmp_path)])
    assert list(tmp_path.iterdir()) == []          # dry-run: zero arquivos
    assert "--write" in capsys.readouterr().out    # e ensina o proximo passo


def test_skills_write_creates_skill(tmp_path, monkeypatch, capsys):
    _patch_rest(monkeypatch, [ROW])
    mem.cmd_skills([str(tmp_path), "--write"])
    p = tmp_path / "fazer-deploy-do-mysite-rodar-npm" / "SKILL.md"
    assert p.exists()
    assert "name: fazer-deploy-do-mysite" in p.read_text()


def test_skills_never_overwrites_existing(tmp_path, monkeypatch):
    _patch_rest(monkeypatch, [ROW])
    mem.cmd_skills([str(tmp_path), "--write"])
    p = tmp_path / "fazer-deploy-do-mysite-rodar-npm" / "SKILL.md"
    p.write_text("EDITADO PELO HUMANO")
    mem.cmd_skills([str(tmp_path), "--write"])     # segunda passada
    assert p.read_text() == "EDITADO PELO HUMANO"  # edicao manual preservada


def test_skills_duplicate_slugs_get_suffix(tmp_path, monkeypatch):
    rows = [ROW, {**ROW, "fact": "Para fazer deploy do mysite: rodar npm (variante nova)"}]
    _patch_rest(monkeypatch, rows)
    mem.cmd_skills([str(tmp_path), "--write"])
    dirs = sorted(d.name for d in tmp_path.iterdir())
    assert dirs == ["fazer-deploy-do-mysite-rodar-npm", "fazer-deploy-do-mysite-rodar-npm-2"]


def test_skills_no_procedures_message(tmp_path, monkeypatch, capsys):
    _patch_rest(monkeypatch, [])
    mem.cmd_skills([str(tmp_path)])
    assert "nenhum fato 'procedure'" in capsys.readouterr().out


# ---- mem help: todo comando documentado ---------------------------------------------

def test_help_documents_every_command():
    documented = {name for _sec, cmds in mem.HELP_SECTIONS for name, _a, _d in cmds}
    registered = set(mem.COMMANDS) - {"help"}
    assert registered == documented, f"faltando no help: {registered - documented}"


def test_help_prints_sections(capsys):
    mem.cmd_help([])
    out = capsys.readouterr().out
    assert "consulta" in out and "curadoria" in out and "jobs" in out
    assert "extract_facts.py" in out  # os scripts fora do console tambem aparecem
