"""Every `from <local module> import names` (top level or inside a function) must name
something the module actually defines. Lazy imports inside functions only fail when that
code path runs: the nightly once imported load_env from capture_session after it moved to
store.config, and no other test reached that line."""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRS = [os.path.join(ROOT, d) for d in ("hooks", "scripts", os.path.join("scripts", "adapters"), "dashboard")]


def _local_modules():
    mods = {}
    for d in DIRS:
        for f in os.listdir(d):
            if f.endswith(".py"):
                mods[f[:-3]] = os.path.join(d, f)
            elif os.path.isfile(os.path.join(d, f, "__init__.py")):
                for sub in os.listdir(os.path.join(d, f)):
                    if sub.endswith(".py"):
                        name = f if sub == "__init__.py" else f"{f}.{sub[:-3]}"
                        mods[name] = os.path.join(d, f, sub)
    return mods


def _defined(path):
    """Top-level names of a module: defs, classes, assignments, imports."""
    names = set()
    for node in ast.parse(open(path).read()).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.If, ast.Try)):   # imports/defs condicionais
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                    names.update((a.asname or a.name).split(".")[0] for a in sub.names)
    return names


def test_local_from_imports_resolve():
    mods = _local_modules()
    defined = {m: _defined(p) for m, p in mods.items()}
    missing = []
    for path in set(mods.values()) | {os.path.join(ROOT, "tests", f) for f in os.listdir(os.path.join(ROOT, "tests"))
                                       if f.endswith(".py")}:
        for node in ast.walk(ast.parse(open(path).read())):
            if not isinstance(node, ast.ImportFrom) or node.level or node.module not in mods:
                continue
            for alias in node.names:
                if alias.name != "*" and alias.name not in defined[node.module] \
                        and f"{node.module}.{alias.name}" not in mods:
                    missing.append(f"{os.path.relpath(path, ROOT)}:{node.lineno} {node.module}.{alias.name}")
    assert not missing, "imports de nomes que não existem:\n" + "\n".join(missing)
