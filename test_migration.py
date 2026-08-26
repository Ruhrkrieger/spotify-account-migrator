#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
End-to-end tests against a mock Spotify API. No network, no credentials.

Each test runs the full export -> import cycle twice: once against the
current (post-February-2026) API shape, once against the legacy one, to prove
the fallback path works. A third pass checks that re-running the import is a
no-op, which is what makes the tool safe to resume.

    python tests/test_migration.py     # or: pytest
"""

import importlib.util
import json
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("spotify_migrate", ROOT / "spotify_migrate.py")
mig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mig)

# Fixture: playlist p1 has 120 tracks (forces pagination + batching),
# p2 has 1, p3 belongs to someone else (followed, not copied).
P1_TRACKS, P2_TRACKS = 120, 1
LIKED, ALBUMS, ARTISTS = 90, 3, 51


class MockApi(mig.Api):
    """Stands in for Api: same surface, answers from memory."""

    def __init__(self, user_id, modern=True):
        self.me = {"id": user_id, "display_name": user_id}
        self.modern = modern
        self.created, self.added, self.library, self.followed = [], {}, [], []

    def _gone(self, path):
        raise mig.ApiError(404, "endpoint removed", path)

    def raw(self, method, url, params=None, body=None):
        params = dict(params or {})
        path = url.replace(mig.BASE, "") if url.startswith("http") else url
        if "?" in path:                       # pagination passes a full next URL
            for k, v in urllib.parse.parse_qs(path.split("?", 1)[1]).items():
                params.setdefault(k, v[0])
            path = path.split("?")[0]

        # ---------------- reads ----------------
        if method == "GET" and path == "/me/playlists":
            return {"items": [
                {"id": "p1", "name": "Rock", "description": "d", "public": True,
                 "owner": {"id": "old"}, "items": {"total": P1_TRACKS}},
                {"id": "p2", "name": "Chill", "public": None,
                 "owner": {"id": "old"}, "tracks": {"total": P2_TRACKS}},
                {"id": "p3", "name": "Editorial", "owner": {"id": "spotify"}},
            ], "next": None}

        if method == "GET" and path.startswith("/playlists/") and path.endswith("/items"):
            if not self.modern:
                self._gone(path)
            pid = path.split("/")[2]
            total = P1_TRACKS if pid == "p1" else P2_TRACKS
            limit = int(params.get("limit", 50))
            assert limit <= 50, "GET /playlists/{id}/items caps limit at 50"
            offset = int(params.get("offset", 0) or 0)
            items = [{"item": {"id": f"{pid}t{i}", "type": "track"}, "is_local": False}
                     for i in range(offset, min(offset + limit, total))]
            nxt = (f"{mig.BASE}{path}?offset={offset + limit}&limit={limit}"
                   if offset + limit < total else None)
            return {"items": items, "next": nxt}

        if method == "GET" and path.startswith("/playlists/") and path.endswith("/tracks"):
            if self.modern:
                self._gone(path)
            pid = path.split("/")[2]
            total = P1_TRACKS if pid == "p1" else P2_TRACKS
            return {"items": [{"track": {"id": f"{pid}t{i}", "type": "track"}}
                              for i in range(total)], "next": None}

        if method == "GET" and path == "/me/tracks":
            return {"items": [{"track": {"id": f"L{i}"}} for i in range(LIKED)], "next": None}

        if method == "GET" and path == "/me/albums":
            return {"items": [{"album": {"id": f"A{i}"}} for i in range(ALBUMS)], "next": None}

        if method == "GET" and path == "/me/following":
            if params.get("after") is None:
                return {"artists": {"items": [{"id": f"R{i}"} for i in range(ARTISTS - 1)],
                                    "next": "more", "cursors": {"after": f"R{ARTISTS - 2}"}}}
            return {"artists": {"items": [{"id": f"R{ARTISTS - 1}"}],
                                "next": None, "cursors": {}}}

        # ---------------- writes ----------------
        if method == "POST" and path == "/me/playlists":
            if not self.modern:
                self._gone(path)
            self.created.append(body)
            return {"id": f"new{len(self.created)}"}

        if method == "POST" and path.startswith("/users/") and path.endswith("/playlists"):
            if self.modern:
                self._gone(path)
            self.created.append(body)
            return {"id": f"new{len(self.created)}"}

        if method == "POST" and path.startswith("/playlists/") and path.endswith("/items"):
            if not self.modern:
                self._gone(path)
            assert len(body["uris"]) <= mig.MAX_ADD_TO_PLAYLIST
            self.added.setdefault(path.split("/")[2], []).extend(body["uris"])
            return {}

        if method == "POST" and path.startswith("/playlists/") and path.endswith("/tracks"):
            if self.modern:
                self._gone(path)
            self.added.setdefault(path.split("/")[2], []).extend(body["uris"])
            return {}

        if method == "PUT" and path == "/me/library":
            if not self.modern:
                self._gone(path)
            uris = params["uris"].split(",")
            assert len(uris) <= mig.MAX_LIBRARY, "PUT /me/library caps at 40 URIs"
            self.library.extend(uris)
            return {}

        if method == "PUT" and path in ("/me/tracks", "/me/albums"):
            if self.modern:
                self._gone(path)
            self.library.extend(params["ids"].split(","))
            return {}

        if method == "PUT" and path == "/me/following":
            if self.modern:
                self._gone(path)
            self.library.extend(params["ids"].split(","))
            return {}

        if method == "PUT" and path.endswith("/followers"):
            if self.modern:
                self._gone(path)
            self.followed.append(path)
            return {}

        raise AssertionError(f"unexpected call: {method} {path}")


def _run_cycle(modern, tmp):
    mig.EXPORT_FILE = tmp / "export.json"
    mig.STATE_FILE = tmp / "state.json"
    for f in (mig.EXPORT_FILE, mig.STATE_FILE):
        if f.exists():
            f.unlink()

    source, target = MockApi("old", modern), MockApi("new", modern)
    real_api = mig.Api

    try:
        mig.Api = lambda *a, **k: source
        mig.cmd_export()
        data = json.loads(mig.EXPORT_FILE.read_text(encoding="utf-8"))

        assert len(data["playlists"]) == 2, "third playlist belongs to someone else"
        assert len(data["playlists"][0]["track_ids"]) == P1_TRACKS
        assert data["playlists"][0]["track_ids"][0] == "p1t0", "order must be preserved"
        assert data["playlists"][1]["public"] is False, "public:None means private"
        assert len(data["followed_playlists"]) == 1
        assert len(data["liked_tracks"]) == LIKED
        assert data["liked_tracks"][0] == f"L{LIKED - 1}", "liked songs replay oldest first"
        assert len(data["saved_albums"]) == ALBUMS
        assert len(data["followed_artists"]) == ARTISTS, "cursor pagination"
        assert data["incomplete"] == [], "advertised totals must match what we fetched"

        mig.Api = lambda *a, **k: target
        mig.cmd_import()

        assert len(target.created) == 2
        assert len(target.added["new1"]) == P1_TRACKS
        assert target.added["new1"][0] == "spotify:track:p1t0"
        # followed playlists go through the library endpoint on the modern path
        expected_library = LIKED + ALBUMS + ARTISTS + (1 if modern else 0)
        assert len(target.library) == expected_library, (len(target.library), expected_library)

        state = json.loads(mig.STATE_FILE.read_text(encoding="utf-8"))
        assert state["liked_done"] == LIKED
        assert state["artists_done"] == ARTISTS

        # resuming a finished migration must do nothing at all
        before = (len(target.created), len(target.library), len(target.added["new1"]))
        mig.cmd_import()
        after = (len(target.created), len(target.library), len(target.added["new1"]))
        assert before == after, "re-running the import created duplicates"
    finally:
        mig.Api = real_api


def test_modern_api(tmp_path=None):
    _run_cycle(True, tmp_path or Path("."))


def test_legacy_api(tmp_path=None):
    _run_cycle(False, tmp_path or Path("."))


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        for modern in (True, False):
            _run_cycle(modern, Path(d))
            print(f"PASS  {'current' if modern else 'legacy'} API shape")
    print("\nAll tests passed.")
    sys.exit(0)
