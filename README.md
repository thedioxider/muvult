# muvult

A Telegram bot that ingests audio files, tags them against MusicBrainz, stores
one deduplicated copy per track, and exposes a personal music library to each
user through [Navidrome](https://www.navidrome.org/).

Send the bot an audio file (or an album's worth at once); it identifies the
recording, files a single canonical copy into a shared pool, and links that copy
into your library. If two users upload the same track, it is stored once and
shared. If someone later uploads a higher-quality copy, the pool file is
upgraded in place and everyone's library follows.

## How it works

A file uploaded over Telegram is downloaded to a staging area, identified by
audio fingerprint (with a MusicBrainz text search as fallback, through beets),
then filed into a shared pool and linked into the uploader's library:

- **Pool** -- `<music root>/.pool/` holds exactly one file per track, named
  `$albumartist/$album/$title`, with a track disambiguation folded in when present
  (`.../arrow (live)`) so a distinct recording of the same track gets its own
  location. This is the only real copy. The disambiguation is also appended to the
  file's **title tag** (`Aerials (live, ...)`), so a live or alternate take is
  distinguishable in Navidrome instead of showing a bare title identical to the
  studio recording.
- **Libraries** -- each user has a directory `<music root>/<username>/`
  containing _symlinks_ into the pool. Navidrome scans these per-user dirs, one
  library per user (its path is `ND_MUSIC_PATH/<username>`).
- **Deduplication** -- a `Track` row is keyed by MusicBrainz recording id (or by
  pool path for as-is imports). Uploading a track someone already owns just adds
  an ownership symlink; no second copy is made.
- **Quality upgrades** -- when a new upload of an existing track is at least as
  good (higher bitrate; ties broken by format rank `flac > ogg > aac > mp3`,
  equal quality still replacing so a re-upload refreshes tags), the pool file is
  replaced and every owner's symlink is repointed. Only a strictly worse upload
  is dropped.
- **As-is import** -- if MusicBrainz has no match (or the user declines), the
  file is stored unmodified under `.pool/users/<username>/` and linked flatly
  into the library.

## Tagging

muvult drives [beets](https://beets.io/) for identification, using two candidate
sources: an audio fingerprint (primary) and a MusicBrainz text search (fallback).

### Fingerprint first, text search as fallback

Every upload is first fingerprinted with [AcoustID](https://acoustid.org/), which
identifies the actual recording from the audio itself, independent of the file's
existing tags. A fingerprint match is authoritative: when it hits, the candidates
are *only* those recordings. The text search below is used only when the
fingerprint finds nothing (an unknown or obscure track, or no network).

### Text search: one search, no per-candidate lookups

beets' stock MusicBrainz recording search misidentifies unicode artists and fails
on titles carrying extra tokens like `(Album Version)`. muvult patches the search
into a more robust shape (`+artist:(…) +(recording:(…) alias:(…)) release:(…)`) --
required identity fields, forgiving on description, album as an optional boost.

Those candidates come from a **single** MusicBrainz recording search. That one
response already carries everything matching and the picker need -- title, artist,
ISRC, disambiguation, and every release the recording sits on with its per-release
track length -- so muvult builds the candidates straight from it, skipping the
extra per-candidate lookup beets would otherwise make. The full, authoritative
metadata for the track you actually import is fetched once, by id, at import time.

A recording's own stored length can differ from the length of the *track* on the
release your file came from. Before scoring, muvult corrects each candidate's
length to the nearest per-release track length, so the match distance, the shown
confidence, and the auto-import recommendation all reflect the real duration --
and a duplicate recording can't win just because its bare length happens to match.

### Candidate deduplication

MusicBrainz frequently holds several recording entities for one performance --
typically one per release of the same album -- differing only in trivia: a length
delta, an ISRC, or which release they hang off. Presented raw, the candidate list
shows the same song several times, indistinguishable to the user. muvult collapses
these: candidates sharing artist, title, and disambiguation are grouped, and one
representative is kept. The survivor is chosen by, in order, carrying an ISRC (the
canonically-registered, usually worldwide recording of the same track), then best
beets match (lowest corrected distance, i.e. closest length), then lowest recording
id so the choice is deterministic across uploads. This applies to every match,
including strong ones, so even auto-imports pick the canonical recording.

### Album enrichment

A MusicBrainz recording carries only track-level metadata (title, artist) -- no
album, track number, disc, or year. When enabled, muvult fills these in:

- **Album, artist, year** come from the recording's *release group*, not any one
  pressing -- so a track's album stays the same whether it was matched against the
  original, a later remaster, or a foreign reissue.
- **Track and disc number** come from the specific release the recording sits on
  (a deluxe-only bonus track is numbered from the deluxe edition).
- **Cover art** is fetched from the [Cover Art Archive](https://coverartarchive.org/)
  and embedded in the file.

This reuses the by-id lookup already made at import (no extra MusicBrainz request).
Enrichment is **on by default** and can be turned off per user via `/settings`.

### Confirmation modes

Each user picks how much the bot asks before importing (via `/settings`); the
strength comes from beets' own match recommendation:

- **off** -- never ask; import the top match, or as-is when there is none.
- **auto** (default) -- ask when there is no strong match, and also when the
  match is ambiguous. For a fingerprint match, "ambiguous" means the fingerprint
  resolved to more than one distinct recording; for a text-search match, it means
  the top candidate has same-artist/title siblings (a studio version and a live
  one, a radio edit, ...). In either case the bot asks you to pick among just
  those; a genuinely unique match imports without asking.
- **on** -- always ask, even on a strong match.

## Commands

**Users**

- `/start` -- welcome / status
- `/help` -- command list
- `/id` -- show your Telegram ID (give this to an admin to be added)
- `/settings` -- choose confirmation mode
- _send audio files_ -- upload them to your library

**Admins** (Telegram IDs listed in `ADMIN_TG_IDS`)

- `/adduser <navidrome_username> <tg_id>` -- create a library and register a user
- `/removeuser <username>` -- remove a user, their library, and orphaned tracks
- `/settgid <username> <new_tg_id>` -- rebind a user to a different Telegram account
- `/setusername <old> <new>` -- rename a user (moves dir, updates symlinks)
- `/users` -- list registered users
- `/recreatelinks [username]` -- rebuild symlinks from the DB (repair tool)
- `/removetrack <path|prefix/*> [username]` -- remove tracks by pool path

## Configuration

Configuration is read from the environment (see `.env.example`):

| Variable        | Description                                                  |
| --------------- | ------------------------------------------------------------ |
| `BOT_TOKEN`     | Telegram bot token                                           |
| `ADMIN_TG_IDS`  | Comma-separated admin Telegram IDs                           |
| `ND_URL`        | Navidrome base URL (admin API)                               |
| `ND_ADMIN_USER` | Navidrome admin username                                     |
| `ND_ADMIN_PASS` | Navidrome admin password                                     |
| `ND_MUSIC_PATH` | Path prefix Navidrome uses for per-user library paths        |
| `MUSIC_ROOT`    | Library root inside the container (default `/music`)         |
| `STAGING_ROOT`  | Temp download area inside the container (default `/staging`) |
| `MB_SEARCH_LIMIT` | MusicBrainz search results fetched per lookup (default 48) |
| `TG_API_ID` / `TG_API_HASH` | Telegram app credentials (https://my.telegram.org) for the self-hosted Bot API server |
| `BOT_API_URL`   | Self-hosted Bot API server URL. Empty -> cloud API (20 MB cap) |
| `BOT_API_LOCAL` | `1` if that server runs with `--local` (files read off the shared volume) |

## Deployment

Runs as a Docker container (polling bot -- no inbound port). See
`docker-compose.yaml`:

```bash
cp .env.example .env    # fill in BOT_TOKEN, ND_ADMIN_PASS, ADMIN_TG_IDS
docker compose up -d --build
```

Volumes:

- `/data/media/muvult:/music` -- pool + per-user libraries (persistent)
- `/data/var/muvult:/data` -- SQLite databases (persistent)
- `staging:/staging` -- scratch download area (ephemeral named volume)
- `bot-api-data:/var/lib/telegram-bot-api` -- shared with the `telegram-bot-api`
  service so muvult reads locally-served files off disk

A `cleaner` sidecar deletes files older than a week from the `staging` and
`bot-api-data` volumes hourly -- the local Bot API server keeps its downloaded
originals and never prunes them on its own.

**Large files.** The cloud Bot API caps bot downloads at 20 MB, so lossless
tracks fail. The compose stack bundles a self-hosted `telegram-bot-api` server
(local mode, 2000 MB cap); muvult points at it via `BOT_API_URL`. Fill
`TG_API_ID` / `TG_API_HASH` in `.env` before deploying. To fall back to the
cloud API, leave `BOT_API_URL` empty and drop the extra service.

Navidrome runs on the host; the container reaches it via
`host.docker.internal` (mapped through `extra_hosts`). Because `/staging` and
`/music` are separate mounts, imports use `shutil.move` (copy + unlink), not a
bare rename.

## Development

```bash
pip install -e '.[dev]'
pytest
```

Tests use `pytest-asyncio` (auto mode) and `respx` to mock Navidrome's HTTP API.

## Layout

| Path                     | Responsibility                                         |
| ------------------------ | ------------------------------------------------------ |
| `src/main.py`            | Entrypoint: init DB, patch beets, start polling        |
| `src/config.py`          | Env-based settings (pydantic-settings)                 |
| `src/auth.py`            | Telegram auth middleware (admin / user / ignore)       |
| `src/db.py`              | SQLModel tables: `User`, `Track`, `TrackOwnership`     |
| `src/handlers/upload.py` | File ingest, confirmation flow, dedup, quality upgrade |
| `src/handlers/admin.py`  | Admin commands + Navidrome library management          |
| `src/handlers/user.py`   | User commands and settings                             |
| `src/beets_svc.py`       | beets setup, candidate search, tagging + staging        |
| `src/beets_patches.py`   | MusicBrainz search reshaping patch                     |
| `src/navidrome.py`       | Navidrome admin API client                             |
| `src/pool.py`            | Pool paths, symlink create/update/remove               |
| `src/quality.py`         | Bitrate/format comparison for upgrades                 |
