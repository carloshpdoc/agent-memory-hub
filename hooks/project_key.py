"""
Canonical project key for a working directory.

Why: the basename of the cwd is a bad project key. The same repo cloned three times
(travel-app, TravelAppiOS, travel-app-new) became three projects, and sessions opened in
`/`, `~` or `~/Development` became fake projects ("root", "Development") whose facts
were then injected as if they belonged to the current project.

Resolution order:
  1. Inside a git repo: the repo name from `remote.origin.url` (clones of the same
     repo share one key). No remote: the name of the repo's top-level dir.
  2. Not a git repo, but under a workspace root (default ~/Development): the first
     path segment below it (~/Development/financial/Empresa -> "financial").
  3. Anything else (/, ~, a workspace root itself, /tmp, scratchpads, Downloads):
     None — the session has no project and its facts are never auto-injected.
Then PROJECT_ALIASES (glob=key pairs) are applied, and the key is lowercased.

Config (env or .env): WORKSPACE_ROOTS (colon-separated, default ~/Development),
PROJECT_ALIASES (e.g. "shop-app-clone*=shop-app,foo=bar").
Pure stdlib; git is called with a short timeout and failures fall back to path rules.
"""
import fnmatch
import os
import re
import subprocess

GIT_TIMEOUT = 2
# Scope for facts from sessions without a project: searchable, never auto-injected.
NO_PROJECT_SCOPE = "_none"
_OTHER_HOME_RE = re.compile(r"^/(?:Users|home)/[^/]+(/.*)?$")


def _git(cwd, *args):
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                           timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def repo_name_from_remote(url):
    """git@github.com:owner/Repo.git | https://host/owner/Repo(.git)(/) -> 'Repo'."""
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def parse_aliases(spec):
    pairs = []
    for item in (spec or "").split(","):
        if "=" in item:
            pattern, key = item.split("=", 1)
            if pattern.strip() and key.strip():
                pairs.append((pattern.strip().lower(), key.strip().lower()))
    return pairs


def _workspace_roots(spec):
    raw = spec or "~/Development"
    return [os.path.normpath(os.path.expanduser(r)) for r in raw.split(":") if r.strip()]


def _local_equivalent(path):
    """/Users/<other>/X -> ~/X, so paths captured on another machine can be resolved here."""
    m = _OTHER_HOME_RE.match(path)
    if not m:
        return path
    return os.path.normpath(os.path.expanduser("~") + (m.group(1) or ""))


def _raw_key(cwd, roots):
    path = os.path.normpath(os.path.expanduser(cwd))
    local = _local_equivalent(path)
    # a subdir that only exists on the other machine: climb to the nearest existing
    # ancestor, but never above a workspace root (that would resolve to nothing useful)
    while (not os.path.isdir(local)
           and any(local.startswith(r + os.sep) for r in roots)
           and os.path.dirname(local) not in roots):
        local = os.path.dirname(local)
    if os.path.isdir(local):
        top = _git(local, "rev-parse", "--show-toplevel")
        # a dotfiles repo in ~ (or a repo at a workspace root) would swallow every dir
        not_a_project = {os.path.expanduser("~"), "/", *roots}
        if top and os.path.normpath(top) not in not_a_project:
            remote = _git(top, "config", "--get", "remote.origin.url")
            return repo_name_from_remote(remote) if remote else os.path.basename(top)
    for root in roots:
        # compare with the home part normalised so other machines' paths match too
        for candidate in (local, path):
            if candidate.startswith(root + os.sep):
                first = candidate[len(root) + 1:].split(os.sep, 1)[0]
                if first and not first.startswith("."):
                    return first
    return None


def normalise_key(name, aliases=()):
    """Lowercase, spaces to hyphens, owner/ prefix dropped, then aliases."""
    if not name:
        return None
    key = name.strip().rsplit("/", 1)[-1].lower()
    key = re.sub(r"\s+", "-", key)
    if not key or key in (".", ".."):
        return None
    for pattern, target in aliases:
        if fnmatch.fnmatchcase(key, pattern):
            return target
    return key


def project_key(cwd, env=None):
    """Canonical project key for cwd, or None when the cwd is not a project."""
    if not cwd:
        return None
    env = env if env is not None else os.environ
    roots = _workspace_roots(env.get("WORKSPACE_ROOTS"))
    aliases = parse_aliases(env.get("PROJECT_ALIASES"))
    return normalise_key(_raw_key(cwd, roots), aliases)


def fact_scope(llm_scope, session_project, known_keys, aliases=()):
    """Scope for an extracted fact.

    The LLM's scope wins only when it names a known project (it often refines a session
    opened in a generic dir, e.g. "swiftlang/swift" from a session in `/`). Otherwise the
    session's project; sessions without a project give NO_PROJECT_SCOPE, never global."""
    candidate = normalise_key(llm_scope, aliases) if isinstance(llm_scope, str) else None
    if candidate and candidate in known_keys:
        return candidate
    return session_project or NO_PROJECT_SCOPE
