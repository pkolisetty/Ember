"""Local semantic index: Ollama embeddings of entry chunks, stored in SQLite, searched by cosine similarity.

Brute force is fine at personal scale (tens of thousands of chunks search in milliseconds).
Vectors are tagged with the model name, so switching models just means re-embedding."""

import json
import os
import re
import sqlite3
import urllib.request

import numpy as np

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("EMBER_EMBED_MODEL", "nomic-embed-text")
CHUNK_CHARS = 1200

# nomic-embed-text expects these task prefixes.
DOC_PREFIX, QUERY_PREFIX = "search_document: ", "search_query: "


def ollama_embed(texts: list[str]) -> np.ndarray:
    req = urllib.request.Request(f"{OLLAMA_URL}/api/embed",
                                 data=json.dumps({"model": MODEL, "input": texts}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        vecs = np.array(json.load(resp)["embeddings"], dtype=np.float32)
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def chunk(text: str) -> list[str]:
    """Split on paragraphs/sentences into chunks of at most ~CHUNK_CHARS."""
    pieces = re.split(r"(?<=[.!?])\s+|\n\s*\n", text.strip())
    chunks, current = [], ""
    for piece in filter(None, (p.strip() for p in pieces)):
        if current and len(current) + len(piece) > CHUNK_CHARS:
            chunks.append(current)
            current = ""
        current = f"{current} {piece}".strip()
    return chunks + [current] if current else chunks


def embed_missing(conn: sqlite3.Connection, log=print) -> int:
    """Embed every entry that has no chunks for the current model. Fails soft if Ollama is down."""
    entries = conn.execute(
        "SELECT id, text FROM entries WHERE id NOT IN (SELECT entry_id FROM chunks WHERE model = ?)",
        (MODEL,)).fetchall()
    for entry in entries:
        pieces = chunk(entry["text"])
        try:
            vecs = ollama_embed([DOC_PREFIX + p for p in pieces])
        except Exception as exc:
            log(f"embedding skipped (is Ollama running?): {exc}")
            return 0
        with conn:
            conn.execute("DELETE FROM chunks WHERE entry_id = ?", (entry["id"],))
            conn.executemany("INSERT INTO chunks (entry_id, idx, text, model, vec) VALUES (?, ?, ?, ?, ?)",
                             [(entry["id"], i, p, MODEL, v.tobytes()) for i, (p, v) in enumerate(zip(pieces, vecs))])
    if entries:
        log(f"embedded {len(entries)} entries")
    return len(entries)


def search(conn: sqlite3.Connection, query: str, limit: int = 10,
           date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    rows = conn.execute(
        "SELECT c.entry_id, c.text, c.vec, e.entry_date, e.summary, e.mood FROM chunks c "
        "JOIN entries e ON e.id = c.entry_id WHERE c.model = ? "
        "AND (? IS NULL OR e.entry_date >= ?) AND (? IS NULL OR e.entry_date <= ?)",
        (MODEL, date_from, date_from, date_to, date_to)).fetchall()
    if not rows:
        return []
    matrix = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    scores = matrix @ ollama_embed([QUERY_PREFIX + query])[0]
    best: dict[int, dict] = {}  # best-scoring chunk per entry
    for i in np.argsort(-scores):
        r = rows[i]
        if r["entry_id"] not in best:
            best[r["entry_id"]] = {"entry_id": r["entry_id"], "date": r["entry_date"], "score": round(float(scores[i]), 3),
                                   "mood": r["mood"], "summary": r["summary"], "matching_text": r["text"]}
            if len(best) == limit:
                break
    return list(best.values())
