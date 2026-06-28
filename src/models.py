from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ConfirmationMode(str, Enum):
    OFF = "off"
    AUTO = "auto"
    ON = "on"


class FileStatus(str, Enum):
    DOWNLOADING = "downloading"
    TAGGING = "tagging"
    PENDING = "pending"
    IMPORTED = "imported"
    SKIPPED = "skipped"
    DUPLICATE = "duplicate"
    FAILED = "failed"


@dataclass
class Candidate:
    index: int
    artist: str
    title: str
    album: str
    year: int | None
    mb_track_id: str | None
    distance: float
    _match: Any = field(repr=False)


@dataclass
class TagResult:
    candidates: list[Candidate]
    recommendation: int
