"""The dashboard deploy copies a fixed list of modules into lib/. A module imported at top
level by any of them but missing from the list crashes the service on start (this happened
when recall_session started importing handoff)."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRS = (os.path.join(ROOT, "hooks"), os.path.join(ROOT, "scripts"))
LOCAL = {os.path.splitext(f)[0]: os.path.join(d, f) for d in DIRS for f in os.listdir(d) if f.endswith(".py")}
# pacotes (ex.: scripts/store): o import é pelo nome da pasta; a fonte é o __init__.py
LOCAL.update({f: os.path.join(d, f, "__init__.py") for d in DIRS for f in os.listdir(d)
              if os.path.isfile(os.path.join(d, f, "__init__.py"))})
TOP_IMPORT = re.compile(r"^(?:from\s+(\w+)\s+import|import\s+(\w+))", re.MULTILINE)


def bundled():
    text = open(os.path.join(ROOT, "dashboard", "deploy.sh")).read().replace("\\\n", " ")
    line = next(ln for ln in text.splitlines() if ln.startswith("cp ") and "lib/" in ln)
    return {os.path.splitext(os.path.basename(p))[0] for p in re.findall(r'"\$REPO/([\w/]+(?:\.py)?)"', line)}


def test_bundle_includes_store_package():
    assert "store" in bundled()


def test_bundle_covers_top_level_local_imports():
    mods = bundled()
    for m in list(mods):
        src = open(LOCAL[m]).read()
        for a, b in TOP_IMPORT.findall(src):
            dep = a or b
            if dep in LOCAL:
                assert dep in mods, f"{m} importa {dep}, que falta no bundle do deploy.sh"
