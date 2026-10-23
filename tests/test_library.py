import pytest

from pireplace.library import find_moods, find_videos, mood_label


def touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")


def test_nested_album_folders_and_junk(tmp_path):
    m = tmp_path / "music"
    touch(m / "classic-swing/Frank Sinatra/Christmas Songs/01 Jingle Bells.flac")
    touch(m / "classic-swing/Frank Sinatra/Christmas Songs/cover.jpg")
    touch(m / "classic-swing/.DS_Store")
    touch(m / "classic-swing/._01 Jingle Bells.flac")
    touch(m / "classic-swing/Thumbs.db")
    touch(m / "modern/Bublé – It's Beginning to Look.MP3")
    touch(m / "empty-mood/notes.txt")
    touch(m / ".hidden-mood/a.mp3")
    moods = find_moods(m)
    assert list(moods) == ["classic-swing", "modern"]
    assert [p.name for p in moods["classic-swing"]] == ["01 Jingle Bells.flac"]
    assert moods["modern"][0].name == "Bublé – It's Beginning to Look.MP3"


def test_loose_files_in_music_root_are_not_a_mood(tmp_path):
    touch(tmp_path / "stray.mp3")
    assert find_moods(tmp_path) == {}


def test_missing_dirs(tmp_path):
    assert find_moods(tmp_path / "nope") == {}
    assert find_videos(tmp_path / "nope") == []


def test_videos_sorted_and_filtered(tmp_path):
    for n in ["b.mp4", "a.MKV", "readme.txt", ".c.mp4", "sub/d.webm"]:
        touch(tmp_path / n)
    assert [p.name for p in find_videos(tmp_path)] == ["a.MKV", "b.mp4", "d.webm"]


@pytest.mark.parametrize("name,label", [("classic-swing", "Classic Swing"),
                                        ("modern", "Modern"),
                                        ("kids_songs", "Kids Songs")])
def test_mood_label(name, label):
    assert mood_label(name) == label
