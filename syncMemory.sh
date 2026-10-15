#!/bin/sh
# Sync de skills deste Mac com o hub (instala o que foi aprovado no dashboard).
cd "$(dirname "$0")" && /usr/bin/python3 scripts/memory.py sync
