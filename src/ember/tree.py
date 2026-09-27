"""Render the family tree (people + relations) as a local HTML page using Mermaid."""

import html
import sqlite3

from . import db

TREE_PATH = db.HOME / "family_tree.html"

EDGES = {
    "parent": "{a} --> {b}",
    "spouse": "{a} === {b}",
    "partner": "{a} -. partner .- {b}",
    "sibling": "{a} -. sibling .- {b}",
    "cousin": "{a} -. cousin .- {b}",
}


def mermaid(conn: sqlite3.Connection) -> str:
    relations = conn.execute("SELECT a, kind, b FROM relations").fetchall()
    ids = {r["a"] for r in relations} | {r["b"] for r in relations}
    lines = ["flowchart TD"]
    for p in conn.execute(f"SELECT * FROM people WHERE id IN ({','.join('?' * len(ids))})", sorted(ids)):
        label = html.escape(p["name"]).replace('"', "&quot;")
        if p["relationship"] and p["relationship"] != "self":
            label += f"<br/><small>{html.escape(p['relationship'])}</small>".replace('"', "&quot;")
        lines.append(f'  p{p["id"]}["{label}"]')
        if p["relationship"] == "self":
            lines.append(f"  class p{p['id']} me")
    for r in relations:
        lines.append("  " + EDGES[r["kind"]].format(a=f"p{r['a']}", b=f"p{r['b']}"))
    return "\n".join(lines)


def write_html(conn: sqlite3.Connection) -> str:
    graph = mermaid(conn).replace("flowchart TD", "flowchart TD\n  classDef me stroke-width:3px", 1)
    TREE_PATH.write_text(f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Family Tree</title>
<style>
  :root {{ --bg: #fafaf8; --fg: #1d1d1b; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg: #1b1b1a; --fg: #ececea; }} }}
  body {{ background: var(--bg); color: var(--fg); font-family: system-ui, sans-serif; margin: 0; padding: 24px 16px; }}
  h1 {{ font-size: 20px; font-weight: 600; margin: 0 0 16px; }}
  .mermaid {{ overflow-x: auto; }}
</style></head>
<body><h1>Family Tree</h1><pre class="mermaid">
{graph}
</pre>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  mermaid.initialize({{ startOnLoad: true, theme: dark ? "dark" : "neutral", securityLevel: "strict" }});
</script></body></html>
""")
    return str(TREE_PATH)
