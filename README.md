# Ember

Talk about your day. Get it indexed. Ask about it later.

ember is a voice-first personal journal for [Claude Code](https://claude.com/claude-code). At the end of the day you dictate what happened. Claude asks a quick follow-up if something important is unclear, and the entry is saved. In the background, Claude extracts **people, events, purchases, decisions, and mood** into a local SQLite database. Later you just ask:

> When did I last see Jake?
> What did I spend at Target this month?
> Why did I pick the RAV4 over the CR-V?
> When was I stressed about money?

Answers come with dates and cite the entry they came from.

## Features

- **Voice-first.** Pair it with a local push-to-talk dictation app like [Handy](https://github.com/cjpais/Handy).
- **Your words are the source of truth.** Every entry is saved verbatim as a Markdown file. The database can be rebuilt from those files at any time (`ember rebuild`), so better extraction later can be applied to old entries.
- **Structured extraction.** Claude pulls people, events, purchases, and decisions from each entry, resolves relative dates ("yesterday", "last Tuesday"), and matches people to ones it already knows, flagging uncertain matches for review.
- **Three kinds of recall:** structured SQL, keyword search (SQLite FTS5), and search by meaning using local embeddings through [Ollama](https://ollama.com).
- **Family tree.** Link relatives (parent/spouse/sibling/cousin) and render the tree as an HTML page.
- **Local data.** Everything lives in `~/ember/`. Only entry text is sent to Claude for extraction.

## How it works

```
 dictate ──▶ Claude Code chat ──log_entry──▶ ~/ember/raw/*.md   (source of truth)
                                                  │
                              background worker ◀─┘
                              ├─ local: chunk + embed (Ollama, nomic-embed-text)
                              └─ cloud: `claude -p` + JSON schema → people, events, purchases, decisions
                                                  │
                                                  ▼
                                          ~/ember/ember.db (SQLite)
                                                  │
 ask ──▶ Claude Code chat ◀── MCP tools: person_timeline · search · semantic_search · query · get_entry
```

## Requirements

- macOS or Linux, with [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://claude.com/claude-code), logged in. The worker runs `claude -p` on your subscription.
- [Ollama](https://ollama.com) with `nomic-embed-text` (optional; needed only for search by meaning)

## Setup

```sh
git clone https://github.com/pkolisetty/ember.git
cd ember
uv sync
ollama pull nomic-embed-text

# register the MCP server for all your Claude Code sessions
claude mcp add --scope user ember -- "$(which uv)" run --quiet --directory "$PWD" ember serve
```

Start a new Claude Code session, and then talk about your day or ask a question.

## CLI

```sh
uv run ember log "Had coffee with Jake at Blue Bottle."   # log without the chat
uv run ember status                                       # processing status, people needing review
uv run ember reprocess [ids...]                           # re-extract entries (e.g. after a prompt change)
uv run ember rebuild                                      # recreate the database from raw/ files
uv run ember sync-handy                                   # add known names to Handy's custom words
```

## Configuration

| Variable | Default | |
|---|---|---|
| `EMBER_HOME` | `~/ember` | Data directory |
| `EMBER_MODEL` | `opus` | Claude model for extraction |
| `EMBER_EMBED_MODEL` | `nomic-embed-text` | Ollama embedding model |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |

## Data layout

```
~/ember/
  raw/YYYY/MM/*.md     verbatim entries (back these up)
  people.json          confirmed people   (back up; survives rebuilds)
  relations.json       family-tree links  (back up; survives rebuilds)
  ember.db           derived index (rebuildable)
  family_tree.html     generated tree
  logs/worker.log
```

## Roadmap

See [ROADMAP.md](ROADMAP.md).

## License

MIT
