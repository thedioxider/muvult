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

A file uploaded over Telegram is downloaded to a staging area, identified via
MusicBrainz (through beets), then filed into a shared pool and linked into the
uploader's library:

- **Pool** -- `<music root>/.pool/` holds exactly one file per track, named
  `$albumartist/$album/$track - $title`. This is the only real copy.
- **Libraries** -- each user has a directory `<music root>/<username>/`
  containing _symlinks_ into the pool. Navidrome scans these per-user dirs, one
  library per user (its path is `ND_MUSIC_PATH/<username>`).
- **Deduplication** -- a `Track` row is keyed by MusicBrainz recording id (or by
  pool path for as-is imports). Uploading a track someone already owns just adds
  an ownership symlink; no second copy is made.
- **Quality upgrades** -- when a new upload of an existing track is better
  (higher bitrate; ties broken by format rank `flac > ogg > aac > mp3`), the
  pool file is replaced and every owner's symlink is repointed.
- **As-is import** -- if MusicBrainz has no match (or the user declines), the
  file is stored unmodified under `.pool/users/<username>/` and linked flatly
  into the library.

## Tagging

muvult drives [beets](https://beets.io/) for MusicBrainz lookup. beets' stock
MusicBrainz recording search misidentifies unicode artists and fails on titles
carrying extra tokens like `(Album Version)`. muvult patches the search into a
more robust shape (`+artist:(…) +(recording:(…) alias:(…)) release:(…)`) --
required identity fields, forgiving on description, album as an optional boost.

### Candidate deduplication

MusicBrainz frequently holds several recording entities for one performance --
typically one per release of the same album -- differing only in trivia: a
sub-second length delta, an ISRC, or which release they hang off. Presented
raw, the candidate list shows the same song several times, indistinguishable to
the user. muvult collapses these: candidates sharing artist, title, and
displayed duration (whole seconds) are grouped, and one representative is kept.
The survivor is chosen by, in order, best beets match (lowest distance), then
carrying an ISRC (the canonically-registered, usually worldwide recording), then
lowest recording id so the choice is deterministic across uploads. This applies
to every match, including strong ones, so even auto-imports pick the canonical
recording.

### Release enrichment

A MusicBrainz recording carries only track-level metadata (title, artist) -- it
is not tied to any release, so it has no album, track number, disc, or year. When
enabled, once a candidate is chosen muvult resolves it to a specific release and
pulls full album metadata from there. The release is picked, in order, by:
official status; a primary artist matching the track's (over "Various Artists"
compilations); a studio album (no secondary types, with album over EP over
single); a worldwide release; earliest date; then lowest release id. The year
comes from the recording's earliest official release. If any of this fails the
track still imports, with recording-level tags only.

This costs an extra MusicBrainz lookup per track (rate-limited to 1/s), so it is
**off by default** and opt-in per user via `/settings` -- with it off, tracks are
tagged with title and artist only (any album/track tags already on the file are
kept) and import faster.

### Confirmation modes

Each user picks how much the bot asks before importing (via `/settings`); the
strength comes from beets' own match recommendation:

- **off** -- never ask; import the top match, or as-is when there is none.
- **auto** (default) -- ask only when there is no strong match.
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
| `MB_SEARCH_LIMIT` | MusicBrainz search results fetched per lookup (default 8)  |

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
- `/tmp/muvult:/staging` -- scratch download area (ephemeral)

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
| `src/beets_svc.py`       | beets setup, candidate search, apply/move, as-is move  |
| `src/beets_patches.py`   | MusicBrainz search reshaping patch                     |
| `src/navidrome.py`       | Navidrome admin API client                             |
| `src/pool.py`            | Pool paths, symlink create/update/remove               |
| `src/quality.py`         | Bitrate/format comparison for upgrades                 |
