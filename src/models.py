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


# Per-user settings live as a JSON blob on ``User.settings``. Every key's default
# is defined here so the /settings UI and the upload path never drift -- read a
# setting as ``settings.get(key, DEFAULT_SETTINGS[key])`` everywhere.
DEFAULT_SETTINGS: dict[str, Any] = {
    # Master switch: tag tracks against MusicBrainz. Off imports every file as-is.
    "tag": True,
    "confirmation": ConfirmationMode.AUTO.value,  # "auto"
    # Album-level enrichment (album, track number, disc, year, cover art).
    "enrich": False,
}


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
    # ISRC of the recording, when MusicBrainz has one. Present marks the
    # canonically-registered recording (the same signal `_dedup_matches` ranks on);
    # surfaced to the user as a bold, '®'-prefixed line in the confirmation lists.
    isrc: str | None = None
    # Combined match confidence in [0, 1], shown to the user as a percent and used
    # for ranking/thresholds. For a fingerprint candidate it is
    # (1 - beets_text_distance) * chroma_cluster_score -- chroma anchors the
    # magnitude honestly while beets' per-candidate distance ranks within the
    # cluster; for a text candidate it is just (1 - distance) (chroma factor 1).
    confidence: float = 0.0


@dataclass
class TagResult:
    candidates: list[Candidate]
    recommendation: "Recommendation"
    # True when the *primary* candidates came from an AcoustID fingerprint match
    # (the authoritative audio identity) rather than a MusicBrainz text search. On
    # this path ``candidates`` is exclusively the fingerprint recordings; the full
    # text-search net is kept in ``search_candidates`` for the "Show all results"
    # reveal and the OFF below-threshold fallback.
    fingerprinted: bool = False
    # The full deduped, confidence-sorted text-search candidate set (``tag_item``
    # computes it every time; we no longer discard it). When not fingerprinted this
    # is the same list as ``candidates``.
    search_candidates: list[Candidate] = field(default_factory=list)
    # beets' recommendation over the text search (the real one -- ``recommendation``
    # is forced strong on the fingerprint path). Drives the OFF fallback's is_high
    # gate. ``None`` is treated as below-strong.
    search_recommendation: "Recommendation | None" = None
