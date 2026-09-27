"""ember command line: log, process, reprocess, status, serve."""

import argparse
import sys
from datetime import datetime

from . import db, extract


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(prog="ember")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_log = sub.add_parser("log", help="save an entry (text argument or stdin)")
    p_log.add_argument("text", nargs="?")
    p_log.add_argument("--date", help="YYYY-MM-DD the entry is about (default: today)")
    p_log.add_argument("--wait", action="store_true", help="process now instead of in the background")
    sub.add_parser("process", help="process pending entries")
    p_re = sub.add_parser("reprocess", help="re-extract entries (e.g. after improving the prompt)")
    p_re.add_argument("ids", nargs="*", type=int, help="entry ids (default: all)")
    sub.add_parser("rebuild", help="recreate the database from raw/ files, then re-extract everything")
    sub.add_parser("sync-handy", help="add people's names to Handy's custom words")
    sub.add_parser("status", help="show processing status")
    sub.add_parser("serve", help="run the MCP server (stdio)")
    args = parser.parse_args()

    if args.cmd == "serve":
        from .server import serve
        serve()
    elif args.cmd == "log":
        text = args.text or sys.stdin.read()
        if not text.strip():
            sys.exit("nothing to log")
        with db.connect() as conn:
            entry_id = db.add_entry(conn, text, args.date)
        print(f"saved entry {entry_id}")
        extract.process_pending(log) if args.wait else extract.spawn_worker()
    elif args.cmd == "process":
        log(f"processed {extract.process_pending(log)} entries")
    elif args.cmd == "reprocess":
        with db.connect() as conn:
            where, params = ("WHERE id IN (%s)" % ",".join("?" * len(args.ids)), args.ids) if args.ids else ("", [])
            conn.execute(f"UPDATE entries SET status = 'pending', attempts = 0 {where}", params)
        log(f"processed {extract.process_pending(log)} entries")
    elif args.cmd == "rebuild":
        log(f"restored {db.rebuild()} entries from {db.RAW_DIR}")
        log(f"processed {extract.process_pending(log)} entries")
    elif args.cmd == "sync-handy":
        from .handy import sync
        print("Handy custom words:", ", ".join(sync()))
    elif args.cmd == "status":
        from .server import status
        print(status())


if __name__ == "__main__":
    main()
