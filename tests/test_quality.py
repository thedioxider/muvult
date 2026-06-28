from src.quality import is_better


def test_higher_bitrate_wins():
    assert is_better(320, "mp3", 192, "mp3") is True


def test_lower_bitrate_loses():
    assert is_better(192, "mp3", 320, "mp3") is False


def test_equal_bitrate_equal_format_is_not_better():
    assert is_better(320, "mp3", 320, "mp3") is False


def test_equal_bitrate_lossless_wins_over_lossy():
    assert is_better(900, "flac", 900, "mp3") is True


def test_bitrate_primary_over_format():
    assert is_better(320, "mp3", 96, "flac") is True


def test_ogg_beats_aac_same_bitrate():
    assert is_better(256, "ogg", 256, "aac") is True


def test_aac_beats_mp3_same_bitrate():
    assert is_better(256, "aac", 256, "mp3") is True
