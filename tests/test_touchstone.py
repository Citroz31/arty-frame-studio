"""Paramètres S et fichiers Touchstone."""

import cmath
import math

import pytest

from arty_frame_studio.touchstone import (
    SParameters,
    magnitude_db,
    phase_degrees,
    read_touchstone,
    touchstone_name,
    write_touchstone,
)

FREQUENCIES = (1e9, 2e9, 3e9)


def matrix(ports):
    return {
        (i, j): tuple(
            complex(0.1 * i + 0.01 * j + 0.001 * k, -0.2 * j + 0.003 * i - 0.0001 * k)
            for k in range(len(FREQUENCIES))
        )
        for i in range(1, ports + 1)
        for j in range(1, ports + 1)
    }


def data(ports):
    return SParameters(ports, FREQUENCIES, matrix(ports))


@pytest.mark.parametrize("ports", [1, 2, 3, 4, 5])
def test_every_port_count_round_trips_through_a_file(tmp_path, ports):
    path = write_touchstone(tmp_path / touchstone_name("etat", ports), data(ports))
    back = read_touchstone(path)
    assert back.ports == ports and back.frequencies == FREQUENCIES
    for key, trace in matrix(ports).items():
        assert back.matrix[key] == pytest.approx(trace, abs=1e-8)


def test_two_port_files_use_the_touchstone_1_0_order_s11_s21_s12_s22(tmp_path):
    values = {(1, 1): (0.1 + 0j,), (2, 1): (0.2 + 0j,), (1, 2): (0.3 + 0j,), (2, 2): (0.4 + 0j,)}
    path = write_touchstone(
        tmp_path / "a.s2p", SParameters(2, (1e9,), values), z0=50, comments=["essai", "ligne 2"]
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[:3] == ["! essai", "! ligne 2", "# HZ S RI R 50"]
    assert lines[3].split() == ["1000000000", "0.1", "0", "0.2", "0", "0.3", "0", "0.4", "0"]


def test_three_port_rows_follow_the_matrix_and_long_rows_wrap(tmp_path):
    path = write_touchstone(tmp_path / "a.s3p", data(3))
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line[0] not in "!#"]
    assert len(lines) == 3 * len(FREQUENCIES)  # trois lignes par fréquence
    assert len(lines[0].split()) == 1 + 6 and len(lines[1].split()) == 6
    wide = write_touchstone(tmp_path / "b.s5p", data(5)).read_text(encoding="utf-8")
    body = [line for line in wide.splitlines() if line[0] not in "!#"]
    assert len(body) == 5 * 2 * len(FREQUENCIES)  # 5 lignes de 5 paires : 4 + 1


def test_the_extension_must_match_the_port_count_and_no_partial_file_is_left(tmp_path):
    with pytest.raises(ValueError, match=r"\.s2p"):
        write_touchstone(tmp_path / "x.s1p", data(2))
    with pytest.raises(ValueError, match=r"\.s2p"):
        write_touchstone(tmp_path / "x.txt", data(2))
    write_touchstone(tmp_path / "dossier" / "x.s2p", data(2))
    assert [p.name for p in (tmp_path / "dossier").iterdir()] == ["x.s2p"]


def test_other_touchstone_formats_and_units_are_read(tmp_path):
    path = tmp_path / "ma.s1p"
    path.write_text("! MA, GHz\n# GHz S MA R 50\n1 0.5 90\n2 1 -45\n", encoding="utf-8")
    back = read_touchstone(path)
    assert back.frequencies == (1e9, 2e9)
    assert back.matrix[(1, 1)][0] == pytest.approx(0.5j)
    assert back.matrix[(1, 1)][1] == pytest.approx(cmath.rect(1, math.radians(-45)))
    path = tmp_path / "db.s1p"
    path.write_text("# MHZ S DB R 50\n100 -20 0\n", encoding="utf-8")
    assert read_touchstone(path).matrix[(1, 1)][0] == pytest.approx(0.1)
    path = tmp_path / "bad.s2p"
    path.write_text("# HZ S RI R 50\n1 0 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="incohérent"):
        read_touchstone(path)
    path = tmp_path / "y.s1p"
    path.write_text("# HZ Y RI R 50\n1 0 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="paramètres S"):
        read_touchstone(path)
    with pytest.raises(ValueError, match="Extension"):
        read_touchstone(tmp_path / "x.txt")


def test_incomplete_parameters_are_refused():
    with pytest.raises(ValueError, match="S21 manquant"):
        SParameters(2, FREQUENCIES, {(1, 1): (0j,) * 3, (1, 2): (0j,) * 3, (2, 2): (0j,) * 3})
    with pytest.raises(ValueError, match="point"):
        SParameters(1, FREQUENCIES, {(1, 1): (0j,)})
    with pytest.raises(ValueError):
        SParameters(0, FREQUENCIES, {})
    with pytest.raises(ValueError):
        SParameters(1, (), {(1, 1): ()})
    with pytest.raises(ValueError):
        SParameters(1, (math.nan,), {(1, 1): (0j,)})


def test_the_nearest_point_defaults_to_the_middle_of_the_band():
    parameters = data(2)
    assert parameters.at(2, 1)[0] == 2e9
    assert parameters.at(2, 1, 2.9e9)[0] == 3e9
    assert parameters.nearest_index(0) == 0


def test_magnitude_and_phase_helpers():
    assert magnitude_db(0.1) == pytest.approx(-20)
    assert magnitude_db(0) == -400.0
    assert phase_degrees(1j) == pytest.approx(90)
