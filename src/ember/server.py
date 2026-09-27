"""MCP server: lets Claude log entries and answer questions about your life."""

import json
import re

from mcp.server.mcpserver import MCPServer

from . import db, embed, extract, tree

INSTRUCTIONS = """This is the user's personal life log. Two jobs:

1. LOGGING. When the user talks about their day or something that happened (usually dictated, so expect
   run-on speech and transcription slips), help them get it recorded:
   - Ask at most a few short follow-ups, and only for details that matter later and are missing:
     who exactly (full name or relationship if a first name is ambiguous), when (if not today),
     how much (for purchases), why (for big decisions). Don't interrogate; skip follow-ups for small stuff.
   - Then call log_entry with the user's messages copied VERBATIM as the transcript (no cleanup, no
     summarizing; join multiple messages with blank lines), and your follow-up Q&A in followups.
   - If the entry is about a past day, pass entry_date.
   - Confirm briefly. Processing happens in the background.

2. RECALL. When the user asks about their past ("when did I last see Jake?", "what did I spend on X?",
   "why did I decide Y?"), use person_timeline, search, and query. For vague or feeling-based questions
   ("when was I stressed?", "times I felt lonely"), use semantic_search. Always answer with dates and cite the
   entry id(s). If nothing is found, say so plainly; never guess.

3. PEOPLE. When the user clarifies who someone is, call update_person (check status for flagged people),
   and add_relation when it reveals family structure (parents, spouses, siblings). Call status if results look incomplete
   (entries may still be processing)."""

mcp = MCPServer("ember", instructions=INSTRUCTIONS)


def rows(cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]


@mcp.tool()
def log_entry(transcript: str, followups: str | None = None, entry_date: str | None = None) -> str:
    """Save a journal entry. The raw text is stored immediately; Claude extracts people, events,
    purchases, and decisions in the background.

    transcript: the user's messages, copied verbatim.
    followups: your follow-up questions and the user's answers, as "Q: ...\nA: ..." lines.
    entry_date: YYYY-MM-DD of the day this entry is about. Omit for today."""
    if entry_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry_date):
        return "entry_date must be YYYY-MM-DD"
    text = transcript.strip() + (f"\n\n## Follow-ups\n{followups.strip()}" if followups else "")
    with db.connect() as conn:
        entry_id = db.add_entry(conn, text, entry_date)
    extract.spawn_worker()
    return f"Saved entry {entry_id}. Indexing in the background."


@mcp.tool()
def search(query: str, limit: int = 20) -> str:
    """Full-text search across entries and extracted events, purchases, and decisions.
    Matches any of the words (stemmed), best matches first. Returns kind, date, entry_id, and content."""
    terms = re.findall(r"\w+", query)
    if not terms:
        return "[]"
    fts_query = " OR ".join(f'"{t}"' for t in terms)
    conn = db.connect(readonly=True)
    result = rows(conn.execute(
        "SELECT kind, date, entry_id, ref_id, snippet(search_fts, 0, '[', ']', '…', 40) AS content "
        "FROM search_fts WHERE search_fts MATCH ? ORDER BY bm25(search_fts) LIMIT ?",
        (fts_query, limit)))
    return json.dumps(result, indent=1)


@mcp.tool()
def semantic_search(query: str, limit: int = 10, date_from: str | None = None, date_to: str | None = None) -> str:
    """Search entries by meaning, not exact words (e.g. 'stressed about money' finds 'worried about rent').
    Best for vague, emotional, or thematic questions. Optional YYYY-MM-DD date range.
    Returns the best-matching passage per entry with its date, mood, summary, and similarity score."""
    conn = db.connect(readonly=True)
    try:
        return json.dumps(embed.search(conn, query, limit, date_from, date_to), indent=1)
    except Exception as exc:
        return f"Semantic search unavailable (is Ollama running?): {exc}"


@mcp.tool()
def person_timeline(name: str, limit: int = 30) -> str:
    """Find people by name, alias, or relationship (e.g. 'Jake', 'cousin', 'mom') and list the events
    they were part of, most recent first. Best tool for 'when did I last see X?'."""
    conn = db.connect(readonly=True)
    like = f"%{name}%"
    people = rows(conn.execute(
        "SELECT * FROM people WHERE name LIKE ? OR aliases LIKE ? OR relationship LIKE ?", (like, like, like)))
    for p in people:
        p["events"] = rows(conn.execute(
            "SELECT e.date, e.date_precision, e.title, e.description, e.location, e.entry_id "
            "FROM events e JOIN event_people ep ON ep.event_id = e.id "
            "WHERE ep.person_id = ? ORDER BY e.date DESC NULLS LAST LIMIT ?", (p["id"], limit)))
    return json.dumps(people, indent=1) if people else f"No person matching '{name}'."


@mcp.tool()
def update_person(name: str, relationship: str | None = None, aliases: list[str] | None = None,
                  person_id: int | None = None) -> str:
    """Correct or add a person when the user clarifies who someone is. Clears their review flag.
    person_id: the person to update (from person_timeline/status); omit to add a new person.
    name: canonical name (e.g. 'Lakshmi'); relationship: to the user, precise (e.g. "dad's younger sister");
    aliases: other ways the user refers to them (e.g. ['Athayya']), merged with existing ones."""
    with db.connect() as conn:
        if person_id is None:
            cur = conn.execute("INSERT INTO people (name, relationship, aliases) VALUES (?, ?, ?)",
                               (name, relationship, json.dumps(sorted(set(aliases or [])))))
            db.save_confirmed_people(conn)
            return f"Added person {cur.lastrowid}: {name}."
        row = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
        if not row:
            return f"No person {person_id}."
        merged = set(json.loads(row["aliases"])) | set(aliases or [])
        if name != row["name"]:
            merged.add(row["name"])
        merged.discard(name)
        conn.execute("UPDATE people SET name = ?, relationship = COALESCE(?, relationship), aliases = ?, "
                     "needs_review = NULL WHERE id = ?", (name, relationship, json.dumps(sorted(merged)), person_id))
        db.save_confirmed_people(conn)
    return f"Updated person {person_id}: {name}."


@mcp.tool()
def add_relation(person_a: int, kind: str, person_b: int) -> str:
    """Link two people in the family tree (ids from person_timeline/status; add people with update_person).
    kind: 'parent' (person_a is person_b's parent), 'spouse', 'partner', 'sibling', or 'cousin'.
    Prefer parent/spouse links; use sibling/cousin only when the connecting people are unknown.
    The user is the person whose relationship is 'self'."""
    if kind not in db.RELATION_KINDS:
        return f"kind must be one of {db.RELATION_KINDS}"
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO relations (a, kind, b) VALUES (?, ?, ?)", (person_a, kind, person_b))
        db.save_confirmed_people(conn)
        path = tree.write_html(conn)
    return f"Linked {person_a} {kind} {person_b}. Tree: {path}"


@mcp.tool()
def family_tree() -> str:
    """Regenerate the family tree page and return its path plus the Mermaid source."""
    conn = db.connect()
    return f"{tree.write_html(conn)}\n\n{tree.mermaid(conn)}"


@mcp.tool()
def query(sql: str) -> str:
    """Run a read-only SQL query (SQLite) for aggregates and trends, e.g. spending by month or mood over time.
    Tables: entries(id, created_at, entry_date, text, status, summary, mood, tags JSON),
    people(id, name, relationship, aliases JSON, needs_review),
    events(id, entry_id, date, date_precision, title, description, category, location),
    event_people(event_id, person_id),
    purchases(id, entry_id, date, item, amount, currency, merchant, category, notes),
    decisions(id, entry_id, date, decision, reasoning, alternatives, category),
    relations(a, kind, b) -- family links; kind in parent (a is b's parent) | spouse | partner | sibling | cousin,
    chunks(id, entry_id, idx, text, model, vec) -- embedding index; use semantic_search instead.
    Dates are 'YYYY-MM-DD' text. Returns at most 200 rows."""
    if not re.match(r"\s*(select|with)\b", sql, re.I):
        return "Only SELECT/WITH queries are allowed."
    conn = db.connect(readonly=True)
    try:
        cur = conn.execute(sql)
        return json.dumps([dict(r) for r in cur.fetchmany(200)], indent=1, default=str)
    except Exception as exc:
        return f"SQL error: {exc}"


@mcp.tool()
def get_entry(entry_id: int) -> str:
    """Get one entry's full original text plus everything extracted from it."""
    conn = db.connect(readonly=True)
    entry = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if not entry:
        return f"No entry {entry_id}."
    out = dict(entry)
    out["events"] = rows(conn.execute(
        "SELECT e.*, (SELECT group_concat(p.name, ', ') FROM event_people ep JOIN people p ON p.id = ep.person_id "
        "WHERE ep.event_id = e.id) AS people FROM events e WHERE entry_id = ?", (entry_id,)))
    out["purchases"] = rows(conn.execute("SELECT * FROM purchases WHERE entry_id = ?", (entry_id,)))
    out["decisions"] = rows(conn.execute("SELECT * FROM decisions WHERE entry_id = ?", (entry_id,)))
    return json.dumps(out, indent=1)


@mcp.tool()
def status() -> str:
    """Counts of entries by processing status, recent failures, and people flagged for review."""
    conn = db.connect(readonly=True)
    return json.dumps({
        "entries": {r["status"]: r["n"] for r in conn.execute("SELECT status, count(*) n FROM entries GROUP BY status")},
        "failures": rows(conn.execute("SELECT id, attempts, error FROM entries WHERE status = 'failed'")),
        "people_needing_review": rows(conn.execute(
            "SELECT id, name, relationship, needs_review FROM people WHERE needs_review IS NOT NULL")),
    }, indent=1)


def serve() -> None:
    db.connect().close()          # make sure the database and folders exist
    extract.spawn_worker()        # pick up anything left over from a previous run
    mcp.run()
