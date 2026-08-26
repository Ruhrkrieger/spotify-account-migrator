# Spotify Account Migrator

Copy an entire Spotify library — playlists, liked songs, saved albums,
followed artists — from one account to another. Free, no track limit, no
third-party service ever sees your data.

Built against the Spotify Web API **as it stands after the February 2026
migration**.

```
==============================================
   Spotify account migrator
==============================================

  1. EXPORT  - sign in to the OLD account
  2. IMPORT  - sign in to the NEW account
  3. Show progress
  4. Reset (start over)
  5. Quit

Your choice [1-5]: 1

>>> A Spotify page will open. Sign in with the OLD account.
Signed in as: your-name  (your-id)

[1/4] Playlists...
  35 playlists found
  [1/35] Rock - 87 tracks
  [2/35] (followed) Big Room Techno
  ...
```

## Why this exists

Every playlist-transfer service charges for this. Soundiiz's free tier stops
at one playlist of 200 tracks and puts liked songs behind the paywall;
TuneMyMusic caps out around 500 tracks. Meanwhile the API that all of them
call is free.

The free scripts that used to do the job mostly stopped working in early 2026.
Spotify's February 2026 migration removed the endpoints they rely on, and
`spotipy`'s convenience methods (`playlist_items`, `user_playlist_create`,
`current_user_saved_tracks_add`) still point at the old ones. The failure mode
is unpleasant: `GET /playlists/{id}/tracks` now answers **200 with an empty
list** instead of an error, so an export happily reports success while
collecting nothing.

This tool calls the current endpoints directly and falls back to the legacy
ones when those are what's being served. See [CHANGELOG.md](CHANGELOG.md) for
the full endpoint mapping.

## What gets transferred

| | |
|---|---|
| Playlists you created | name, description, public/private, track order |
| Playlists you follow | re-followed on the new account |
| Liked songs | in their original chronological order |
| Saved albums | yes |
| Followed artists | yes |

**Not transferable:** local files (the API does not expose them — they are
listed at the end of the export so you can re-add them by hand), custom
playlist cover images, and listening history.

## Requirements

- Python 3.8+
- A Spotify account at each end. The developer dashboard requires the app
  owner to have Premium.

## Setup

### 1. Create a Spotify app (2 minutes, free)

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard),
   sign in with the **old** account, click **Create app**.
2. Set the **Redirect URI** to exactly:

   ```
   http://127.0.0.1:8888/callback
   ```

   It has to be `127.0.0.1` — Spotify rejects `localhost`.
3. Tick **Web API**, save, then open **Settings** to copy your **Client ID**
   and **Client Secret**.
4. Open the **User Management** tab and add the email *and* username of
   **both** accounts. A development-mode app only serves allow-listed users,
   and without this the second sign-in is refused.

### 2. Run it

**Windows** — double-click `run.bat`.

**macOS / Linux**

```bash
pip install -r requirements.txt
python spotify_migrate.py
```

Pick `1`, sign in with the old account, wait. Then pick `2` and sign in with
the **new** one. If Spotify signs you straight back in as the old user, click
*Not you?* or use a private window.

The script refuses to run the import if both sides resolve to the same
account, and refuses to import an export that came back empty.

### Command line

```bash
python spotify_migrate.py export
python spotify_migrate.py import
python spotify_migrate.py status
python spotify_migrate.py reset
```

## If it stops halfway

Run it again and pick the same option. Progress is flushed to
`migration_state.json` after every batch, so it resumes exactly where it
stopped and never creates duplicates. Rate limits (HTTP 429) are handled by
waiting out `Retry-After`.

## Security

The tool talks only to `api.spotify.com`. Your credentials never leave your
machine. Three files hold secrets and are all git-ignored:

| File | Contents |
|---|---|
| `config.json` | your Client ID and Client Secret |
| `.cache-source` / `.cache-target` | OAuth tokens for each account |
| `spotify_export.json` | a full dump of your library |

Delete them when you are done, and revoke the app at
[spotify.com/account/apps](https://www.spotify.com/account/apps/).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `INVALID_CLIENT: Invalid redirect URI` | The dashboard value must be exactly `http://127.0.0.1:8888/callback` |
| `User not registered in the Developer Dashboard` | Add that account under **User Management** |
| `403 Forbidden` on writes | You are on an outdated copy of this script, or a scope was not granted — revoke the app, delete `.cache-*`, run again |
| Playlists export with 0 tracks | Same cause. This version cross-checks and warns you |
| Browser keeps using the wrong account | Sign out of spotify.com, or delete `.cache-target` |
| `Address already in use` | Port 8888 is taken — change it in the dashboard *and* in `SPOTIPY_REDIRECT_URI` |

## How it works

`spotipy` handles the OAuth dance only (browser flow, local callback server,
token refresh). Every API call is a plain `requests` call, so the tool does
not depend on the library keeping up with Spotify's changes.

Export writes `spotify_export.json` — a readable snapshot of your library that
doubles as a backup and can be replayed into any account later.

## Tests

The test suite runs the full export/import cycle against a mock API, in both
its current and legacy shapes, and checks that re-running the import is a
no-op. No network, no credentials.

```bash
python tests/test_migration.py
# or: pytest
```

## Contributing

Issues and pull requests welcome. If Spotify moves an endpoint again, the
places to look are `create_playlist`, `add_to_playlist`, `save_library` and
`fetch_playlist_tracks` — each one lists the endpoints it tries, in order.

## License

MIT — see [LICENSE](LICENSE).
