import pytest
from src.handlers.upload import _format_status_message, FileState
from src.models import FileStatus


def test_format_status_groups_by_status():
    states = {
        "a.mp3": FileState("a.mp3", FileStatus.DOWNLOADING),
        "b.flac": FileState("b.flac", FileStatus.IMPORTED),
        "c.ogg": FileState("c.ogg", FileStatus.TAGGING),
        "d.mp3": FileState("d.mp3", FileStatus.IMPORTED, note="replaced MP3 192kbps"),
    }
    msg = _format_status_message(states)
    assert "📥" in msg
    assert "🔍" in msg
    assert "✅" in msg
    assert "a.mp3" in msg
    assert "b.flac" in msg
    assert "replaced MP3 192kbps" in msg


def test_format_status_omits_empty_groups():
    states = {"a.mp3": FileState("a.mp3", FileStatus.IMPORTED)}
    msg = _format_status_message(states)
    assert "📥" not in msg
    assert "✅" in msg
