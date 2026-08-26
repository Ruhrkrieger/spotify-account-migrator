# Changelog

## [1.0.0] - 2026-08-26

First public release.

### The February 2026 Web API migration

Spotify replaced a set of endpoints in February 2026. Most existing migration
scripts — and `spotipy`'s own helper methods — still call the old ones, which
is why they fail today, sometimes *silently*:

| Operation | Removed | Current | Symptom if you call the old one |
|---|---|---|---|
| Read playlist tracks | `GET /playlists/{id}/tracks` | `GET /playlists/{id}/items` | **200 with an empty list** — every playlist looks empty |
| Create a playlist | `POST /users/{id}/playlists` | `POST /me/playlists` | `403 Forbidden` |
| Add tracks | `POST /playlists/{id}/tracks` | `POST /playlists/{id}/items` | `403 Forbidden` |
| Save tracks / albums | `PUT /me/tracks`, `PUT /me/albums` | `PUT /me/library?uris=...` | `403 Forbidden` |
| Follow artists | `PUT /me/following` | `PUT /me/library?uris=...` | deprecated, still served |
| Follow a playlist | `PUT /playlists/{id}/followers` | `PUT /me/library?uris=...` | deprecated, still served |

Two consequences worth knowing:

- The empty-list-instead-of-an-error behaviour on playlist reads is the nasty
  one. An export can "succeed" while collecting nothing at all. This tool
  cross-checks the number of tracks Spotify advertises against what it
  actually retrieved, and refuses to import an export with zero tracks.
- `GET /playlists/{id}/items` caps `limit` at **50** (the old endpoint allowed
  100), and `PUT /me/library` caps at **40 URIs** per call.

Read endpoints (`GET /me/playlists`, `/me/tracks`, `/me/albums`,
`/me/following`) were left untouched.

### Added
- Export / import of own playlists, followed playlists, liked songs, saved
  albums and followed artists.
- Resumable: progress is written to `migration_state.json` after each batch,
  so an interrupted run picks up exactly where it stopped without duplicates.
- Every write tries the current endpoint first and falls back to the legacy
  one, so the tool works whichever API shape is being served.
- Interactive menu, for people who have never opened a terminal.
- `run.bat` / `run.sh` launchers that check Python and install dependencies.
