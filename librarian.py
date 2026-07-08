#!/usr/bin/env python3
"""Librarian — local document discovery. Ask for a file, find the file.

No compiled wiki, no LLM, no cloud — one SQLite FTS5 index of filename +
path + text-content head, refreshed by cron or on demand. Point an agent
tool (e.g. FrankenClaw's `file_find`) or your shell at the index and a
fuzzy description ("the spec about X") comes back as ranked real paths.

Usage:
  librarian.py index                # incremental refresh (cron this)
  librarian.py index --full         # drop and rebuild from scratch
  librarian.py find "quarterly report draft"      # ranked matches
  librarian.py find "deploy script" -n 5 --json   # JSON for tool callers
  librarian.py status               # index size + freshness

What gets indexed: every file in the visible (non-hidden) trees under your
home directory, minus dev/cache noise. Text-ish files (+ PDF via pdftotext,
docx via its zip xml) also get a content head so "the doc about X" works,
not just filenames. Secrets (key/credential/env/db files) never enter the
index, not even by name, and hidden-file content is never read.

Optional config at ~/.librarian/config.json:
  {
    "roots": ["~/work", "~/notes"],
    "hidden_allowlist": [
      [".myagent/workspace", true],
      [".myscheduler", false]
    ]
  }

"roots" replaces the default home-directory walk with an explicit list.
"hidden_allowlist" adds hidden dirs (skipped by default) that hold real
knowledge; the second field says whether file CONTENT may be indexed there —
use false for dirs whose config may carry credentials or webhook URLs, so
files stay findable by name only.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HOME = Path.home()
DB_DIR = HOME / ".librarian"
DB_PATH = DB_DIR / "index.sqlite3"
CONFIG_PATH = DB_DIR / "config.json"

# Directory names never descended into, wherever they appear.
PRUNE_DIRS = {
    "node_modules", ".git", "__pycache__", ".cache", "snap",
    "google-cloud-sdk", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "logs",  # runtime logs are not documents; forensics uses the logs directly
}
PRUNE_DIR_PATTERNS = re.compile(r".*venv.*|\.tox|site-packages")

# Files never indexed, not even by name (secrets stay out entirely).
# Every alternative is anchored to the BASENAME — a path segment upstream
# must not exclude ordinary documents below it.
EXCLUDE_FILES = re.compile(
    r"/(keys\.json|auth\.json|credentials[^/]*\.json|\.env(\.[^/]*)?"
    r"|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?"
    r"|[^/]*\.(pem|key)|[^/]*_key[^/]*\.txt|[^/]*token[^/]*\.(txt|json)"
    r"|[^/]*\.(sqlite3?|db)(-wal|-shm)?)$",
    re.IGNORECASE,
)

TEXT_EXTS = {
    "md", "txt", "py", "sh", "bash", "js", "ts", "tsx", "jsx", "json",
    "yaml", "yml", "toml", "ini", "conf", "cfg", "service", "html", "htm",
    "css", "liquid", "csv", "xml", "patch", "diff", "rst", "tex", "env",
    "example", "spec", "info", "install",
}
CONTENT_CAP = 16_384  # bytes of content head per file
PDF_PAGE_CAP = 4


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"librarian: unreadable config {CONFIG_PATH}: {e}",
              file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(cfg, dict):
        print(f"librarian: config {CONFIG_PATH} must be a JSON object",
              file=sys.stderr)
        raise SystemExit(2)
    return cfg


def visible_roots():
    cfg = load_config()
    if cfg.get("roots"):
        if (not isinstance(cfg["roots"], list)
                or not all(isinstance(r, str) for r in cfg["roots"])):
            print('librarian: config "roots" must be a list of paths',
                  file=sys.stderr)
            raise SystemExit(2)
        roots = [(Path(r).expanduser(), True) for r in cfg["roots"]]
        # A vanished root must fail loud: an incremental pass that can't see
        # a root would purge all its files from the index as "removed".
        missing = [str(p) for p, _ in roots if not p.exists()]
        if missing:
            print(f"librarian: config roots missing: {', '.join(missing)}",
                  file=sys.stderr)
            raise SystemExit(2)
    else:
        roots = [(p, True) for p in HOME.iterdir()
                 if not p.name.startswith(".") and not p.is_symlink()]
    for entry in cfg.get("hidden_allowlist", []):
        if not (isinstance(entry, list) and len(entry) == 2
                and isinstance(entry[0], str)):
            print('librarian: hidden_allowlist entries must be '
                  f'["path", bool] — got {entry!r}', file=sys.stderr)
            raise SystemExit(2)
        rel, content_ok = entry[0], bool(entry[1])
        p = Path(rel).expanduser()
        if not p.is_absolute():
            p = HOME / rel
        if p.exists():
            roots.append((p, content_ok))
    return roots


def walk(roots):
    """Yield (path, size, mtime, content_ok) for every indexable file."""
    for root, root_content_ok in roots:
        if root.is_file():
            yield from _stat_one(root, root_content_ok)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in PRUNE_DIRS
                and not PRUNE_DIR_PATTERNS.fullmatch(d)
                and not d.startswith(".")
            ]
            for fn in filenames:
                # Hidden files (.env & co) are findable by name only —
                # their content never enters the index.
                content_ok = root_content_ok and not fn.startswith(".")
                yield from _stat_one(Path(dirpath) / fn, content_ok)


def _stat_one(p, content_ok):
    if EXCLUDE_FILES.search(str(p)):
        return
    try:
        if p.is_symlink():
            return
        st = p.stat()
    except OSError:
        return
    yield str(p), st.st_size, st.st_mtime, content_ok


_extract_errors = 0


def extract_content(path, ext, size):
    """Best-effort text head for content search. Empty string = names only."""
    global _extract_errors
    try:
        if ext in TEXT_EXTS or (ext == "" and size < 512 * 1024):
            with open(path, "rb") as f:
                head = f.read(CONTENT_CAP)
            if b"\x00" in head:  # binary masquerading as text
                return ""
            return head.decode("utf-8", errors="ignore")
        if ext == "pdf":
            out = subprocess.run(
                ["pdftotext", "-l", str(PDF_PAGE_CAP), path, "-"],
                capture_output=True, timeout=20)
            return out.stdout[:CONTENT_CAP].decode("utf-8", errors="ignore")
        if ext == "docx":
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml")[:CONTENT_CAP * 4]
            return re.sub(rb"<[^>]+>", b" ", xml)[:CONTENT_CAP].decode(
                "utf-8", errors="ignore")
    except Exception:
        _extract_errors += 1  # surfaced in the index summary line
    return ""


def open_db():
    """Writer connection (index path only) — creates schema, WAL mode."""
    DB_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS files(
            id INTEGER PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            ext TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
            name, path, content, tokenize='porter unicode61'
        );
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    """)
    return con


def open_db_ro():
    """Read-only connection for find/status — never blocks the indexer,
    never creates schema. Exits loudly if the index doesn't exist."""
    if not DB_PATH.exists():
        print(f"librarian: no index at {DB_PATH} — run `librarian.py index`",
              file=sys.stderr)
        raise SystemExit(2)
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15)
    return con


def cmd_index(full=False):
    t0 = time.time()
    con = open_db()
    if full:
        con.executescript("DELETE FROM files; DELETE FROM fts;")
    known = {p: (i, sz, mt) for i, p, sz, mt in
             con.execute("SELECT id, path, size, mtime FROM files")}
    seen, added, updated = set(), 0, 0
    for path, size, mtime, content_ok in walk(visible_roots()):
        seen.add(path)
        prior = known.get(path)
        if prior and prior[1] == size and abs(prior[2] - mtime) < 1e-6:
            continue
        name = os.path.basename(path)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        content = extract_content(path, ext, size) if content_ok else ""
        if prior:
            fid = prior[0]
            con.execute("UPDATE files SET size=?, mtime=? WHERE id=?",
                        (size, mtime, fid))
            con.execute("DELETE FROM fts WHERE rowid=?", (fid,))
            updated += 1
        else:
            fid = con.execute(
                "INSERT INTO files(path, name, ext, size, mtime) VALUES(?,?,?,?,?)",
                (path, name, ext, size, mtime)).lastrowid
            added += 1
        con.execute(
            "INSERT INTO fts(rowid, name, path, content) VALUES(?,?,?,?)",
            (fid, name, path, content))
    removed = 0
    for path, (fid, _, _) in known.items():
        if path not in seen:
            con.execute("DELETE FROM files WHERE id=?", (fid,))
            con.execute("DELETE FROM fts WHERE rowid=?", (fid,))
            removed += 1
    con.execute("INSERT OR REPLACE INTO meta VALUES('last_index', ?)",
                (time.time(),))
    con.commit()
    con.close()
    err_note = f" · {_extract_errors} extraction errors" if _extract_errors else ""
    print(f"indexed {len(seen)} files (+{added} ~{updated} -{removed}) "
          f"in {time.time() - t0:.1f}s{err_note}")
    if _extract_errors > 50:  # systemic (e.g. pdftotext gone) — page via cron
        raise SystemExit(1)


def _match_expr(query, require_all):
    terms = re.findall(r"[A-Za-z0-9_]+", query)
    if not terms:
        return None
    joiner = " " if require_all else " OR "
    return joiner.join(f'"{t}"*' for t in terms)


def cmd_find(query, limit, as_json):
    con = open_db_ro()
    rows, last_err = [], None
    for require_all in (True, False):
        expr = _match_expr(query, require_all)
        if expr is None:
            break
        try:
            rows = con.execute(
                """SELECT f.path, bm25(fts, 8.0, 4.0, 1.0) AS score,
                          snippet(fts, 2, '>>', '<<', ' … ', 12)
                   FROM fts JOIN files f ON f.id = fts.rowid
                   WHERE fts MATCH ? ORDER BY score LIMIT ?""",
                (expr, limit)).fetchall()
            last_err = None
        except sqlite3.OperationalError as e:
            rows, last_err = [], e  # broken/locked index ≠ "no matches"
        if len(rows) >= 3:  # AND pass good enough; else retry with OR
            break
    con.close()
    if last_err is not None:
        print(f"librarian: query failed: {last_err}", file=sys.stderr)
        raise SystemExit(2)
    results = [{"path": p, "score": round(s, 2),
                "snippet": snip.replace("\n", " ").strip()}
               for p, s, snip in rows]
    if as_json:
        print(json.dumps(results, indent=1))
    elif not results:
        print("no matches")
    else:
        for r in results:
            print(f"{r['path']}\n    {r['snippet']}")


def cmd_status():
    con = open_db_ro()
    n = con.execute("SELECT count(*) FROM files").fetchone()[0]
    last = con.execute(
        "SELECT value FROM meta WHERE key='last_index'").fetchone()
    con.close()
    when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(float(last[0])))
            if last else "never")
    size_mb = DB_PATH.stat().st_size / 1e6 if DB_PATH.exists() else 0
    print(f"{n} files indexed · last index {when} · db {size_mb:.0f} MB")


def main():
    ap = argparse.ArgumentParser(
        description="Librarian — ask for a file, find the file.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_idx = sub.add_parser("index")
    p_idx.add_argument("--full", action="store_true")
    p_find = sub.add_parser("find")
    p_find.add_argument("query")
    p_find.add_argument("-n", type=int, default=10)
    p_find.add_argument("--json", action="store_true")
    sub.add_parser("status")
    args = ap.parse_args()
    if args.cmd == "index":
        cmd_index(full=args.full)
    elif args.cmd == "find":
        cmd_find(args.query, args.n, args.json)
    else:
        cmd_status()


if __name__ == "__main__":
    main()
