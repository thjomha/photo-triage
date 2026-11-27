"""SQLite database for the face service. Everything here stays on your own machine."""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
  id          INTEGER PRIMARY KEY,
  path        TEXT NOT NULL UNIQUE,     -- app path: share/folder/file.jpg
  folder      TEXT NOT NULL,
  size        INTEGER,
  mtime       REAL,
  width       INTEGER,
  height      INTEGER,
  taken_at    TEXT,                     -- ISO date(time): from EXIF, else file name, else file date
  date_source TEXT,                     -- 'exif' | 'filename' | 'mtime'
  lat         REAL,
  lon         REAL,
  place       TEXT,                     -- looked up from GPS (python -m faces.places)
  note        TEXT,                     -- your own description of the photo
  scanned_at  REAL,
  face_count  INTEGER NOT NULL DEFAULT 0,
  faces_done  INTEGER NOT NULL DEFAULT 0,
  error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_photos_folder ON photos(folder);
CREATE INDEX IF NOT EXISTS idx_photos_taken  ON photos(taken_at);

CREATE TABLE IF NOT EXISTS persons (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL COLLATE NOCASE UNIQUE,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS faces (
  id         INTEGER PRIMARY KEY,
  path       TEXT NOT NULL REFERENCES photos(path) ON DELETE CASCADE,
  x REAL NOT NULL, y REAL NOT NULL, w REAL NOT NULL, h REAL NOT NULL,   -- box, 0..1 of the photo
  score      REAL NOT NULL,
  emb        BLOB NOT NULL,             -- 128 float32, length 1
  crop       BLOB,                      -- small JPEG shown in the naming screen
  person_id  INTEGER REFERENCES persons(id) ON DELETE SET NULL,
  cluster_id INTEGER,                   -- group of look-alike unnamed faces
  ignored    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_faces_path    ON faces(path);
CREATE INDEX IF NOT EXISTS idx_faces_person  ON faces(person_id);
CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_id);

CREATE TABLE IF NOT EXISTS folder_labels (
  folder TEXT PRIMARY KEY,
  place  TEXT,
  note   TEXT
);
CREATE TABLE IF NOT EXISTS geocache (key TEXT PRIMARY KEY, place TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def default_path() -> str:
    return os.environ.get("FACES_DB") or os.path.join(os.path.expanduser("~"), "photo-triage-data", "faces.db")


def connect(path: str | None = None, init: bool = False) -> sqlite3.Connection:
    path = path or default_path()
    if init:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if init:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(text, tokenize='unicode61 remove_diacritics 2')"
            )
        except sqlite3.OperationalError:
            # SQLite without FTS5: fall back to a plain table searched with LIKE
            conn.execute("CREATE TABLE IF NOT EXISTS search (rowid INTEGER PRIMARY KEY, text TEXT)")
        conn.commit()
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='search'").fetchone()
    return bool(row and row[0] and "fts5" in row[0].lower())


def kv_get(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
    )
