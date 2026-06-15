#!/usr/bin/env bash
# Illustrative demo (generic data, not a real run) of the profile -> rules loop.
# Used only to record docs/profile-demo.gif for the README. Safe to run anywhere.
clear
G=$'\033[32m'; Y=$'\033[33m'; D=$'\033[2m'; C=$'\033[36m'; B=$'\033[1m'; R=$'\033[0m'
p() { printf "%s\n" "$1"; }
prompt() { printf "${D}\$ ${R}${B}%s${R}\n" "$1"; sleep 0.5; }

prompt "python3 scripts/synthesize_profile.py"
sleep 0.6
p "${D}[ollama]${R} 6 patterns proposed from 84 facts across 7 projects"
p "${D}profile: PROFILE.md  ·  review with: memory.py profile${R}"
sleep 1.3
echo

prompt "python3 scripts/memory.py profile"
sleep 0.4
p "${Y}?${R} ${D}a1b2c3d4${R} ${D}(tooling_habit)${R} ${G}0.95${R}  Prefers the gh CLI over the GitHub MCP"
p "     ${D}· project-a, project-b, project-c${R}"
p "     ${C}-> Always use the gh CLI for GitHub operations (pr view/diff, api).${R}"
p "${Y}?${R} ${D}e5f6a7b8${R} ${D}(preference)${R} ${G}0.92${R}  Review comments inline only, never the PR body"
p "     ${D}· project-a, project-d${R}"
p "     ${C}-> Post PR review comments inline only, unless explicitly asked.${R}"
sleep 1.8
echo

prompt "python3 scripts/memory.py profile approve a1b2c3d4"
sleep 0.4
p "a1b2c3d4 ${G}-> approved${R}"
prompt "python3 scripts/memory.py profile approve e5f6a7b8"
sleep 0.4
p "e5f6a7b8 ${G}-> approved${R}"
sleep 0.9
echo

prompt "python3 scripts/apply_profile_rules.py --write"
sleep 0.5
p "wrote 2 rule(s) to ${B}~/.claude/profile-rules.md${R}"
p "${D}loaded in every session via '@profile-rules.md' in CLAUDE.md${R}"
sleep 1.3
echo

prompt "cat ~/.claude/profile-rules.md"
sleep 0.4
p "${D}# My profile rules (derived from my own history)${R}"
p "- Always use the gh CLI for GitHub operations (pr view/diff, api)."
p "- Post PR review comments inline only, unless explicitly asked."
sleep 2.2
