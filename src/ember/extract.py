"""Background processing: send pending entries to Claude (`claude -p`) and store what it extracts."""

import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import db, embed

MODEL = os.environ.get("EMBER_MODEL", "opus")
MAX_ATTEMPTS = 3

CATEGORIES = ["social", "family", "work", "health", "fitness", "travel", "errand", "home",
              "finance", "entertainment", "learning", "milestone", "other"]

_str_or_null = {"type": ["string", "null"]}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "mood", "tags", "people", "events", "purchases", "decisions"],
    "properties": {
        "summary": {"type": "string", "description": "1-2 sentence summary of the entry."},
        "mood": {"type": ["integer", "null"], "minimum": 1, "maximum": 10},
        "tags": {"type": "array", "items": {"type": "string"}},
        "people": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["ref", "name", "existing_person_id", "relationship", "aliases", "ambiguity"],
                "properties": {
                    "ref": {"type": "string", "description": "Local handle used by events, e.g. 'p1'."},
                    "name": {"type": "string"},
                    "existing_person_id": {"type": ["integer", "null"]},
                    "relationship": _str_or_null,
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "ambiguity": {**_str_or_null, "description": "Why the match to a known person is uncertain; null if confident."},
                },
            },
        },
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["date", "date_precision", "title", "description", "category", "location", "people"],
                "properties": {
                    "date": _str_or_null,
                    "date_precision": {"enum": ["day", "month", "year", "unknown"]},
                    "title": {"type": "string"},
                    "description": _str_or_null,
                    "category": {"enum": CATEGORIES},
                    "location": _str_or_null,
                    "people": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "purchases": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["date", "item", "amount", "currency", "merchant", "category", "notes"],
                "properties": {
                    "date": _str_or_null,
                    "item": {"type": "string"},
                    "amount": {"type": ["number", "null"]},
                    "currency": _str_or_null,
                    "merchant": _str_or_null,
                    "category": _str_or_null,
                    "notes": _str_or_null,
                },
            },
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["date", "decision", "reasoning", "alternatives", "category"],
                "properties": {
                    "date": _str_or_null,
                    "decision": {"type": "string"},
                    "reasoning": _str_or_null,
                    "alternatives": _str_or_null,
                    "category": _str_or_null,
                },
            },
        },
    },
}

SYSTEM_PROMPT = """You turn personal journal entries into structured records for a private life log.
The entries are dictated (speech-to-text), so fix obvious transcription errors, especially names that
closely match known people.

Rules:
- Dates: resolve relative dates ("yesterday", "last Tuesday", "this morning") against the entry date.
  Output YYYY-MM-DD. If only the month or year is known, use the first day and set date_precision.
  If the date can't be determined, use null and "unknown".
- Events: one per distinct thing that happened (meetings, activities, milestones, appointments...).
  Don't invent events that weren't described. Attach people by their ref.
  A plain purchase goes only in purchases, not also in events (unless it was an outing in itself).
- People: every named person, plus clearly identifiable unnamed ones ("my mom").
  If a person matches a KNOWN PERSON, set existing_person_id. Match only when confident; if two known
  people could fit, or it's a new person with a known name, leave existing_person_id null and explain in ambiguity.
  The user is the author; never list them as a person.
- Purchases: anything bought or paid for, with amount when stated. Don't guess prices.
- Decisions: meaningful choices the user made or committed to, with their reasoning if given.
- mood: 1-10 only if the entry conveys it; otherwise null.
- tags: a few short lowercase topic tags."""


def claude_bin() -> str:
    return shutil.which("claude") or str(Path.home() / ".local/bin/claude")


def known_people(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT p.id, p.name, p.relationship, p.aliases, MAX(e.date) AS last_seen
        FROM people p
        LEFT JOIN event_people ep ON ep.person_id = p.id
        LEFT JOIN events e ON e.id = ep.event_id
        GROUP BY p.id ORDER BY p.name
    """).fetchall()
    return [{**dict(r), "aliases": json.loads(r["aliases"])} for r in rows]


def call_claude(entry: sqlite3.Row, people: list[dict]) -> dict:
    prompt = (
        f"ENTRY DATE: {entry['entry_date']} (logged at {entry['created_at']})\n\n"
        f"KNOWN PEOPLE:\n{json.dumps(people, indent=1) if people else '(none yet)'}\n\n"
        f"ENTRY:\n{entry['text']}"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_CODE") and k != "CLAUDECODE"}
    proc = subprocess.run(
        [claude_bin(), "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
         "--system-prompt", SYSTEM_PROMPT, "--model", MODEL, "--tools", "",
         "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence"],
        input=prompt, capture_output=True, text=True, timeout=600, env=env, cwd=db.HOME,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[-2000:]}")
    out = json.loads(proc.stdout)
    if out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"claude returned no structured output: {proc.stdout[-2000:]}")
    return out["structured_output"]


def clear_extracted(conn: sqlite3.Connection, entry_id: int) -> None:
    for table in ("events", "purchases", "decisions"):
        conn.execute(f"DELETE FROM {table} WHERE entry_id = ?", (entry_id,))
    conn.execute("DELETE FROM search_fts WHERE entry_id = ? AND kind != 'entry'", (entry_id,))


def apply(conn: sqlite3.Connection, entry: sqlite3.Row, data: dict) -> None:
    entry_id, entry_date = entry["id"], entry["entry_date"]
    clear_extracted(conn, entry_id)

    def index(content: str, kind: str, ref_id: int, date: str | None):
        conn.execute("INSERT INTO search_fts (content, kind, ref_id, entry_id, date) VALUES (?, ?, ?, ?, ?)",
                     (content, kind, ref_id, entry_id, date or entry_date))

    person_ids: dict[str, int] = {}
    for p in data["people"]:
        existing = p["existing_person_id"]
        row = existing and conn.execute("SELECT * FROM people WHERE id = ?", (existing,)).fetchone()
        if row:
            aliases = set(json.loads(row["aliases"])) | set(p["aliases"])
            if p["name"] != row["name"]:
                aliases.add(p["name"])
            conn.execute("UPDATE people SET aliases = ?, relationship = COALESCE(relationship, ?) WHERE id = ?",
                         (json.dumps(sorted(aliases)), p["relationship"], row["id"]))
            person_ids[p["ref"]] = row["id"]
        else:
            cur = conn.execute("INSERT INTO people (name, relationship, aliases, needs_review) VALUES (?, ?, ?, ?)",
                               (p["name"], p["relationship"], json.dumps(sorted(set(p["aliases"]))), p["ambiguity"]))
            person_ids[p["ref"]] = cur.lastrowid

    names = {pid: n for pid, n in conn.execute("SELECT id, name || COALESCE(' (' || relationship || ')', '') FROM people")}
    for e in data["events"]:
        cur = conn.execute(
            "INSERT INTO events (entry_id, date, date_precision, title, description, category, location) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entry_id, e["date"], e["date_precision"], e["title"], e["description"], e["category"], e["location"]))
        pids = {person_ids[r] for r in e["people"] if r in person_ids}
        conn.executemany("INSERT INTO event_people (event_id, person_id) VALUES (?, ?)",
                         [(cur.lastrowid, pid) for pid in pids])
        who = ", ".join(names[pid] for pid in pids)
        index(" | ".join(filter(None, [e["title"], e["description"], e["location"], who, e["category"]])),
              "event", cur.lastrowid, e["date"])

    for p in data["purchases"]:
        cur = conn.execute(
            "INSERT INTO purchases (entry_id, date, item, amount, currency, merchant, category, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entry_id, p["date"], p["item"], p["amount"], p["currency"], p["merchant"], p["category"], p["notes"]))
        index(" | ".join(filter(None, ["purchase", p["item"], p["merchant"], p["category"], p["notes"]])),
              "purchase", cur.lastrowid, p["date"])

    for d in data["decisions"]:
        cur = conn.execute(
            "INSERT INTO decisions (entry_id, date, decision, reasoning, alternatives, category) VALUES (?, ?, ?, ?, ?, ?)",
            (entry_id, d["date"], d["decision"], d["reasoning"], d["alternatives"], d["category"]))
        index(" | ".join(filter(None, ["decision", d["decision"], d["reasoning"], d["alternatives"]])),
              "decision", cur.lastrowid, d["date"])

    conn.execute(
        "UPDATE entries SET status = 'done', error = NULL, summary = ?, mood = ?, tags = ?, processed_at = ? WHERE id = ?",
        (data["summary"], data["mood"], json.dumps(data["tags"]),
         datetime.now().astimezone().isoformat(timespec="seconds"), entry_id))


def process_pending(log=print) -> int:
    """Process every pending (or retryable failed) entry. Only one worker runs at a time."""
    conn = db.connect()
    with open(db.HOME / ".worker.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another worker is already running")
            return 0
        done = 0
        embed.embed_missing(conn, log)  # local stage first
        while True:
            entry = conn.execute(
                "SELECT * FROM entries WHERE status = 'pending' OR (status = 'failed' AND attempts < ?) "
                "ORDER BY id LIMIT 1", (MAX_ATTEMPTS,)).fetchone()
            if not entry:
                return done
            log(f"processing entry {entry['id']}")
            conn.execute("UPDATE entries SET attempts = attempts + 1 WHERE id = ?", (entry["id"],))
            conn.commit()
            try:
                data = call_claude(entry, known_people(conn))
                with conn:
                    apply(conn, entry, data)
                done += 1
                log(f"entry {entry['id']}: {len(data['events'])} events, {len(data['people'])} people, "
                    f"{len(data['purchases'])} purchases, {len(data['decisions'])} decisions")
            except Exception as exc:  # keep going; the entry stays retryable
                conn.rollback()
                conn.execute("UPDATE entries SET status = 'failed', error = ? WHERE id = ?", (str(exc), entry["id"]))
                conn.commit()
                log(f"entry {entry['id']} failed: {exc}")


def spawn_worker() -> None:
    """Start `ember process` detached, so logging returns immediately."""
    log_file = open(db.LOG_DIR / "worker.log", "a")
    subprocess.Popen([sys.executable, "-m", "ember.cli", "process"],
                     stdout=log_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     start_new_session=True, cwd=db.HOME)
