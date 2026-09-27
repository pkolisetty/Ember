"""Keep Handy's custom-words list in sync with the people in the life log, so dictation spells names right."""

import json
import subprocess
import time
from pathlib import Path

from . import db

SETTINGS = Path.home() / "Library/Application Support/com.pais.handy/settings_store.json"
EXTRA_WORDS = ["Athayya", "Mavayya", "Babai", "Pinni", "Peddamma", "Peddananna", "Nanna", "Amma"]


def sync() -> list[str]:
    """Merge all people names/aliases into Handy's custom_words. Restarts Handy so it picks them up."""
    conn = db.connect(readonly=True)
    words = set(EXTRA_WORDS)
    for p in conn.execute("SELECT name, aliases FROM people WHERE relationship IS NOT 'self'"):
        for w in [p["name"], *json.loads(p["aliases"])]:
            words.update(w.split())  # single words match best
    running = subprocess.run(["pgrep", "-x", "handy"], capture_output=True).returncode == 0
    if running:  # Handy rewrites its settings on exit, so quit it before editing
        subprocess.run(["osascript", "-e", 'quit app "Handy"'])
        for _ in range(20):
            if subprocess.run(["pgrep", "-x", "handy"], capture_output=True).returncode != 0:
                break
            time.sleep(0.5)
    store = json.loads(SETTINGS.read_text())
    merged = sorted(set(store["settings"].get("custom_words") or []) | words, key=str.lower)
    store["settings"]["custom_words"] = merged
    SETTINGS.write_text(json.dumps(store, indent=2))
    if running:
        subprocess.run(["open", "-a", "Handy"])
    return merged
