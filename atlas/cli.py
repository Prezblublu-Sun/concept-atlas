"""`atlas` command line. Thin: parse → call → print. Increment 1 verbs (ingest agent) + increment 2 verbs
(legacy, curate, build, run match/assign/finish, fixture --kind, ucl subset).

atlas init <root> [--name N] [--doc-type paper|thesis] [--pdf-dir pdf]
atlas docs add <root> [--manifest manifest.csv] [--pattern '**/*.pdf'] [--scan]      register PDFs (no extraction)
atlas docs list <root> [--q text] [--limit 50]
atlas run ingest <root> [--reextract] [--limit N] [--doc-ids a,b] [--workers 1]
atlas run match|assign|finish <root> [--build <id>] [--doc-ids a,b] [--workers N] [--force]
atlas build new|list|status|promote|all <root> [<id>] [--note …] [--workers N]
atlas curate confirm <root> <name> | atlas curate status <root>
atlas legacy export-curated <terms_v3.db> <root> [--spec subdomain_spec.json] [--force]
atlas legacy import-highlights <highlights.sqlite> <sentences.db> <root>
atlas serve <root> [--host 127.0.0.1] [--port 8770]      (a port in use is an error, exit 1)
atlas fixture make <dir> [--seed 1] [--kind paper|thesis]
atlas ucl subset <root> [--inventory csv] [--proximity csv] [--pdf-dir dir] [--target 120]
atlas notes export <root> <collection_id> [-o file.md]
Exit codes: 0 ok, 1 usage/project error, 2 stage reported failures. Prints Report.summary() for stages.
Windows: stdout is reconfigured to UTF-8 (errors='replace').
"""
from __future__ import annotations

import argparse
import json
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
    p.add_argument("--scan", action="store_true",
                   help="also scan pdf_dir for PDFs not in the manifest (default only when there is no manifest)")
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
    p.add_argument("--build", help="build id (default: the newest open build)")
    p.add_argument("--force", action="store_true", help="match: redo documents whose fingerprint is unchanged")

    b = sub.add_parser("build", help="derived builds").add_subparsers(dest="build_cmd", metavar="<verb>")
    b.required = True
    p = b.add_parser("new", help="create an open build: pin the evidence, snapshot curated/")
    p.add_argument("root")
    p.add_argument("--note")
    p = b.add_parser("list", help="list builds")
    p.add_argument("root")
    p = b.add_parser("status", help="manifest, stage items and checks of a build")
    p.add_argument("root")
    p.add_argument("build_id", nargs="?")
    p = b.add_parser("promote", help="make a finished build the current one (builds/current.txt)")
    p.add_argument("root")
    p.add_argument("build_id", nargs="?")
    p = b.add_parser("all", help="new → match → assign → finish → promote")
    p.add_argument("root")
    p.add_argument("--note")
    p.add_argument("--workers", type=int, default=1)

    c = sub.add_parser("curate", help="confirm curated files").add_subparsers(dest="curate_cmd", metavar="<verb>")
    c.required = True
    p = c.add_parser("confirm", help="record the current sha of a curated file as confirmed")
    p.add_argument("root")
    p.add_argument("name", help="concepts | patterns | subdomains | domains | doc_domains | … | all")
    p = c.add_parser("status", help="confirmation state of every curated file")
    p.add_argument("root")

    lg = sub.add_parser("legacy", help="import from the previous term viewer").add_subparsers(dest="legacy_cmd",
                                                                                              metavar="<verb>")
    lg.required = True
    p = lg.add_parser("export-curated", help="terms_v3.db → curated/*.yaml (+ confirmations)")
    p.add_argument("terms_db")
    p.add_argument("root")
    p.add_argument("--spec", help="v2 subdomain_spec.json (cross-group recovery / cross-check; found "
                                  "automatically next to the v3 db or in the sibling v2 folder when omitted)")
    p.add_argument("--force", action="store_true", help="overwrite an existing curated/")
    p = lg.add_parser("import-highlights", help="v3 highlights.sqlite → notes targeting sentences")
    p.add_argument("highlights_db")
    p.add_argument("sentences_db")
    p.add_argument("root")

    p = sub.add_parser("serve", help="start the web UI")
    p.add_argument("root")
    p.add_argument("--host")
    p.add_argument("--port", type=int)

    fx = sub.add_parser("fixture", help="developer tools").add_subparsers(dest="fixture_cmd", metavar="<verb>")
    fx.required = True
    p = fx.add_parser("make", help="write the synthetic fixture corpus")
    p.add_argument("dir")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--kind", choices=("paper", "thesis"), default="paper")

    u = sub.add_parser("ucl", help="UCL thesis subset").add_subparsers(dest="ucl_cmd", metavar="<verb>")
    u.required = True
    p = u.add_parser("subset", help="write manifest.csv / project.yaml / subset_report.md for the UCL thesis subset")
    p.add_argument("root")
    p.add_argument("--inventory")
    p.add_argument("--proximity")
    p.add_argument("--pdf-dir")
    p.add_argument("--target", type=int, default=120)

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
    for w in report.warnings[:200]:
        print(f"  warning: {w}")
    if len(report.warnings) > 200:
        print(f"  … {len(report.warnings) - 200} more warnings")
    for key in ("added", "updated", "unchanged", "missing_files"):
        if key in report.details and isinstance(report.details[key], list):
            print(f"  {key}: {len(report.details[key])}")
    for key in ("seconds", "occurrences_total", "counts_rows", "importance_rows", "unit_subdomains",
                "subdomain_concepts", "sha256"):
        if key in report.details:
            print(f"  {key}: {report.details[key]}")
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
    return _print_report(register_documents(proj, manifest=a.manifest, pattern=a.pattern,
                                            scan=True if a.scan else None))


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
    doc_ids = [s.strip() for s in a.doc_ids.split(",") if s.strip()] if a.doc_ids else None
    opts: dict = {}
    if a.stage == "ingest":
        opts = {"reextract": a.reextract, "limit": a.limit, "workers": a.workers, "doc_ids": doc_ids}
    elif a.stage == "match":
        opts = {"doc_ids": doc_ids, "workers": a.workers, "force": a.force}
    return _print_report(run_stage(proj, a.stage, build=a.build, **opts))


def _cmd_build(a: argparse.Namespace) -> int:
    from atlas.config import load_project
    from atlas.pipeline import build as B
    proj = load_project(a.root)
    if a.build_cmd == "new":
        m = B.new_build(proj, a.note)
        print(f"build {m['build_id']} created: {m.get('docs')} documents, {m.get('sentences')} sentences, "
              f"scheme {m.get('scheme')}")
        return EXIT_OK
    if a.build_cmd == "list":
        rows = B.list_builds(proj)
        print("build_id\tstatus\tcreated\tdocs\tsentences\tcurrent\tnote")
        for m in rows:
            print(f"{m.get('build_id')}\t{m.get('status')}\t{m.get('created')}\t{m.get('docs')}\t{m.get('sentences')}\t"
                  f"{'*' if m.get('current') else ''}\t{m.get('note') or ''}")
        print(f"{len(rows)} builds")
        return EXIT_OK
    if a.build_cmd == "status":
        m = B.build_status(proj, a.build_id)
        print(json.dumps(m, ensure_ascii=False, indent=1, default=str))
        return EXIT_OK
    if a.build_cmd == "promote":
        r = B.promote(proj, a.build_id)
        print(f"build {r['build_id']} promoted (previous: {r['previous'] or '-'})")
        return EXIT_OK
    r = B.build_all(proj, a.note, workers=a.workers)
    print(f"build {r['build_id']} finished and promoted")
    for stage, secs in r["timings"].items():
        rep = r["reports"].get(stage)
        print(f"  {stage:8s} {secs:9.2f} s" + (f"  {rep['summary']}" if rep else ""))
        for w in (rep or {}).get("warnings", [])[:20]:
            print(f"    warning: {w}")
    return EXIT_OK


def _cmd_curate(a: argparse.Namespace) -> int:
    from atlas.config import load_project
    from atlas.pipeline import gates
    proj = load_project(a.root)
    if a.curate_cmd == "confirm":
        if a.name == "all":
            done = gates.confirm_all(proj)
            print("confirmed: " + ", ".join(sorted(done)))
        else:
            r = gates.confirm(proj, a.name)
            print(f"confirmed {a.name}: {r['sha'][:12]} at {r['confirmed_at']}")
        return EXIT_OK
    rows = gates.status(proj)
    print("name\tstate\tsha\tconfirmed_at")
    for r in rows:
        print(f"{r['name']}\t{r['state']}\t{(r['sha'] or '')[:12]}\t{r['confirmed_at'] or ''}")
    return EXIT_OK


def _cmd_legacy(a: argparse.Namespace) -> int:
    from atlas.config import load_project
    from atlas.tools import legacy
    proj = load_project(a.root)
    if a.legacy_cmd == "export-curated":
        r = legacy.export_curated(a.terms_db, proj, spec=a.spec, force=a.force)
        print(legacy.format_export_report(r))
        return EXIT_OK
    r = legacy.import_highlights(a.highlights_db, a.sentences_db, proj)
    print(f"highlights imported: {r['imported']}; unmatched: {len(r['unmatched'])}; statement highlights skipped: "
          f"{r['skipped_statements']}; methods: {r['methods']}")
    for u in r["unmatched"][:20]:
        print(f"  unmatched: {u}")
    return EXIT_OK


def _cmd_serve(a: argparse.Namespace) -> int:
    from atlas.config import load_project
    from atlas.http.server import serve
    proj = load_project(a.root)
    serve(proj, a.host, a.port)
    return EXIT_OK


def _cmd_fixture_make(a: argparse.Namespace) -> int:
    from atlas.tools.fixture import make_fixture
    info = make_fixture(a.dir, a.seed, kind=a.kind)
    print(f"fixture written to {info['out_dir']}: {len(info['documents'])} documents, manifest {info['manifest']}")
    return EXIT_OK


def _cmd_ucl_subset(a: argparse.Namespace) -> int:
    from atlas.tools.ucl_subset import make_subset
    r = make_subset(a.root, inventory=a.inventory, proximity=a.proximity, pdf_dir=a.pdf_dir, target=a.target)
    print(f"UCL subset: {r['n']} theses → {r['manifest']} ({r['report']})")
    for k, v in r.get("per_rule", {}).items():
        print(f"  {k}: {v}")
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
    from atlas.pipeline.build import BuildError
    from atlas.pipeline.gates import GateError
    from atlas.pipeline.runner import StageError
    try:
        if a.cmd == "init":
            return _cmd_init(a)
        if a.cmd == "docs":
            return _cmd_docs_add(a) if a.docs_cmd == "add" else _cmd_docs_list(a)
        if a.cmd == "run":
            return _cmd_run(a)
        if a.cmd == "build":
            return _cmd_build(a)
        if a.cmd == "curate":
            return _cmd_curate(a)
        if a.cmd == "legacy":
            return _cmd_legacy(a)
        if a.cmd == "serve":
            return _cmd_serve(a)
        if a.cmd == "fixture":
            return _cmd_fixture_make(a)
        if a.cmd == "ucl":
            return _cmd_ucl_subset(a)
        if a.cmd == "notes":
            return _cmd_notes_export(a)
        ap.print_usage(sys.stderr)
        return EXIT_USAGE
    except (FileNotFoundError, FileExistsError, KeyError, ValueError, NotImplementedError, OSError, BuildError,
            GateError, StageError) as e:
        log.debug("command failed", exc_info=True)
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
