#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
spotify_migrate.py -- copy a whole Spotify library from one account to another.

Targets the Spotify Web API as it stands after the February 2026 migration
(POST /me/playlists, GET|POST /playlists/{id}/items, PUT /me/library), and
falls back to the legacy endpoints when they are still the ones being served.

spotipy is used only for the OAuth dance; every API call is made directly.

    python spotify_migrate.py            # interactive menu
    python spotify_migrate.py export     # sign in to the OLD account
    python spotify_migrate.py import     # sign in to the NEW account
    python spotify_migrate.py status
    python spotify_migrate.py reset
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
except ImportError:
    print("Missing dependency. Run:   pip install spotipy requests")
    input("\nPress Enter to close...")
    sys.exit(1)

HERE = Path(__file__).resolve().parent
EXPORT_FILE = HERE / "spotify_export.json"
STATE_FILE = HERE / "migration_state.json"
CONFIG_FILE = HERE / "config.json"
CACHE_SOURCE = HERE / ".cache-source"
CACHE_TARGET = HERE / ".cache-target"

BASE = "https://api.spotify.com/v1"
REDIRECT_URI = os.environ.get("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

SCOPES_READ = ("playlist-read-private playlist-read-collaborative "
               "user-library-read user-follow-read")
SCOPES_WRITE = ("playlist-modify-public playlist-modify-private playlist-read-private "
                "user-library-modify user-follow-modify")

# Batch sizes imposed by the API
MAX_ADD_TO_PLAYLIST = 100   # POST /playlists/{id}/items
MAX_LIBRARY = 40            # PUT /me/library
PAGE = 50


def log(msg):
    print(msg, flush=True)


def load_json(path, default):
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            return default
    return default


def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def credentials():
    """Env vars first, then config.json, then ask once and remember."""
    cid = os.environ.get("SPOTIPY_CLIENT_ID")
    secret = os.environ.get("SPOTIPY_CLIENT_SECRET")
    if cid and secret:
        return cid, secret
    cfg = load_json(CONFIG_FILE, {})
    cid, secret = cfg.get("client_id"), cfg.get("client_secret")
    if cid and secret:
        return cid, secret
    log("\n--- Your Spotify app credentials (asked only once) ---")
    log("Find them at https://developer.spotify.com/dashboard -> your app -> Settings")
    cid = input("Client ID     : ").strip()
    secret = input("Client Secret : ").strip()
    if not cid or not secret:
        sys.exit("Empty credentials. Come back when you have them.")
    save_json(CONFIG_FILE, {"client_id": cid, "client_secret": secret})
    log(f"Saved to {CONFIG_FILE.name} (git-ignored). You won't be asked again.")
    return cid, secret


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status, text, url):
        super().__init__(f"HTTP {status} on {url}: {text[:300]}")
        self.status = status
        self.text = text


class Api:
    """Thin Spotify Web API client: retries, pagination, legacy fallback."""

    def __init__(self, scope, cache_path, label):
        cid, secret = credentials()
        self.auth = SpotifyOAuth(
            client_id=cid, client_secret=secret, redirect_uri=REDIRECT_URI,
            scope=scope, cache_path=str(cache_path),
            open_browser=True, show_dialog=True,
        )
        log(f"\n>>> A Spotify page will open. Sign in with {label}.")
        # spotipy runs the full OAuth flow on the first call
        self.me = spotipy.Spotify(auth_manager=self.auth).current_user()
        self.session = requests.Session()
        log(f"Signed in as: {self.me.get('display_name') or self.me['id']}  ({self.me['id']})")
        granted = (self.auth.get_cached_token() or {}).get("scope", "")
        missing = [s for s in scope.split() if s not in granted]
        if missing:
            log(f"WARNING - missing permissions: {' '.join(missing)}")
            log("  Revoke the app at https://www.spotify.com/account/apps/ ,")
            log("  delete the matching .cache-* file, then run again.")

    def token(self):
        tok = self.auth.get_cached_token()   # refreshes on its own when expired
        if not tok:
            raise ApiError(401, "no access token", "auth")
        return tok["access_token"]

    def raw(self, method, url, params=None, body=None):
        if not url.startswith("http"):
            url = BASE + url
        delay = 2
        for _ in range(8):
            headers = {"Authorization": f"Bearer {self.token()}"}
            try:
                r = self.session.request(method, url, headers=headers,
                                         params=params, json=body, timeout=30)
            except requests.RequestException as e:
                log(f"    ...network hiccup ({e}), retrying in {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if r.status_code == 429:                      # rate limited
                wait = int(r.headers.get("Retry-After", delay)) + 1
                log(f"    ...rate limited, waiting {wait}s")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                log(f"    ...server error {r.status_code}, retrying in {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if r.status_code >= 400:
                raise ApiError(r.status_code, r.text, url)
            if not r.content:
                return {}
            try:
                return r.json()
            except ValueError:
                return {}
        raise ApiError(0, "too many consecutive failures", url)

    def get(self, path, **params):
        return self.raw("GET", path, params=params or None)

    def try_both(self, attempts):
        """Run the first attempt that isn't rejected as a dead endpoint.

        The Feb 2026 migration answers removed endpoints with 400/403/404,
        so those statuses mean 'try the next shape' rather than 'give up'.
        """
        last = None
        for i, fn in enumerate(attempts):
            try:
                return fn()
            except ApiError as e:
                last = e
                if e.status in (400, 403, 404) and i < len(attempts) - 1:
                    continue
                raise
        raise last

    def paginate(self, path, **params):
        params.setdefault("limit", PAGE)
        page = self.get(path, **params)
        while page:
            block = page
            if "items" not in block:            # e.g. {"artists": {...}}
                for v in block.values():
                    if isinstance(v, dict) and "items" in v:
                        block = v
                        break
            for it in block.get("items", []):
                yield it
            nxt = block.get("next")
            page = self.raw("GET", nxt) if nxt else None


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def playlist_total(pl):
    """Track count as advertised by Spotify ('items' since Feb 2026)."""
    for key in ("items", "tracks"):
        blk = pl.get(key)
        if isinstance(blk, dict) and "total" in blk:
            return blk["total"]
    return None


def fetch_playlist_tracks(api, pid):
    """Read a playlist's tracks, new endpoint first, legacy one as a fallback."""
    def collect(path, limit):
        out, skipped = [], []
        for item in api.paginate(path, limit=limit, additional_types="track,episode"):
            t = item.get("item") or item.get("track")   # 'track' is deprecated
            if not t:
                continue
            if item.get("is_local") or t.get("is_local"):
                skipped.append(t.get("name") or "?")
                continue
            if t.get("type") != "track" or not t.get("id"):
                continue
            out.append(t["id"])
        return out, skipped

    try:
        return collect(f"/playlists/{pid}/items", 50)
    except ApiError as e:
        if e.status in (400, 403, 404):
            return collect(f"/playlists/{pid}/tracks", 100)
        raise


def cmd_export(_=None):
    api = Api(SCOPES_READ, CACHE_SOURCE, "the OLD account")
    my_id = api.me["id"]
    data = {"source_user": my_id, "playlists": [], "followed_playlists": [],
            "liked_tracks": [], "saved_albums": [], "followed_artists": [],
            "skipped_local_files": [], "incomplete": []}

    log("\n[1/4] Playlists...")
    all_pl = list(api.paginate("/me/playlists", limit=PAGE))
    log(f"  {len(all_pl)} playlists found")

    for i, pl in enumerate(all_pl, 1):
        owner = (pl.get("owner") or {}).get("id")
        if owner != my_id:
            data["followed_playlists"].append({"id": pl["id"], "name": pl["name"]})
            log(f"  [{i}/{len(all_pl)}] (followed) {pl['name']}")
            continue
        ids, local = fetch_playlist_tracks(api, pl["id"])
        for name in local:
            data["skipped_local_files"].append({"playlist": pl["name"], "name": name})
        total = playlist_total(pl)
        warn = ""
        if total is not None and len(ids) + len(local) < total:
            warn = f"   <-- {total} expected"
            data["incomplete"].append({"name": pl["name"], "expected": total,
                                       "retrieved": len(ids)})
        data["playlists"].append({
            "old_id": pl["id"], "name": pl["name"],
            "description": pl.get("description") or "",
            "public": bool(pl.get("public")), "track_ids": ids,
        })
        log(f"  [{i}/{len(all_pl)}] {pl['name']} - {len(ids)} tracks{warn}")

    log("\n[2/4] Liked songs...")
    liked = [(it.get("track") or it.get("item") or {}).get("id")
             for it in api.paginate("/me/tracks", limit=PAGE)]
    liked = [x for x in liked if x]
    data["liked_tracks"] = list(reversed(liked))    # oldest first, to keep the order
    log(f"  {len(liked)} liked songs")

    log("\n[3/4] Saved albums...")
    albums = [(it.get("album") or it.get("item") or {}).get("id")
              for it in api.paginate("/me/albums", limit=PAGE)]
    albums = [x for x in albums if x]
    data["saved_albums"] = list(reversed(albums))
    log(f"  {len(albums)} albums")

    log("\n[4/4] Followed artists...")
    artists = []
    page = api.get("/me/following", type="artist", limit=PAGE)
    while page:
        block = page.get("artists", page)
        for a in block.get("items", []):
            if a.get("id"):
                artists.append(a["id"])
        after = (block.get("cursors") or {}).get("after")
        page = (api.get("/me/following", type="artist", limit=PAGE, after=after)
                if (block.get("next") and after) else None)
    data["followed_artists"] = artists
    log(f"  {len(artists)} artists")

    total_tracks = sum(len(p["track_ids"]) for p in data["playlists"])
    data["signature"] = (f"{my_id}|{len(data['playlists'])}|{total_tracks}|"
                         f"{len(data['liked_tracks'])}")
    save_json(EXPORT_FILE, data)

    log(f"\nExport complete -> {EXPORT_FILE.name}")
    log(f"  {len(data['playlists'])} own playlists ({total_tracks} tracks), "
        f"{len(data['followed_playlists'])} followed, {len(data['liked_tracks'])} liked, "
        f"{len(data['saved_albums'])} albums, {len(data['followed_artists'])} artists")
    if data["skipped_local_files"]:
        log(f"  NOTE: {len(data['skipped_local_files'])} local files skipped "
            f"(the API cannot transfer those)")
    if data["incomplete"]:
        log(f"  WARNING: {len(data['incomplete'])} incomplete playlists, "
            f"see {EXPORT_FILE.name}")
    if total_tracks == 0 and data["playlists"]:
        log("\n  WARNING: no track was retrieved at all. Do not run the import;")
        log("  please open an issue, the API shape has probably changed again.")
    else:
        log("\nNext step: option 2 (IMPORT).")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def create_playlist(api, name, public, description):
    body = {"name": name, "public": public, "description": description[:300]}
    uid = api.me["id"]
    return api.try_both([
        lambda: api.raw("POST", "/me/playlists", body=body),
        lambda: api.raw("POST", f"/users/{uid}/playlists", body=body),   # pre-2026
    ])


def add_to_playlist(api, pid, uris):
    return api.try_both([
        lambda: api.raw("POST", f"/playlists/{pid}/items", body={"uris": uris}),
        lambda: api.raw("POST", f"/playlists/{pid}/tracks", body={"uris": uris}),
    ])


def save_library(api, uris, legacy_path=None, legacy_ids=None):
    """PUT /me/library, falling back to the old per-type endpoint."""
    attempts = [lambda: api.raw("PUT", "/me/library", params={"uris": ",".join(uris)})]
    if legacy_path:
        attempts.append(lambda: api.raw("PUT", legacy_path,
                                        params={"ids": ",".join(legacy_ids)}))
    return api.try_both(attempts)


def cmd_import(_=None):
    if not EXPORT_FILE.exists():
        log("No export found. Run option 1 (EXPORT) first.")
        return
    data = load_json(EXPORT_FILE, None)
    total_tracks = sum(len(p["track_ids"]) for p in data["playlists"])
    if data["playlists"] and total_tracks == 0:
        log("The export holds no track at all, so it is unusable.")
        log("Run option 1 (EXPORT) again before importing.")
        return

    state = load_json(STATE_FILE, {})
    if state.get("signature") != data.get("signature"):
        if state:
            log("The export changed since the last import: starting over.")
        state = {"signature": data.get("signature"), "playlists": {},
                 "followed_playlists": [], "liked_done": 0,
                 "albums_done": 0, "artists_done": 0}

    api = Api(SCOPES_WRITE, CACHE_TARGET, "the NEW account")
    if api.me["id"] == data["source_user"]:
        log("\nYou signed in with the SAME account as the export.")
        log("Delete the .cache-target file and try again.")
        return

    def flush():
        save_json(STATE_FILE, state)

    pls = data["playlists"]
    log(f"\n[1/5] Recreating {len(pls)} playlists ({total_tracks} tracks)...")
    for i, pl in enumerate(pls, 1):
        entry = state["playlists"].get(pl["old_id"])
        if entry and entry.get("done", 0) >= len(pl["track_ids"]):
            log(f"  [{i}/{len(pls)}] {pl['name']} - already done")
            continue
        if not entry:
            created = create_playlist(api, pl["name"], pl["public"], pl["description"])
            entry = {"new_id": created["id"], "done": 0}
            state["playlists"][pl["old_id"]] = entry
            flush()
        for batch in chunks(pl["track_ids"][entry["done"]:], MAX_ADD_TO_PLAYLIST):
            add_to_playlist(api, entry["new_id"], [f"spotify:track:{t}" for t in batch])
            entry["done"] += len(batch)
            flush()
        log(f"  [{i}/{len(pls)}] {pl['name']} - {entry['done']} tracks")

    fpl = data["followed_playlists"]
    log(f"\n[2/5] Re-following {len(fpl)} playlists...")
    for pl in fpl:
        if pl["id"] in state["followed_playlists"]:
            continue
        try:
            api.try_both([
                lambda: api.raw("PUT", "/me/library",
                                params={"uris": f"spotify:playlist:{pl['id']}"}),
                lambda: api.raw("PUT", f"/playlists/{pl['id']}/followers",
                                body={"public": False}),
            ])
            state["followed_playlists"].append(pl["id"])
            flush()
            log(f"  followed: {pl['name']}")
        except ApiError as e:
            log(f"  could not follow '{pl['name']}' ({e.status})")

    liked = data["liked_tracks"]
    log(f"\n[3/5] {len(liked)} liked songs...")
    while state["liked_done"] < len(liked):
        batch = liked[state["liked_done"]:state["liked_done"] + MAX_LIBRARY]
        save_library(api, [f"spotify:track:{t}" for t in batch], "/me/tracks", batch)
        state["liked_done"] += len(batch)
        flush()
        log(f"  {state['liked_done']}/{len(liked)}")

    albums = data["saved_albums"]
    log(f"\n[4/5] {len(albums)} albums...")
    while state["albums_done"] < len(albums):
        batch = albums[state["albums_done"]:state["albums_done"] + MAX_LIBRARY]
        save_library(api, [f"spotify:album:{a}" for a in batch], "/me/albums", batch)
        state["albums_done"] += len(batch)
        flush()
        log(f"  {state['albums_done']}/{len(albums)}")

    artists = data["followed_artists"]
    log(f"\n[5/5] {len(artists)} artists...")
    while state["artists_done"] < len(artists):
        batch = artists[state["artists_done"]:state["artists_done"] + MAX_LIBRARY]
        uris = [f"spotify:artist:{a}" for a in batch]
        api.try_both([
            lambda: api.raw("PUT", "/me/library", params={"uris": ",".join(uris)}),
            lambda: api.raw("PUT", "/me/following",
                            params={"type": "artist", "ids": ",".join(batch)}),
        ])
        state["artists_done"] += len(batch)
        flush()
        log(f"  {state['artists_done']}/{len(artists)}")

    log("\n=== Migration complete. Open Spotify on the new account. ===")
    if data.get("skipped_local_files"):
        log(f"Reminder: {len(data['skipped_local_files'])} local files could not be moved.")


# ---------------------------------------------------------------------------

def cmd_status(_=None):
    if not EXPORT_FILE.exists():
        log("Nothing exported yet.")
        return
    d = load_json(EXPORT_FILE, {})
    s = load_json(STATE_FILE, {})
    tt = sum(len(p["track_ids"]) for p in d.get("playlists", []))
    log(f"Export: {len(d.get('playlists', []))} own playlists ({tt} tracks), "
        f"{len(d.get('followed_playlists', []))} followed, "
        f"{len(d.get('liked_tracks', []))} liked, "
        f"{len(d.get('saved_albums', []))} albums, "
        f"{len(d.get('followed_artists', []))} artists.")
    if not s:
        log("Import: not started.")
        return
    done = sum(e.get("done", 0) for e in s.get("playlists", {}).values())
    log(f"Import: {len(s.get('playlists', {}))} playlists created ({done} tracks added), "
        f"{s.get('liked_done', 0)} liked, {s.get('albums_done', 0)} albums, "
        f"{s.get('artists_done', 0)} artists.")


def cmd_reset(_=None):
    log("This deletes the local export and progress. Nothing on Spotify is touched.")
    if input("Confirm? [y/N] : ").strip().lower() not in ("y", "yes", "o"):
        log("Cancelled.")
        return
    for f in (EXPORT_FILE, STATE_FILE):
        if f.exists():
            f.unlink()
            log(f"  deleted: {f.name}")
    log("Ready for a fresh export.")


MENU = """
==============================================
   Spotify account migrator
==============================================

  1. EXPORT  - sign in to the OLD account
  2. IMPORT  - sign in to the NEW account
  3. Show progress
  4. Reset (start over)
  5. Quit
"""


def interactive():
    actions = {"1": cmd_export, "2": cmd_import, "3": cmd_status, "4": cmd_reset}
    while True:
        print(MENU)
        choice = input("Your choice [1-5]: ").strip()
        if choice in ("5", "q", "Q", ""):
            return
        fn = actions.get(choice)
        if not fn:
            print("\nInvalid choice.")
            continue
        try:
            fn()
        except ApiError as e:
            print(f"\n>>> API error: {e}")
        except KeyboardInterrupt:
            print("\nInterrupted. Run again to resume where it stopped.")
        except Exception:
            import traceback
            traceback.print_exc()
            print("\n>>> Something went wrong (details above).")
        input("\nPress Enter to go back to the menu...")


def main():
    if len(sys.argv) == 1:
        interactive()
        return
    p = argparse.ArgumentParser(description="Migrate a Spotify library to another account.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export", help="read the old account").set_defaults(func=cmd_export)
    sub.add_parser("import", help="write to the new account").set_defaults(func=cmd_import)
    sub.add_parser("status", help="show progress").set_defaults(func=cmd_status)
    sub.add_parser("reset", help="delete local export and progress").set_defaults(func=cmd_reset)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception:
        import traceback
        traceback.print_exc()
        input("\nPress Enter to close...")
