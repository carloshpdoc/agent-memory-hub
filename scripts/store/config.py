"""
agent-memory-hub — configuration, read once for every module (the single load_env).

The .env is resolved: AGENT_MEMORY_HUB_ENV override → ~/.config/agent-memory-hub/.env →
repo root (a plain clone keeps working with no setup). A process environment variable
always wins over the file. Pure stdlib.
"""
import os

STORE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(STORE_DIR))


def env_path():
    override = os.environ.get("AGENT_MEMORY_HUB_ENV")
    if override:
        return override
    xdg = os.path.join(os.path.expanduser("~"), ".config", "agent-memory-hub", ".env")
    if os.path.exists(xdg):
        return xdg
    return os.path.join(REPO, ".env")


def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


ENV_PATH = env_path()
ENV = load_env(ENV_PATH)


def apply_ca_bundle(env, environ=os.environ):
    """SSL_CERT_FILE do .env vale para o processo todo: o OpenSSL só lê a variável de ambiente.
    Sem isso, um Python que não usa o keychain (python.org) falha em rede com inspeção TLS."""
    path = env.get("SSL_CERT_FILE")
    if not path or environ.get("SSL_CERT_FILE"):
        return
    path = os.path.expanduser(os.path.expandvars(path))
    if not os.path.isfile(path):
        raise RuntimeError(f"SSL_CERT_FILE do .env não existe: {path}")
    environ["SSL_CERT_FILE"] = path


apply_ca_bundle(ENV)


def get(key, default=None):
    """Variável de ambiente do processo, senão a do .env, senão default."""
    return os.environ.get(key) or ENV.get(key) or default
