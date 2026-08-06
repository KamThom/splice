"""SQLite index for the music/audiobook library — a rebuildable cache, not the
source of truth (the files on disk are). Lives at Library/.splice/index.db, so
it travels with the library folder rather than being tied to one install."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    hash TEXT UNIQUE NOT NULL,
    library_path TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    title TEXT, artist TEXT, album TEXT, date TEXT, tracknumber TEXT,
    duration_seconds REAL,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id),
    track_id INTEGER NOT NULL REFERENCES tracks(id),
    position INTEGER NOT NULL,
    PRIMARY KEY (playlist_id, track_id)
);
CREATE TABLE IF NOT EXISTS fuzzy_dupes (
    id INTEGER PRIMARY KEY,
    track_id_a INTEGER NOT NULL REFERENCES tracks(id),
    track_id_b INTEGER NOT NULL REFERENCES tracks(id),
    reason TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tracks_title_artist ON tracks(title, artist);
"""


def db_path(library_root: str) -> Path:
    return Path(library_root) / ".splice" / "index.db"


def open_db(library_root: str) -> sqlite3.Connection:
    path = db_path(library_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def find_by_hash(conn: sqlite3.Connection, file_hash: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tracks WHERE hash = ?", (file_hash,)).fetchone()


def find_by_normalized_title_artist(conn: sqlite3.Connection, title: str, artist: str) -> list[sqlite3.Row]:
    if not title.strip() or not artist.strip():
        return []
    return conn.execute(
        "SELECT * FROM tracks WHERE lower(trim(title)) = ? AND lower(trim(artist)) = ?",
        (title.strip().lower(), artist.strip().lower()),
    ).fetchall()


def insert_track(conn: sqlite3.Connection, *, file_hash: str, library_path: str, kind: str,
                  title: str, artist: str, album: str, date: str, tracknumber: str,
                  duration_seconds: float | None) -> int:
    cur = conn.execute(
        """INSERT INTO tracks (hash, library_path, kind, title, artist, album, date,
                                tracknumber, duration_seconds, added_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (file_hash, library_path, kind, title, artist, album, date, tracknumber,
         duration_seconds, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def get_or_create_playlist(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM playlists WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO playlists (name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid


def next_playlist_position(conn: sqlite3.Connection, playlist_id: int) -> int:
    row = conn.execute(
        "SELECT MAX(position) AS m FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)
    ).fetchone()
    return (row["m"] + 1) if row["m"] is not None else 0


def add_playlist_track(conn: sqlite3.Connection, playlist_id: int, track_id: int, position: int):
    conn.execute(
        "INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
        (playlist_id, track_id, position),
    )
    conn.commit()


def playlist_track_paths_in_order(conn: sqlite3.Connection, playlist_id: int) -> list[str]:
    rows = conn.execute(
        """SELECT t.library_path FROM playlist_tracks pt
           JOIN tracks t ON t.id = pt.track_id
           WHERE pt.playlist_id = ?
           ORDER BY pt.position""",
        (playlist_id,),
    ).fetchall()
    return [row["library_path"] for row in rows]


def add_fuzzy_dupe(conn: sqlite3.Connection, track_id_a: int, track_id_b: int, reason: str):
    existing = conn.execute(
        """SELECT id FROM fuzzy_dupes
           WHERE (track_id_a = ? AND track_id_b = ?) OR (track_id_a = ? AND track_id_b = ?)""",
        (track_id_a, track_id_b, track_id_b, track_id_a),
    ).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO fuzzy_dupes (track_id_a, track_id_b, reason) VALUES (?, ?, ?)",
        (track_id_a, track_id_b, reason),
    )
    conn.commit()


def list_tracks(conn: sqlite3.Connection, kind: str | None = None) -> list[sqlite3.Row]:
    if kind:
        return conn.execute(
            "SELECT * FROM tracks WHERE kind = ? ORDER BY artist, album, tracknumber", (kind,)
        ).fetchall()
    return conn.execute("SELECT * FROM tracks ORDER BY artist, album, tracknumber").fetchall()


def list_playlists(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM playlists ORDER BY name").fetchall()


def playlist_membership_counts(conn: sqlite3.Connection) -> dict[int, int]:
    """track_id -> number of playlists it appears in."""
    rows = conn.execute("SELECT track_id, COUNT(*) AS n FROM playlist_tracks GROUP BY track_id").fetchall()
    return {row["track_id"]: row["n"] for row in rows}


def music_total_duration_seconds(conn: sqlite3.Connection) -> float:
    """Sum of duration across every music track, including Unsorted — real
    audio length isn't tag-dependent, so an unfiled track's runtime still
    counts toward total playtime."""
    row = conn.execute("SELECT COALESCE(SUM(duration_seconds), 0) AS total FROM tracks WHERE kind = 'music'").fetchone()
    return row["total"] or 0.0


def music_unique_artist_count(conn: sqlite3.Connection) -> int:
    """Distinct non-blank artists across music tracks. Unsorted tracks
    routinely have a blank artist (that's usually why they're Unsorted), so
    they're naturally excluded without a separate filter."""
    row = conn.execute(
        "SELECT COUNT(DISTINCT artist) AS n FROM tracks WHERE kind = 'music' AND artist IS NOT NULL AND trim(artist) != ''"
    ).fetchone()
    return row["n"] or 0


def list_unresolved_fuzzy_dupes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM fuzzy_dupes WHERE resolved = 0").fetchall()


def resolve_fuzzy_dupe(conn: sqlite3.Connection, dupe_id: int):
    conn.execute("UPDATE fuzzy_dupes SET resolved = 1 WHERE id = ?", (dupe_id,))
    conn.commit()


def rename_playlist(conn: sqlite3.Connection, playlist_id: int, new_name: str):
    conn.execute("UPDATE playlists SET name = ? WHERE id = ?", (new_name, playlist_id))
    conn.commit()


def delete_playlist(conn: sqlite3.Connection, playlist_id: int):
    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
    conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
    conn.commit()


def remove_playlist_track(conn: sqlite3.Connection, playlist_id: int, track_id: int):
    conn.execute(
        "DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id)
    )
    conn.commit()


def reorder_playlist_tracks(conn: sqlite3.Connection, playlist_id: int, ordered_track_ids: list[int]):
    for position, track_id in enumerate(ordered_track_ids):
        conn.execute(
            "UPDATE playlist_tracks SET position = ? WHERE playlist_id = ? AND track_id = ?",
            (position, playlist_id, track_id),
        )
    conn.commit()


def playlist_track_ids(conn: sqlite3.Connection, playlist_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT track_id FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)
    ).fetchall()
    return {row["track_id"] for row in rows}


def playlists_containing_track(conn: sqlite3.Connection, track_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT playlist_id FROM playlist_tracks WHERE track_id = ?", (track_id,)
    ).fetchall()
    return [row["playlist_id"] for row in rows]


def delete_track(conn: sqlite3.Connection, track_id: int):
    """Removes a track from the index entirely — the caller is responsible
    for deleting the underlying file and rewriting any playlists' .m3u8
    files (playlists_containing_track, called before this, tells you which)."""
    conn.execute("DELETE FROM playlist_tracks WHERE track_id = ?", (track_id,))
    conn.execute("DELETE FROM fuzzy_dupes WHERE track_id_a = ? OR track_id_b = ?", (track_id, track_id))
    conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
    conn.commit()
