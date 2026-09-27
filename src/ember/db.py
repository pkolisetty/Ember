"""Storage: raw Markdown files (source of truth) + a SQLite index derived from them."""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get("EMBER_HOME", Path.home() / "ember"))
RAW_DIR = HOME / "raw"
DB_PATH = HOME / "ember.db"
LOG_DIR = HOME / "logs"
PEOPLE_PATH = HOME / "people.json"  # user-confirmed people; survives rebuilds
RELATIONS_PATH = HOME / "relations.json"  # family-tree links between people; survives rebuilds
RELATION_KINDS = ("parent", "spouse", "partner", "sibling", "cousin")  # "a parent b" = a is b's parent

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id           INTEGER PRIMARY KEY,
    created_at   TEXT NOT NULL,          -- when it was logged (local ISO time)
    entry_date   TEXT NOT NULL,          -- the day the entry is about (YYYY-MM-DD)
    raw_path     TEXT NOT NULL,
    text         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    summary      TEXT,
    mood         INTEGER,
    tags         TEXT,                   -- JSON array
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS people (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    relationship TEXT,
    aliases      TEXT NOT NULL DEFAULT '[]',  -- JSON array
    needs_review TEXT                         -- why a match was uncertain, if it was
);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    entry_id       INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    date           TEXT,                 -- YYYY-MM-DD, or NULL if unknown
    date_precision TEXT,                 -- day | month | year | unknown
    title          TEXT NOT NULL,
    description    TEXT,
    category       TEXT,
    location       TEXT
);

CREATE TABLE IF NOT EXISTS event_people (
    event_id  INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, person_id)
);

CREATE TABLE IF NOT EXISTS purchases (
    id       INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    date     TEXT,
    item     TEXT NOT NULL,
    amount   REAL,
    currency TEXT,
    merchant TEXT,
    category TEXT,
    notes    TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY,
    entry_id     INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    date         TEXT,
    decision     TEXT NOT NULL,
    reasoning    TEXT,
    alternatives TEXT,
    category     TEXT
);

-- Family-tree links. parent is directed (a is b's parent); the rest are symmetric.
CREATE TABLE IF NOT EXISTS relations (
    a    INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    b    INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    PRIMARY KEY (a, kind, b)
);

-- Semantic index: embedding vectors (float32 blobs) of entry text chunks.
CREATE TABLE IF NOT EXISTS chunks (
    id       INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    idx      INTEGER NOT NULL,
    text     TEXT NOT NULL,
    model    TEXT NOT NULL,
    vec      BLOB NOT NULL
);

-- Full-text index over entries and everything extracted from them.
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    content,
    kind UNINDEXED,        -- entry | event | purchase | decision
    ref_id UNINDEXED,
    entry_id UNINDEXED,
    date UNINDEXED,
    tokenize = 'porter unicode61'
);
"""


def connect(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    else:
        for d in (RAW_DIR, LOG_DIR):
            d.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def add_entry(conn: sqlite3.Connection, text: str, entry_date: str | None = None) -> int:
    """Write the raw Markdown file first, then index it. The file is the source of truth."""
    now = datetime.now().astimezone()
    entry_date = entry_date or now.date().isoformat()
    cur = conn.execute(
        "INSERT INTO entries (created_at, entry_date, raw_path, text) VALUES (?, ?, '', ?)",
        (now.isoformat(timespec="seconds"), entry_date, text),
    )
    entry_id = cur.lastrowid
    path = RAW_DIR / now.strftime("%Y/%m") / f"{now:%Y-%m-%d_%H%M%S}_{entry_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {entry_id}\ncreated_at: {now.isoformat(timespec='seconds')}\n"
        f"entry_date: {entry_date}\n---\n\n{text.strip()}\n"
    )
    conn.execute("UPDATE entries SET raw_path = ? WHERE id = ?", (str(path), entry_id))
    conn.execute(
        "INSERT INTO search_fts (content, kind, ref_id, entry_id, date) VALUES (?, 'entry', ?, ?, ?)",
        (text, entry_id, entry_id, entry_date),
    )
    conn.commit()
    return entry_id


def rebuild() -> int:
    """Recreate the database from the raw Markdown files. The old database is kept as a backup.
    Entries come back as 'pending'; run the worker afterwards to re-extract them."""
    if DB_PATH.exists():
        backup = DB_PATH.with_name(f"ember.db.bak-{datetime.now():%Y%m%d-%H%M%S}")
        DB_PATH.rename(backup)
        for suffix in ("-wal", "-shm"):
            Path(f"{DB_PATH}{suffix}").unlink(missing_ok=True)
    conn = connect()
    if PEOPLE_PATH.exists():
        conn.executemany("INSERT INTO people (id, name, relationship, aliases) VALUES (?, ?, ?, ?)",
                         [(p["id"], p["name"], p["relationship"], json.dumps(p["aliases"]))
                          for p in json.loads(PEOPLE_PATH.read_text())])
    if RELATIONS_PATH.exists():
        conn.executemany("INSERT INTO relations (a, kind, b) VALUES (?, ?, ?)",
                         [(r["a"], r["kind"], r["b"]) for r in json.loads(RELATIONS_PATH.read_text())])
    count = 0
    for path in sorted(RAW_DIR.rglob("*.md")):
        _, front, body = path.read_text().split("---\n", 2)
        meta = dict(line.split(": ", 1) for line in front.strip().splitlines())
        text = body.strip()
        conn.execute("INSERT INTO entries (id, created_at, entry_date, raw_path, text) VALUES (?, ?, ?, ?, ?)",
                     (int(meta["id"]), meta["created_at"], meta["entry_date"], str(path), text))
        conn.execute("INSERT INTO search_fts (content, kind, ref_id, entry_id, date) VALUES (?, 'entry', ?, ?, ?)",
                     (text, int(meta["id"]), int(meta["id"]), meta["entry_date"]))
        count += 1
    conn.commit()
    return count


def save_confirmed_people(conn: sqlite3.Connection) -> None:
    """Write every person the user has confirmed (no review flag) to people.json."""
    people = [{"id": r["id"], "name": r["name"], "relationship": r["relationship"], "aliases": json.loads(r["aliases"])}
              for r in conn.execute("SELECT * FROM people WHERE needs_review IS NULL ORDER BY id")]
    PEOPLE_PATH.write_text(json.dumps(people, indent=1, ensure_ascii=False) + "\n")
    relations = [dict(r) for r in conn.execute("SELECT a, kind, b FROM relations ORDER BY a, b")]
    RELATIONS_PATH.write_text(json.dumps(relations, indent=1) + "\n")
