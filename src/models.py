from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Typed only; imported lazily so models.py stays free of the beets (and lap)
    # import chain at runtime.
    from beets.autotag.match import Recommendation


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
    length: float | None = None
    disambig: str | None = None


@dataclass
class TagResult:
    candidates: list[Candidate]
    recommendation: "Recommendation"
    # True when the candidates came from an AcoustID fingerprint match (the
    # authoritative audio identity) rather than a MusicBrainz text search. On
    # this path the candidate list is *exclusively* the fingerprint recordings,
    # so the confirmation gate prompts iff more than one survives dedup.
    fingerprinted: bool = False
