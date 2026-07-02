"""Test bootstrap: put the hook and adapter dirs on sys.path so tests can import them
the same way the scripts import each other at runtime."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("hooks", os.path.join("scripts", "adapters"), "scripts"):
    p = os.path.join(ROOT, sub)
    if p not in sys.path:
        sys.path.insert(0, p)
