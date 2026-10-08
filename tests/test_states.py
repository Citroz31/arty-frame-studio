"""Liste d'états : mot, niveau de TR, nom."""

import pytest

from arty_frame_studio.states import (
    MAX_STATES,
    describe_states,
    infer_states_width,
    load_states_text,
    parse_states,
)
from arty_frame_studio.sweep import SweepState

EXAMPLE = """\
# mot ; TR ; nom
mot;TR;nom
00000000 ; TX ; référence
00000001 ; RX
0x0A
00000011, 1, haut, avec virgule
00000100\tRX\tonglet
"""


def test_states_carry_word_tr_and_name_in_any_separator():
    states = parse_states(EXAMPLE, 8)
    assert states == [
        SweepState(0, 1, "référence"),
        SweepState(1, 0),
        SweepState(10),
        SweepState(3, 1, "haut, avec virgule"),
        SweepState(4, 0, "onglet"),
    ]


def test_tx_level_chooses_which_pin_level_means_transmit():
    text = "01 ; TX\n10 ; RX\n11 ; 1\n00 ; 0"
    high = parse_states(text, 2)
    low = parse_states(text, 2, tx_level=0)
    assert [state.tr for state in high] == [1, 0, 1, 0]
    assert [state.tr for state in low] == [0, 1, 1, 0]  # 1 et 0 restent des niveaux


def test_whitespace_separated_columns_and_other_bases():
    states = parse_states("FF TX nom avec espaces\n0A", 8, base="hex")
    assert states == [SweepState(255, 1, "nom avec espaces"), SweepState(10)]
    assert parse_states("200", 8, base="dec") == [SweepState(200)]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "Aucun état"),
        ("# rien\n\n", "Aucun état"),
        ("0001\n0010 ; HAUT", "Ligne 2 : TR « HAUT »"),
        ("0001\n\n2", "Ligne 3"),
        ("111111111", "dépasse la largeur de 8 bits"),
    ],
)
def test_invalid_lines_name_their_line_number(text, message):
    with pytest.raises(ValueError, match=message):
        parse_states(text, 8)


def test_at_most_4096_states():
    parse_states("\n".join(f"{n:012b}" for n in range(MAX_STATES)), 12)
    with pytest.raises(ValueError, match="4096"):
        parse_states("\n".join(f"{n:013b}" for n in range(MAX_STATES + 1)), 13)
    with pytest.raises(ValueError):
        parse_states("1", 27)
    with pytest.raises(ValueError):
        parse_states("1", 8, tx_level=2)
    with pytest.raises(ValueError):
        parse_states("1", 8, base="oct")


def test_width_is_inferred_from_binary_words_of_equal_length():
    assert infer_states_width("# tête\n000000000000000000000000;TX\n000000000000000000000001") == 24
    assert infer_states_width("0001\n001") is None
    assert infer_states_width("0001\n0x3") is None
    assert infer_states_width("") is None


def test_files_are_read_with_or_without_a_spreadsheet_bom(tmp_path):
    path = tmp_path / "etats.csv"
    path.write_bytes("﻿mot;TR\n0101;TX\n".encode())
    assert parse_states(load_states_text(path), 4) == [SweepState(5, 1)]
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ValueError, match="UTF-8"):
        load_states_text(path)
    path.write_bytes(b"0" * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="volumineux"):
        load_states_text(path)


def test_description_counts_transmit_and_receive_states():
    states = parse_states("0001;TX\n0010;RX\n0011;RX\n0100", 4)
    assert describe_states(states, 4) == (
        "4 état(s) de 4 bits · 0001 → 0100 · TR : 1 TX, 2 RX, 1 sans TR"
    )
    assert describe_states([SweepState(1)], 4) == "1 état(s) de 4 bits · 0001 → 0001"
    assert describe_states([], 4) == "Aucun état."
