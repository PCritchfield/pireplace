from dataclasses import replace

import pytest

from pireplace.state import State, StateStore

DEFAULTS = State(mood="classic-swing", music_volume=60, log_volume=40,
                 log_muted=False, music_playing=True)


def test_missing_file_returns_defaults(tmp_path):
    assert StateStore(tmp_path / "s.json", DEFAULTS).load() == DEFAULTS


def test_round_trip(tmp_path):
    store = StateStore(tmp_path / "sub" / "s.json", DEFAULTS)
    s = replace(DEFAULTS, mood="modern", music_volume=10, log_muted=True)
    store.save(s)
    assert store.load() == s


@pytest.mark.parametrize("raw", ["", "{not json", '{"music_volume": "x"}',
                                 '{"music_volume": 999}', "[]"])
def test_corrupt_file_returns_defaults(tmp_path, raw):
    p = tmp_path / "s.json"
    p.write_text(raw)
    assert StateStore(p, DEFAULTS).load() == DEFAULTS


def test_partial_file_fills_from_defaults(tmp_path):
    p = tmp_path / "s.json"
    p.write_text('{"mood": "modern"}')
    assert StateStore(p, DEFAULTS).load() == replace(DEFAULTS, mood="modern")


def test_save_is_atomic_no_temp_left(tmp_path):
    store = StateStore(tmp_path / "s.json", DEFAULTS)
    store.save(DEFAULTS)
    assert [f.name for f in tmp_path.iterdir()] == ["s.json"]
