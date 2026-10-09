"""Unit tests for the marked-block sync used to write rules into Codex's AGENTS.md."""
import pytest

import apply_profile_rules as ar

BODY = "# Regras\n\n- regra um\n"


def test_upsert_block_into_empty_file():
    out = ar.upsert_block("", BODY)
    assert out == f"{ar.BLOCK_START}\n# Regras\n\n- regra um\n{ar.BLOCK_END}\n"


def test_upsert_block_appends_and_keeps_handwritten_text():
    hand = "# Minhas instruções\n\nUse pytest.\n"
    out = ar.upsert_block(hand, BODY)
    assert out.startswith(hand.rstrip() + "\n\n" + ar.BLOCK_START)
    assert out.endswith(ar.BLOCK_END + "\n")


def test_upsert_block_replaces_only_the_block():
    first = ar.upsert_block("antes\n", BODY) + "depois\n"
    out = ar.upsert_block(first, "- regra nova\n")
    assert "regra um" not in out and "- regra nova" in out
    assert out.startswith("antes\n") and out.endswith("depois\n")
    assert out.count(ar.BLOCK_START) == 1


def test_upsert_block_is_idempotent():
    once = ar.upsert_block("x\n", BODY)
    assert ar.upsert_block(once, BODY) == once


def test_upsert_block_refuses_broken_markers():
    with pytest.raises(ValueError):
        ar.upsert_block(f"{ar.BLOCK_START}\nsem fim\n", BODY)
