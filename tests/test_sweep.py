"""Mode mesure : mots, exécution pas à pas, décisions manuelles et export."""

import asyncio
import csv

import pytest

from arty_frame_studio import sweep
from arty_frame_studio.model import FrameConfig
from arty_frame_studio.sweep import (
    ERROR,
    FAIL,
    OK,
    SKIPPED,
    Limit,
    Measurement,
    StepResult,
    SweepRunner,
)

BASE = FrameConfig(word=0, bit_count=8, divider=20, latch_ticks=8, gap_ticks=40)


def run(coroutine):
    async def supervise():
        async with asyncio.timeout(10):
            return await coroutine

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(supervise())
    finally:
        loop.close()


# -- mots -----------------------------------------------------------------------
def test_words_follow_the_entered_base_and_prefixes():
    assert sweep.parse_words("00000000\n00000001 00000010,00000011;100", 8) == [0, 1, 2, 3, 4]
    assert sweep.parse_words("0x1F 0b101 7", 8, base="dec") == [31, 5, 7]
    assert sweep.parse_words("FF 0A", 8, base="hex") == [255, 10]
    assert sweep.parse_words("0000_0001", 8) == [1]


@pytest.mark.parametrize("text", ["", "   ", "2", "0b102", "0xZZ", "100000000"])
def test_invalid_words_are_refused_with_their_text(text):
    with pytest.raises(ValueError):
        sweep.parse_words(text, 8)


def test_word_width_is_inferred_only_from_equal_length_binary_words():
    assert sweep.infer_width("000000000000000000000000\n000000000000000000000001") == 24
    assert sweep.infer_width("0001 001") is None
    assert sweep.infer_width("0x1F 0x20") is None
    assert sweep.infer_width("10 20", base="dec") is None
    assert sweep.infer_width("") is None


def test_counter_and_walking_generators():
    assert sweep.counter_words(0, 4, 1, 8) == [0, 1, 2, 3, 4]
    assert sweep.counter_words(0, 255, 64, 8) == [0, 64, 128, 192]
    assert sweep.walking_words(4) == [1, 2, 4, 8]
    assert sweep.walking_words(4, ones=False) == [0b1110, 0b1101, 0b1011, 0b0111]
    with pytest.raises(ValueError, match="maximum"):
        sweep.counter_words(0, (1 << 24) - 1, 1, 24)
    for bad in ((5, 4, 1, 8), (0, 256, 1, 8), (0, 5, 0, 8), (-1, 5, 1, 8), (0, 5, 1, 27)):
        with pytest.raises(ValueError):
            sweep.counter_words(*bad)
    assert sweep.describe_words([0, 5], 8) == "2 mot(s) de 8 bits · 00000000 → 00000101"


def test_limits_and_durations():
    limit = Limit(-3.0, -1.0)
    assert limit.check(-3.0) and limit.check(-1.0) and not limit.check(-0.9)
    assert not limit.check(float("nan")) and Limit(high=2).check(-50) and not Limit().active
    with pytest.raises(ValueError):
        Limit(2, 1)
    with pytest.raises(ValueError):
        Limit(float("inf"), None)
    assert sweep.format_duration(59) == "59 s"
    assert sweep.format_duration(125) == "2 min 05 s"
    assert sweep.format_duration(3725) == "1 h 02 min"
    assert sweep.format_duration(float("nan")) == "—"


# -- exécution --------------------------------------------------------------------
class Bench:
    """Envoi et mesure simulés : enregistre l'ordre des opérations."""

    def __init__(self, values=None):
        self.events = []
        self.values = values or {}

    async def send(self, config):
        self.events.append(("send", config.word, config.bit_count))

    async def measure(self, config, index):
        self.events.append(("measure", config.word))
        return Measurement((self.values.get(config.word, 1.0),), ("niveau (V)",))


def runner(bench, words, **options):
    results = []
    steps = []
    states = []
    instance = SweepRunner(
        BASE,
        words,
        send=bench.send,
        measure=options.pop("measure", bench.measure),
        on_result=results.append,
        on_step=lambda index, word: steps.append((index, word)),
        on_state=states.append,
        **options,
    )
    return instance, results, steps, states


def test_each_word_is_sent_then_measured_in_order():
    bench = Bench()
    instance, results, steps, states = runner(bench, [0, 1, 2])
    summary = run(instance.run())
    assert bench.events == [
        ("send", 0, 8),
        ("measure", 0),
        ("send", 1, 8),
        ("measure", 1),
        ("send", 2, 8),
        ("measure", 2),
    ]
    assert [r.status for r in results] == [OK, OK, OK]
    assert steps == [(0, 0), (1, 1), (2, 2)]
    assert states == ["running", "finished"]
    assert summary.reason == "finished" and summary.counts[OK] == 3 and summary.next_index == 3
    assert results[1].word_bin == "00000001" and results[1].values == (1.0,)


def test_sending_without_a_measurement_only_steps_through_the_words():
    bench = Bench()
    instance, results, _, _ = runner(bench, [3, 4], measure=None)
    run(instance.run())
    assert [event[:2] for event in bench.events] == [("send", 3), ("send", 4)]
    assert [r.status for r in results] == [OK, OK] and results[0].values == ()


def test_limits_turn_values_into_failures_and_can_stop_the_sweep():
    bench = Bench({1: 9.0})
    instance, results, _, _ = runner(bench, [0, 1, 2], limit=Limit(0.0, 2.0))
    summary = run(instance.run())
    assert [r.status for r in results] == [OK, FAIL, OK]
    assert "9 hors de" in results[1].note and summary.counts[FAIL] == 1

    bench = Bench({1: 9.0})
    instance, results, _, states = runner(
        bench, [0, 1, 2], limit=Limit(0.0, 2.0), stop_on_fail=True
    )
    summary = run(instance.run())
    assert [r.status for r in results] == [OK, FAIL]
    assert summary.reason == "fail" and summary.next_index == 2 and states[-1] == "stopped"
    # Resuming goes on with the word after the failure.
    summary = run(instance.run(start=summary.next_index))
    assert summary.reason == "finished" and [r.index for r in instance.results] == [0, 1, 2]


def test_limit_without_any_value_fails_rather_than_passing_silently():
    async def nothing(config, index):
        return Measurement()

    instance, results, _, _ = runner(Bench(), [0], limit=Limit(0, 1), measure=nothing)
    run(instance.run())
    assert results[0].status == FAIL and "Aucune valeur" in results[0].note


def test_a_failed_send_stops_the_sweep_and_can_resume_at_the_same_word():
    bench = Bench()
    calls = {"n": 0}

    async def flaky(config):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("Liaison UART interrompue")
        await bench.send(config)

    instance = SweepRunner(BASE, [0, 1, 2], send=flaky, measure=bench.measure)
    summary = run(instance.run())
    assert summary.reason == "error" and summary.next_index == 2
    assert "mot 3" in summary.message and "Liaison UART interrompue" in summary.message
    assert instance.results[-1].status == ERROR
    summary = run(instance.run(start=summary.next_index))
    assert summary.reason == "finished" and instance.results[-1].word == 2


def test_a_failed_measurement_stops_with_the_word_and_the_reason():
    async def broken(config, index):
        raise RuntimeError("Délai dépassé")

    instance, results, _, _ = runner(Bench(), [5, 6], measure=broken)
    summary = run(instance.run())
    assert summary.reason == "error" and results[0].status == ERROR
    assert results[0].note == "Délai dépassé" and summary.next_index == 0


def test_manual_validation_waits_for_the_operator_and_honours_every_decision():
    decisions = asyncio.Queue()

    async def ask(config, index):
        return await decisions.get()

    async def scenario():
        instance, results, _, _ = runner(Bench(), [0, 1, 2, 3], measure=ask, max_retries=5)
        task = asyncio.create_task(instance.run())
        await asyncio.sleep(0.01)
        assert not results  # waiting for the operator
        for decision in (
            Measurement(action="retry"),  # same word sent again
            Measurement(note="ok pour moi"),
            Measurement(action="fail", note="écart de phase"),
            Measurement(action="skip"),
            Measurement(),
        ):
            await decisions.put(decision)
            await asyncio.sleep(0.01)
        return await task, results, instance

    summary, results, instance = run(scenario())
    assert [r.status for r in results] == [OK, FAIL, SKIPPED, OK]
    assert results[0].note == "ok pour moi" and results[1].note == "écart de phase"
    assert summary.reason == "finished"


def test_retries_are_bounded():
    async def always_retry(config, index):
        return Measurement(action="retry")

    instance, results, _, _ = runner(Bench(), [0], measure=always_retry, max_retries=2)
    run(instance.run())
    assert results[0].status == FAIL and "Abandon après 2 renvois" in results[0].note


def test_stop_interrupts_a_pending_validation_and_skip_moves_on():
    async def never(config, index):
        await asyncio.sleep(60)

    async def scenario():
        instance, results, _, states = runner(Bench(), [0, 1, 2], measure=never)
        task = asyncio.create_task(instance.run())
        await asyncio.sleep(0.01)
        instance.skip()
        await asyncio.sleep(0.02)
        instance.stop()
        return await task, results, states

    summary, results, states = run(scenario())
    assert results[0].status == SKIPPED and summary.reason == "stopped"
    assert summary.next_index == 1 and states[-1] == "stopped"


def test_pause_takes_effect_between_words_and_resume_continues():
    async def scenario():
        gate = asyncio.Event()

        async def slow(config, index):
            if index == 0:
                await gate.wait()
            return Measurement((float(index),))

        instance, results, _, states = runner(Bench(), [0, 1, 2], measure=slow)
        task = asyncio.create_task(instance.run())
        await asyncio.sleep(0.01)
        instance.pause()
        gate.set()
        await asyncio.sleep(0.05)
        paused = (list(states), len(results))
        instance.resume()
        return paused, await task, results

    (states_while_paused, done_while_paused), summary, results = run(scenario())
    assert states_while_paused[-1] == "paused" and done_while_paused == 1
    assert summary.reason == "finished" and len(results) == 3


def test_settle_time_is_waited_between_send_and_measurement():
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    bench = Bench()
    instance, _, _, _ = runner(bench, [0, 1], settle=0.25, sleep=fake_sleep)
    run(instance.run())
    assert waits == [0.25, 0.25]
    with pytest.raises(ValueError):
        SweepRunner(BASE, [0], send=bench.send, settle=-1)
    with pytest.raises(ValueError):
        SweepRunner(BASE, [], send=bench.send)


# -- export -----------------------------------------------------------------------
def test_csv_has_one_row_per_step_with_named_columns(tmp_path):
    results = [
        StepResult(0, 0, 8, OK, (-3.1, 12.0), ("S21 (dB)", "phase (°)"), "", 1_700_000_000.0),
        StepResult(1, 1, 8, FAIL, (-9.0,), ("S21 (dB)",), "hors limite", 1_700_000_001.0),
        StepResult(2, 2, 8, SKIPPED, note="Sauté", started=1_700_000_002.0),
        StepResult(3, 3, 8, OK, (5.0, 6.0), (), "", 1_700_000_003.0),
    ]
    path = sweep.write_results_csv(results, tmp_path / "sub" / "mesure.csv")
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    assert rows[0][:7] == [
        "pas",
        "mot_bin",
        "mot_hex",
        "mot_dec",
        "statut",
        "S21 (dB)",
        "phase (°)",
    ]
    assert rows[1][:7] == ["1", "00000000", "0x0", "0", "OK", "-3.1", "12"]
    assert rows[2][4:8] == ["Échec", "-9", "", "hors limite"]
    assert rows[3][4] == "Sauté" and rows[3][5] == ""
    assert rows[4][5:7] == ["5", "6"]  # unnamed values keep their order
    assert rows[0][-1] == "horodatage" and len(rows) == 5


def test_value_statistics_ignore_skipped_and_error_steps():
    results = [
        StepResult(0, 0, 8, OK, (1.0,)),
        StepResult(1, 1, 8, FAIL, (3.0,)),
        StepResult(2, 2, 8, SKIPPED),
        StepResult(3, 3, 8, ERROR, (100.0,)),
    ]
    stats = sweep.value_statistics(results)
    assert stats == {"count": 2.0, "min": 1.0, "max": 3.0, "mean": 2.0}
    assert sweep.value_statistics([]) == {}
