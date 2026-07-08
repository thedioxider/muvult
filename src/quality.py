_FORMAT_RANK = {"flac": 3, "ogg": 2, "aac": 1, "mp3": 0}


def is_better(
    new_bitrate: int, new_format: str, old_bitrate: int, old_format: str
) -> bool:
    """True only when the new upload is *strictly* better than the old one.

    Higher bitrate wins; on equal bitrate a better container wins (flac > ogg >
    aac > mp3). Equal quality is not "better" -- callers keep the existing copy.
    """
    if new_bitrate != old_bitrate:
        return new_bitrate > old_bitrate
    new_rank = _FORMAT_RANK.get(new_format.lower(), -1)
    old_rank = _FORMAT_RANK.get(old_format.lower(), -1)
    return new_rank > old_rank
