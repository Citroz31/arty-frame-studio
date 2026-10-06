"""Pilote VNA PNA, simulation, sonde Touchstone et détection."""

import asyncio
import ipaddress
import math

import pytest

from arty_frame_studio.instruments import InstrumentError, ScpiInstrument
from arty_frame_studio.model import FrameConfig
from arty_frame_studio.pna import (
    FoundInstrument,
    PnaDriver,
    SimulatedPna,
    VnaError,
    discover_instruments,
)
from arty_frame_studio.probes import VnaProbe, state_filename
from arty_frame_studio.sweep import OK, SweepRunner, SweepState
from arty_frame_studio.touchstone import read_touchstone

CONFIG = FrameConfig(word=0, bit_count=12)


def run(coroutine):
    async def supervise():
        async with asyncio.timeout(10):
            return await coroutine

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(supervise())
    finally:
        loop.close()


def driver(simulated=None, channel=1, ports=2):
    simulated = simulated or SimulatedPna()
    instrument = ScpiInstrument(simulated)
    instrument.identify()
    return PnaDriver(instrument, channel, ports), simulated


# -- préparation ------------------------------------------------------------------------
def test_missing_measurements_are_created_and_existing_ones_reused():
    pna, sim = driver()
    pna.prepare()
    assert pna.names[(1, 1)] == "CH1_S11_1"  # déjà sur le canal
    assert pna.names[(2, 1)] == "AFS_S21"
    assert sorted(pna.created) == ["AFS_S12", "AFS_S21", "AFS_S22"]
    assert len(pna.frequencies) == 51 and pna.frequencies[0] == 1e9
    assert "Canal 1 · 2 port(s) · 51 point(s) de 1.000 GHz à 6.000 GHz" in pna.describe()
    assert "TRIG:SOUR IMM" in sim.commands and sim.errors == []


def test_unknown_channel_lists_the_channels_present():
    pna, _ = driver(channel=7)
    with pytest.raises(VnaError, match=r"canal 7 n'existe pas.*présents : 1, 2"):
        pna.prepare()


def test_more_ports_than_the_instrument_has_surface_its_error():
    pna, _ = driver(SimulatedPna(max_ports=2), ports=3)
    with pytest.raises(InstrumentError, match="Illegal parameter value: S13"):
        pna.prepare()


def test_driver_argument_validation():
    sim = ScpiInstrument(SimulatedPna())
    for channel, ports in ((0, 2), (201, 2), (1, 0), (1, 9), (True, 2)):
        with pytest.raises(ValueError):
            PnaDriver(sim, channel, ports)
    with pytest.raises(VnaError, match="pas préparé"):
        PnaDriver(sim, 1, 2).acquire()


# -- mesure ----------------------------------------------------------------------------
def test_one_sweep_per_acquisition_and_the_matrix_follows_the_word():
    pna, sim = driver(ports=2)
    pna.prepare()
    sim.set_word(0b000_000_000_100)
    first, problems = pna.acquire()
    sim.set_word(0b000_001_000_100)
    second, _ = pna.acquire()
    assert problems == [] and sim.sweeps == [4, 68]
    ghz = 3.5  # milieu de la bande simulée
    assert abs(first.matrix[(2, 1)][25]) == pytest.approx(10 ** (-(2 + 0.1 * ghz) / 20))
    assert first.matrix[(1, 2)] == first.matrix[(2, 1)]  # réciproque
    ratio = second.matrix[(2, 1)][25] / first.matrix[(2, 1)][25]
    assert math.degrees(math.atan2(ratio.imag, ratio.real)) == pytest.approx(5.625)
    assert any("SENS1:SWE:MODE SING;*OPC?" in command for command in sim.commands)


def test_averaging_uses_a_group_of_sweeps_and_restarts_the_average():
    pna, sim = driver(SimulatedPna(averaging=8))
    pna.prepare()
    assert pna.averages == 8 and "moyennage ×8" in pna.describe()
    before = len(sim.commands)
    pna.acquire()
    sent = sim.commands[before:]
    assert sent[:3] == ["SENS1:AVER:CLE", "SENS1:SWE:GRO:COUN 8", "SENS1:SWE:MODE GRO;*OPC?"]
    assert not any("SING" in command for command in sent)


def test_a_changed_point_count_is_reported_not_silently_accepted():
    pna, sim = driver()
    pna.prepare()
    sim.frequencies = sim.frequencies[:10]  # l'utilisateur a changé les points sur le VNA
    with pytest.raises(VnaError, match="nombre de points"):
        pna.acquire()


def test_instrument_errors_during_a_sweep_are_returned_as_problems():
    pna, sim = driver()
    pna.prepare()
    sim.errors.append('-221,"Settings conflict"')
    _, problems = pna.acquire()
    assert problems == ['-221,"Settings conflict"']


def test_restore_deletes_created_measurements_and_resets_mode_and_trigger():
    pna, sim = driver()
    pna.prepare()
    pna.acquire()
    assert sim.mode == "HOLD"
    pna.restore()
    assert list(sim.measurements) == ["CH1_S11_1"] and sim.mode == "CONT"
    assert sim.trigger == "INT" and not pna.prepared
    pna.restore()  # sans effet la seconde fois


# -- sonde Touchstone ----------------------------------------------------------------------
STATES = [SweepState(0b000000000100, 1, "TX"), SweepState(0b000001000100, 0, "RX"), SweepState(7)]


def probe_for(pna, sim, folder, **options):
    return VnaProbe(pna, STATES, folder, on_word=sim.set_word, **options)


def test_state_files_are_named_by_rank_word_and_tr_level():
    assert state_filename(6, SweepState(6, 1), 12, 2) == "0007_000000000110_tr1.s2p"
    assert state_filename(0, SweepState(1), 4, 4) == "0001_0001.s4p"


def test_each_state_writes_a_touchstone_file_and_reports_the_tracked_value(tmp_path):
    pna, sim = driver()
    probe = probe_for(pna, sim, tmp_path / "campagne", track=(2, 1))
    run(probe.prepare())
    config = FrameConfig(word=STATES[0].word, bit_count=12)
    result = run(probe(config, 0))
    assert result.action == "ok" and result.file == "0001_000000000100_tr1.s2p"
    assert result.names == ("S21 (dB)", "S21 phase (°)")
    assert result.values[0] == pytest.approx(-(2 + 0.35))
    written = read_touchstone(tmp_path / "campagne" / result.file)
    assert written.ports == 2 and len(written.frequencies) == 51
    text = (tmp_path / "campagne" / result.file).read_text(encoding="utf-8")
    assert "! état 1/3 · mot 000000000100 (0x4)" in text and "! TR 1" in text
    assert "! nom TX" in text and "N5245B-SIM" in text
    run(probe.finish())
    assert sim.mode == "CONT"


def test_the_tracked_frequency_picks_the_nearest_point(tmp_path):
    pna, sim = driver()
    probe = probe_for(pna, sim, tmp_path, track=(1, 1), track_frequency=1.04e9)
    run(probe.prepare())
    result = run(probe(FrameConfig(word=4, bit_count=12), 0))
    assert result.values[0] == pytest.approx(-20.0)  # S11 = 0,1 → -20 dB
    with pytest.raises(ValueError, match="S33"):
        probe_for(pna, sim, tmp_path, track=(3, 3))
    with pytest.raises(ValueError, match="fini"):
        probe_for(pna, sim, tmp_path, track_frequency=math.inf)
    with pytest.raises(ValueError, match="Aucun état"):
        VnaProbe(pna, [], tmp_path)


def test_an_instrument_error_fails_the_state_but_keeps_its_file(tmp_path):
    pna, sim = driver()
    probe = probe_for(pna, sim, tmp_path)
    run(probe.prepare())
    sim.errors.append('-221,"Settings conflict"')
    result = run(probe(FrameConfig(word=4, bit_count=12), 0))
    assert result.action == "fail" and "Settings conflict" in result.note
    assert (tmp_path / result.file).exists()


def test_existing_files_are_not_measured_again_when_resuming(tmp_path):
    pna, sim = driver()
    first = probe_for(pna, sim, tmp_path)
    run(first.prepare())
    run(first(FrameConfig(word=STATES[0].word, bit_count=12), 0))
    sweeps = len(sim.sweeps)
    resumed = probe_for(pna, sim, tmp_path, skip_existing=True)
    result = run(resumed(FrameConfig(word=STATES[0].word, bit_count=12), 0))
    assert len(sim.sweeps) == sweeps and "Déjà mesuré" in result.note and result.action == "ok"
    assert result.values[0] == pytest.approx(-2.35)
    # Un fichier d'une autre mesure (autre nombre de points) n'est pas réutilisé.
    sim.frequencies = sim.frequencies[:20]
    pna.prepare()
    run(resumed(FrameConfig(word=STATES[0].word, bit_count=12), 0))
    assert len(sim.sweeps) == sweeps + 1
    # Sans l'option, tout est remesuré.
    run(probe_for(pna, sim, tmp_path)(FrameConfig(word=STATES[0].word, bit_count=12), 0))
    assert len(sim.sweeps) == sweeps + 2


def test_a_whole_campaign_with_tr_runs_through_the_sweep_engine(tmp_path):
    pna, sim = driver()
    probe = probe_for(pna, sim, tmp_path)
    levels = []

    async def send(config):
        sim.set_word(config.word)

    async def before(state):
        levels.append(state.tr)

    run(probe.prepare())
    runner = SweepRunner(CONFIG, STATES, send=send, before_word=before, measure=probe)
    summary = run(runner.run())
    run(probe.finish())
    assert summary.reason == "finished" and summary.counts[OK] == 3
    assert levels == [1, 0]
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "0001_000000000100_tr1.s2p",
        "0002_000001000100_tr0.s2p",
        "0003_000000000111.s2p",
    ]
    assert [result.file for result in runner.results][0] == "0001_000000000100_tr1.s2p"


# -- détection --------------------------------------------------------------------------------
IDN = {
    ("visa", "USB0::0x2A8D::0x0001::MY1::0::INSTR"): "Keysight Technologies,P9374A,MY1,A.1",
    ("visa", "TCPIP0::192.168.1.9::inst0::INSTR"): "KEYSIGHT TECHNOLOGIES,DSOX1202A,CN1,1.0",
    ("lan", "127.0.0.1"): "Keysight Technologies,N5245B,MY2,A.2",
    ("lan", "192.168.1.60"): "Keysight Technologies,N5245B,MY3,A.3",
}


def fake_identify(kind, address, timeout):
    return IDN.get((kind, address))


def test_vna_models_are_recognised_from_the_identity():
    vna = FoundInstrument("lan", "1.2.3.4", "Keysight Technologies,N5245B,MY1,A.1")
    usb = FoundInstrument("visa", "USB0::x::INSTR", "Keysight Technologies,P9374A,MY1,A.1")
    scope = FoundInstrument("lan", "1.2.3.5", "KEYSIGHT TECHNOLOGIES,DSOX1202A,CN1,1.0")
    assert vna.is_vna and usb.is_vna and not scope.is_vna
    assert not FoundInstrument("lan", "x", "garbage").is_vna
    assert vna.label.startswith("VNA · ") and scope.label.startswith("Instrument · ")


def test_discovery_lists_visa_resources_the_local_application_and_known_hosts():
    messages = []
    found = discover_instruments(
        hosts=["192.168.1.60", "192.168.1.61"],
        progress=messages.append,
        visa_resources=lambda: [key[1] for key in IDN if key[0] == "visa"],
        port_open=lambda host, timeout: host in ("127.0.0.1", "192.168.1.60"),
        identify_instrument=fake_identify,
    )
    assert [(item.kind, item.address, item.is_vna) for item in found] == [
        ("visa", "USB0::0x2A8D::0x0001::MY1::0::INSTR", True),
        ("lan", "127.0.0.1", True),
        ("lan", "192.168.1.60", True),
        ("visa", "TCPIP0::192.168.1.9::inst0::INSTR", False),  # l'oscilloscope passe après
    ]
    assert messages[0].startswith("Recherche VISA")


def test_the_network_scan_only_runs_when_asked_and_stays_on_the_local_subnets():
    probed = []

    def port_open(host, timeout):
        probed.append(host)
        return host == "192.168.1.60"

    kwargs = {
        "visa_resources": lambda: [],
        "networks": lambda: [ipaddress.IPv4Network("192.168.1.0/24")],
        "port_open": port_open,
        "identify_instrument": fake_identify,
    }
    assert discover_instruments(**kwargs) == []  # seulement ce PC
    assert probed == ["127.0.0.1"]
    probed.clear()
    found = discover_instruments(scan_network=True, **kwargs)
    assert [item.address for item in found] == ["192.168.1.60"]
    assert len(probed) == 1 + 254 and probed.count("127.0.0.1") == 1


def test_an_unresponsive_candidate_is_skipped_not_fatal():
    found = discover_instruments(
        visa_resources=lambda: ["USB0::dead::INSTR"],
        port_open=lambda host, timeout: True,
        identify_instrument=lambda kind, address, timeout: None,
    )
    assert found == []
