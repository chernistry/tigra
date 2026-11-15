"""Command line: a LinkedIn data export in, one offline HTML page out."""

import argparse
import csv
import json
import os
import sys
import webbrowser
from pathlib import Path

import duckdb

from tigra import NAME, __version__
from tigra.export import Export, ExportError
from tigra.load import load_export, read_aliases
from tigra.model import build_model
from tigra.viz import render_html, write_private


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=NAME,
        description="Turn your LinkedIn data export into an offline, interactive graph of your network. "
        "Everything stays on your machine. Not affiliated with LinkedIn.",
    )
    parser.add_argument("--version", action="version", version=f"{NAME} {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Build graph.html from your export.")
    build.add_argument("export", type=Path, help="The export ZIP LinkedIn sent you, or the folder it unpacks to.")
    build.add_argument("--out", type=Path, default=Path(f"{NAME}-out"), help=f"Output folder (default: ./{NAME}-out).")
    build.add_argument("--aliases", type=Path, help="CSV with columns variant,canonical that merges company names.")
    build.add_argument("--seed", type=int, default=42, help="Seed for community detection (default: 42).")
    build.add_argument("--db", type=Path, help="Also save the DuckDB database to this new file (default: memory only).")
    build.add_argument("--open", action="store_true", help="Open the page in your browser when it is ready.")
    args = parser.parse_args(argv)
    return _build(args)


def _build(args: argparse.Namespace) -> int:
    os.umask(0o077)  # every output holds your contacts' data: readable by you only
    if args.db and args.db.exists():
        return _fail(f"{args.db} already exists; give a new file, so contacts who left are not kept.")
    try:
        aliases = read_aliases(args.aliases) if args.aliases else None
        with Export(args.export) as export:
            con = duckdb.connect(str(args.db) if args.db else ":memory:")
            try:
                load_export(con, export, aliases=aliases)
                model = build_model(con, seed=args.seed)
            finally:
                con.close()
    except (ExportError, FileNotFoundError, ValueError, csv.Error) as e:
        return _fail(str(e))
    page = args.out / "graph.html"
    write_private(args.out / "graph.json", json.dumps(model, ensure_ascii=False, indent=1))
    write_private(page, render_html(model))
    meta = model["meta"]
    print(
        f"{meta['contacts']} contacts · {meta['communities']} communities · {meta['with_dm']} you messaged · "
        f"{meta['cohorts']} connection bursts"
    )
    print(f"Open {page.resolve()}")
    if args.open:
        webbrowser.open(page.resolve().as_uri())
    return 0


def _fail(message: str) -> int:
    print(f"{NAME}: {message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
