"""`atlas` command line. Implemented by: increment 1, agent "ingest" (verbs below); thin: parse → call → print.

atlas init <root> [--name N] [--doc-type paper|thesis] [--pdf-dir pdf]
atlas docs add <root> [--manifest manifest.csv] [--pattern '**/*.pdf']     register PDFs (no extraction)
atlas docs list <root> [--q text] [--limit 50]
atlas run ingest <root> [--reextract] [--limit N] [--doc-ids a,b] [--workers 1]
atlas serve <root> [--host 127.0.0.1] [--port 8770]      (a port in use is an error, exit 1)
atlas fixture make <dir> [--seed 1]
atlas notes export <root> <collection_id> [-o file.md]
Exit codes: 0 ok, 1 usage/project error, 2 stage reported failures. Prints Report.summary() for stages.
Windows: stdout is reconfigured to UTF-8 (errors='replace').
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from atlas import __version__

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_STAGE_FAILED = 2

log = logging.getLogger(__name__)


def _reconfigure_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="atlas", description="concept-atlas: evidence-first concept system for "
                                                           "research corpora")
    ap.add_argument("--version", action="version", version=f"atlas {__version__}")
    ap.add_argument("-v", "--verbose", action="count", default=0, help="-v: INFO, -vv: DEBUG (stderr)")
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")
    sub.required = True

    p = sub.add_parser("init", help="create a project folder with project.yaml")
    p.add_argument("root")
    p.add_argument("--name")
    p.add_argument("--doc-type", choices=("paper", "thesis"), default="paper")
    p.add_argument("--pdf-dir", default="pdf", help="folder with the PDFs (relative to root or absolute)")
    p.add_argument("--force", action="store_true", help="overwrite an existing project.yaml")

    docs = sub.add_parser("docs", help="register and list documents").add_subparsers(dest="docs_cmd",
                                                                                     metavar="<verb>")
    docs.required = True
    p = docs.add_parser("add", help="register PDFs from manifest.csv and/or pdf_dir (no extraction)")
    p.add_argument("root")
    p.add_argument("--manifest", help="CSV with doc_id,title,year,authors,doi,venue,doc_type,file")
    p.add_argument("--pattern", default="**/*.pdf")
    p = docs.add_parser("list", help="list registered documents")
    p.add_argument("root")
    p.add_argument("--q", help="filter: title/authors/keywords/doc_id")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("run", help="run a pipeline stage")
    from atlas.pipeline.runner import STAGES
    p.add_argument("stage", choices=STAGES)
    p.add_argument("root")
    p.add_argument("--reextract", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--doc-ids", help="comma-separated doc ids")
    p.add_argument("--workers", type=int, default=1)

    p = sub.add_parser("serve", help="start the web UI")
    p.add_argument("root")
    p.add_argument("--host")
    p.add_argument("--port", type=int)

    fx = sub.add_parser("fixture", help="developer tools").add_subparsers(dest="fixture_cmd", metavar="<verb>")
    fx.required = True
    p = fx.add_parser("make", help="write the synthetic fixture corpus")
    p.add_argument("dir")
    p.add_argument("--seed", type=int, default=1)

    notes = sub.add_parser("notes", help="notes and collections").add_subparsers(dest="notes_cmd",
                                                                                 metavar="<verb>")
    notes.required = True
    p = notes.add_parser("export", help="export a collection to Markdown")
    p.add_argument("root")
    p.add_argument("collection_id")
    p.add_argument("-o", "--output", help="write to this file instead of stdout")
    return ap


def _print_report(report) -> int:
    print(report.summary())
    for w in report.warnings:
        print(f"  warning: {w}")
    for key in ("added", "updated", "unchanged", "missing_files"):
        if key in report.details and isinstance(report.details[key], list):
            print(f"  {key}: {len(report.details[key])}")
    return EXIT_STAGE_FAILED if report.items_failed else EXIT_OK


def _cmd_init(a: argparse.Namespace) -> int:
    from atlas.config import init_project
    proj = init_project(a.root, name=a.name, doc_type=a.doc_type, pdf_dir=a.pdf_dir, force=a.force)
    print(f"project '{proj.name}' created at {proj.root}")
    print(f"  pdf_dir: {proj.pdf_dir}")
    return EXIT_OK


def _cmd_docs_add(a: argparse.Namespace) -> int:
    from atlas.config import load_project
    from atlas.pipeline.stages.ingest import register_documents
    proj = load_project(a.root)
    return _print_report(register_documents(proj, manifest=a.manifest, pattern=a.pattern))


def _cmd_docs_list(a: argparse.Namespace) -> int:
    from atlas.config import load_project
    from atlas.store.evidence import EvidenceStore
    proj = load_project(a.root)
    store = EvidenceStore.open(proj.evidence_db, "rw")
    try:
        res = store.list_documents(q=a.q, limit=a.limit)
    finally:
        store.close()
    print("doc_id\tyear\tpages\tsentences\ttitle\tauthors")
    for d in res["rows"]:
        print(f"{d['doc_id']}\t{d.get('year') or ''}\t{d.get('n_pages', 0)}\t{d.get('n_sentences', 0)}\t"
              f"{d.get('title') or ''}\t{d.get('authors') or ''}")
    print(f"{len(res['rows'])} of {res['total']} documents")
    return EXIT_OK


def _cmd_run(a: argparse.Namespace) -> int:
    from atlas.config import load_project
    from atlas.pipeline.runner import run_stage
    proj = load_project(a.root)
    opts: dict = {}
    if a.stage == "ingest":
        opts = {"reextract": a.reextract, "limit": a.limit, "workers": a.workers,
                "doc_ids": [s.strip() for s in a.doc_ids.split(",") if s.strip()] if a.doc_ids else None}
    return _print_report(run_stage(proj, a.stage, **opts))


def _cmd_serve(a: argparse.Namespace) -> int:
    from atlas.config import load_project
    from atlas.http.server import serve
    proj = load_project(a.root)
    serve(proj, a.host, a.port)
    return EXIT_OK


def _cmd_fixture_make(a: argparse.Namespace) -> int:
    from atlas.tools.fixture import make_fixture
    info = make_fixture(a.dir, a.seed)
    print(f"fixture written to {info['out_dir']}: {len(info['documents'])} documents, manifest {info['manifest']}")
    return EXIT_OK


def _cmd_notes_export(a: argparse.Namespace) -> int:
    from atlas.config import load_project
    from atlas.service.api import AtlasService
    proj = load_project(a.root)
    md = AtlasService(proj).export_collection_md(a.collection_id)
    if a.output:
        out = Path(a.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"written {out}")
    else:
        print(md)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    _reconfigure_streams()
    ap = build_parser()
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:          # argparse exits 2 on usage errors, 0 on --help/--version
        return EXIT_USAGE if e.code else EXIT_OK
    level = logging.WARNING - 10 * min(int(a.verbose), 2)
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        if a.cmd == "init":
            return _cmd_init(a)
        if a.cmd == "docs":
            return _cmd_docs_add(a) if a.docs_cmd == "add" else _cmd_docs_list(a)
        if a.cmd == "run":
            return _cmd_run(a)
        if a.cmd == "serve":
            return _cmd_serve(a)
        if a.cmd == "fixture":
            return _cmd_fixture_make(a)
        if a.cmd == "notes":
            return _cmd_notes_export(a)
        ap.print_usage(sys.stderr)
        return EXIT_USAGE
    except (FileNotFoundError, FileExistsError, KeyError, ValueError, NotImplementedError, OSError) as e:
        log.debug("command failed", exc_info=True)
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
