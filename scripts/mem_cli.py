#!/usr/bin/env python3
"""
Console entry point for the installed `mem` command.

Thin wrapper around the memory console (`memory.py`) so `pip install -e .` / `pipx`
can expose a global `mem`. The console resolves the hooks dir and .env relative to
its own location, so an editable install (files stay in the clone) keeps full
functionality — including `mem health`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # scripts/ on path

from memory import main  # noqa: E402


def run():
    raise SystemExit(main(sys.argv[1:]))


if __name__ == "__main__":
    run()
