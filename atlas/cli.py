"""`atlas` command line. Thin: parse → call → print. Increment 1 verbs (ingest agent) + increment 2 verbs
(legacy, curate, build, run match/assign/finish, fixture --kind, ucl subset).

atlas init <root> [--name N] [--doc-type paper|thesis] [--pdf-dir pdf]
atlas docs add <root> [--manifest manifest.csv] [--pattern '**/*.pdf'] [--scan]      register PDFs (no extraction)
atlas docs list <root> [--q text] [--limit 50]
atlas run ingest <root> [--reextract] [--limit N] [--doc-ids a,b] [--workers 1]
atlas run match|assign|finish <root> [--build <id>] [--doc-ids a,b] [--workers N] [--force]
atlas run terms|concepts|scheme|classify <root> [--dry-run] [--pilot N] [--mode live|batch] [--budget X] [--wait]
                                               [--limit N] [--seed S] [--model M] [--draft file] [--doc-ids …]
                                               [--concept-ids …] [--build <id>] [--force]   LLM stages (increment 4)
atlas build new|list|status|promote|all|upgrade <root> [<id>] [--note …] [--workers N]
atlas curate confirm <root> <name> | atlas curate status <root> | atlas curate merge <root> <draft> [--domain DOM_x]
atlas qa sample <root> --stage terms|classify [--n 50] [--seed 1] [--run id]   blind-review sheet → evalset/
atlas qa record <root> <xlsx> | atlas qa report <root>
atlas legacy export-curated <terms_v3.db> <root> [--spec subdomain_spec.json] [--force]
atlas legacy import-highlights <highlights.sqlite> <sentences.db> <root>
atlas serve <root> [--host 127.0.0.1] [--port 8770] [--no-prewarm]   (a port in use is an error, exit 1)
atlas evidence gc <root> [--vacuum] [--dry-run] [--rebuild-fts]      remove superseded extractions
atlas fixture make <dir> [--seed 1] [--kind paper|thesis]
atlas ucl subset <root> [--inventory csv] [--proximity csv] [--pdf-dir dir] [--target 120]
atlas notes export <root> <collection_id> [-o file.md]
atlas llm ping <root> [--effort low]      the only spending command: one 20-token request (refused under ATLAS_LLM_NO_NETWORK)
atlas llm batches list|status|collect <root> [<batch_id>]
atlas cost report <root> [--run id] [--stage s] | atlas cost calibrate <root>
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
    p.add_argument("--force", action="store_true", help="match / classify: redo items whose fingerprint is unchanged")
    g = p.add_argument_group("LLM stages (terms, concepts, scheme, classify)")
    g.add_argument("--dry-run", action="store_true", help="print the plan (items, tokens, low/high cost) and make no call")
    g.add_argument("--pilot", type=int, metavar="N", help="terms/scheme: N documents; concepts/classify: N items")
    g.add_argument("--mode", choices=("live", "batch"), help="live calls or one Message Batch (default: llm.batch_default)")
    g.add_argument("--budget", type=float, metavar="X", help="stop before the high estimate exceeds X (provider currency)")
    g.add_argument("--wait", action="store_true", help="batch mode: poll until the batch ended, then finish the draft")
    g.add_argument("--seed", type=int, help="sampling seed (terms)")
    g.add_argument("--model", help="model for this run / estimate (default: llm.model, else claude-opus-5-5)")
    g.add_argument("--draft", help="concepts: a terms draft to take candidates from")
    g.add_argument("--concept-ids", help="classify: comma-separated concept ids")

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
    p = b.add_parser("upgrade", help="bring a finished build of an older schema to the current one in place (seconds)")
    p.add_argument("root")
    p.add_argument("build_id", nargs="?", help="default: the promoted build")

    c = sub.add_parser("curate", help="confirm curated files").add_subparsers(dest="curate_cmd", metavar="<verb>")
    c.required = True
    p = c.add_parser("confirm", help="record the current sha of a curated file as confirmed")
    p.add_argument("root")
    p.add_argument("name", help="concepts | patterns | subdomains | domains | doc_domains | … | all")
    p = c.add_parser("status", help="confirmation state of every curated file")
    p.add_argument("root")
    p = c.add_parser("merge", help="three-way merge of a stage draft (work/drafts/<stage>/<run>.yaml) into curated/")
    p.add_argument("root")
    p.add_argument("draft")
    p.add_argument("--domain", help="terms drafts: the domain id the terms are placed under (default: first scope domain)")

    qa = sub.add_parser("qa", help="blind review of LLM drafts (the accept gate)").add_subparsers(dest="qa_cmd", metavar="<verb>")
    qa.required = True
    p = qa.add_parser("sample", help="write evalset/<stage>_<run>.xlsx (+ .jsonl) with an empty judgement column")
    p.add_argument("root")
    p.add_argument("--stage", choices=("terms", "classify"), required=True)
    p.add_argument("--n", type=int, default=50)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--run", help="draft run id (default: the newest draft of the stage)")
    p = qa.add_parser("record", help="read the filled sheet into evalset/judgments/")
    p.add_argument("root")
    p.add_argument("xlsx")
    p = qa.add_parser("report", help="accuracy per stage and stratum over every recorded judgment")
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
    p.add_argument("--prewarm", action=argparse.BooleanOptionalAction, default=True,
                   help="read evidence.sqlite and the promoted build.sqlite sequentially at startup so the OS cache "
                        "holds them (default: on; --no-prewarm to skip)")

    ev = sub.add_parser("evidence", help="evidence store maintenance").add_subparsers(dest="evidence_cmd",
                                                                                      metavar="<verb>")
    ev.required = True
    p = ev.add_parser("gc", help="delete extractions no document, build or note uses any more")
    p.add_argument("root")
    p.add_argument("--vacuum", action="store_true", help="VACUUM afterwards and rebuild the FTS indexes (stop `atlas serve` first)")
    p.add_argument("--dry-run", action="store_true", help="report what would be removed, change nothing")
    p.add_argument("--rebuild-fts", action="store_true", help="only rebuild the FTS indexes (recovery after an interrupted --vacuum)")

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

    llm = sub.add_parser("llm", help="provider connectivity and message batches").add_subparsers(dest="llm_cmd",
                                                                                                  metavar="<verb>")
    llm.required = True
    p = llm.add_parser("ping", help="one 20-token request: prints model, usage, cost, request id (spends money)")
    p.add_argument("root")
    p.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"), default="low")
    bt = llm.add_parser("batches", help="message batches of this project").add_subparsers(dest="batches_cmd",
                                                                                          metavar="<verb>")
    bt.required = True
    p = bt.add_parser("list", help="batches known to the ledger")
    p.add_argument("root")
    p = bt.add_parser("status", help="ledger and provider state of one batch")
    p.add_argument("root")
    p.add_argument("batch_id")
    p = bt.add_parser("collect", help="fetch the results of an ended batch into the ledger (idempotent)")
    p.add_argument("root")
    p.add_argument("batch_id")
    p.add_argument("--wait", action="store_true", help="poll until the batch ended, then collect")
    p.add_argument("--poll", type=float, default=30.0, help="seconds between polls with --wait")

    cost = sub.add_parser("cost", help="ledger reports").add_subparsers(dest="cost_cmd", metavar="<verb>")
    cost.required = True
    p = cost.add_parser("report", help="ledger totals, cache hit rate, unknown pending spend")
    p.add_argument("root")
    p.add_argument("--run", help="one run id")
    p.add_argument("--stage", help="one stage")
    p = cost.add_parser("calibrate", help="refresh unit_costs (per-item cost medians) from the ledger")
    p.add_argument("root")
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
    from atlas.pipeline.runner import LLM_STAGES, run_stage
    proj = load_project(a.root)
    doc_ids = [s.strip() for s in a.doc_ids.split(",") if s.strip()] if a.doc_ids else None
    opts: dict = {}
    if a.stage == "ingest":
        opts = {"reextract": a.reextract, "limit": a.limit, "workers": a.workers, "doc_ids": doc_ids}
    elif a.stage == "match":
        opts = {"doc_ids": doc_ids, "workers": a.workers, "force": a.force}
    elif a.stage in LLM_STAGES:
        opts = {"dry_run": a.dry_run, "pilot": a.pilot, "mode": a.mode, "budget": a.budget, "wait": a.wait,
                "limit": a.limit, "seed": a.seed, "model": a.model}
        if a.stage == "terms":
            opts["doc_ids"] = doc_ids
        elif a.stage == "concepts":
            opts["draft"] = a.draft
        elif a.stage == "classify":
            opts["force"] = a.force
            opts["concept_ids"] = [s.strip() for s in a.concept_ids.split(",") if s.strip()] if a.concept_ids else None
    report = run_stage(proj, a.stage, build=a.build, **opts)
    if a.stage in LLM_STAGES:
        from atlas.pipeline.llm_stage import format_plan
        if report.details.get("dry_run"):
            print(format_plan(report.details["plan"]))
            for w in report.warnings[:50]:
                print(f"  warning: {w}")
            return EXIT_OK
        rc = _print_report(report)
        for key in ("run_id", "draft", "report", "batch_id", "cost", "currency"):
            if report.details.get(key) is not None:
                print(f"  {key}: {report.details[key]}")
        return rc
    return _print_report(report)


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
    if a.build_cmd == "upgrade":
        r = B.upgrade(proj, a.build_id)
        if r.get("unchanged"):
            print(f"build {r['build_id']} already has schema {r['to']}; nothing to do")
        else:
            print(f"build {r['build_id']} upgraded: schema {r['from']} -> {r['to']} in {r['seconds']} s "
                  f"(sha256 {r['sha256'][:12]}…); restart `atlas serve` if it is running")
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
    if a.curate_cmd == "merge":
        r = gates.merge_draft(proj, a.draft, domain=a.domain)
        print(gates.format_merge_report(r))
        return EXIT_OK
    rows = gates.status(proj)
    print("name\tstate\tsha\tconfirmed_at")
    for r in rows:
        print(f"{r['name']}\t{r['state']}\t{(r['sha'] or '')[:12]}\t{r['confirmed_at'] or ''}")
    for n in gates.draft_notices(proj):
        print(f"notice: {n}")
    return EXIT_OK


def _cmd_qa(a: argparse.Namespace) -> int:
    from atlas.config import load_project
    from atlas.verify import qa
    proj = load_project(a.root)
    if a.qa_cmd == "sample":
        r = qa.sample(proj, a.stage, n=a.n, seed=a.seed, run_id=a.run)
        print(f"qa sheet: {r['xlsx']} ({r['n']} of {r['total']} items of {a.stage} run {r['run']}, task {r['task']} @ "
              f"{r['prompt_sha'][:12]}); fill the judgement column, then `atlas qa record {a.root} {r['xlsx']}`")
        return EXIT_OK
    if a.qa_cmd == "record":
        r = qa.record(proj, a.xlsx)
        print(f"recorded {r['judged']} judgements of {r['n']} rows ({r['correct']} correct) for {r['stage']} run {r['run']} → {r['jsonl']}")
        for u in r["unparsed"][:20]:
            print(f"  unparsed judgement for {u['key']}: {u['value']!r}")
        return EXIT_OK
    print(qa.format_report(qa.report(proj)))
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
    serve(proj, a.host, a.port, prewarm_files=bool(a.prewarm))
    return EXIT_OK


def _cmd_evidence(a: argparse.Namespace) -> int:
    from atlas.config import load_project
    from atlas.tools import gc
    proj = load_project(a.root)
    if a.rebuild_fts:
        secs = gc.rebuild_fts(proj)
        print(f"FTS indexes rebuilt in {secs:.1f} s")
        return EXIT_OK
    r = gc.run(proj, vacuum=a.vacuum, dry_run=a.dry_run)
    print(gc.format_report(r))
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


def _cmd_llm(a: argparse.Namespace) -> int:
    from atlas.config import load_project
    from atlas.llm import batch as Bt
    from atlas.llm.base import ChatRequest, NetworkForbidden, network_forbidden
    from atlas.llm.client import describe_secret, key_env_of, llm_config, make_client, models_of, primary_provider
    from atlas.llm.ledger import Ledger, new_run_id, price_table, report_unknown_pending
    from atlas.store.llm import LlmStore
    proj = load_project(a.root)
    if a.llm_cmd == "ping" and network_forbidden():
        raise NetworkForbidden("ATLAS_LLM_NO_NETWORK is set: `atlas llm ping` refuses to call the provider")
    store = LlmStore.open(proj.llm_db)
    try:
        if a.llm_cmd == "ping":
            cfg = llm_config(proj)
            provider = primary_provider(cfg)
            client = make_client(proj, provider)
            model = models_of(cfg).get(provider) or str(cfg.get("model") or "")
            print("key: not needed (fake)" if provider == "fake" else describe_secret(key_env_of(cfg, provider)))
            ledger = Ledger(store, price_table(proj), provider=client.provider, currency=client.currency,
                            run_id=new_run_id("ping"), stage="ping")
            req = ChatRequest(model=model, system=("Connectivity check. Reply with the single word OK.",),
                              user="ping", max_tokens=20, effort=a.effort if provider != "deepseek" else None,
                              task="ping", key="ping")
            row = ledger.begin(req, "ping", "", 1, 0.01)
            try:
                resp = client.complete(req)
            except BaseException as e:
                ledger.fail_pending(row, f"{type(e).__name__}: {e}")
                raise
            status = "ok" if resp.ok else ("refused" if resp.stop_reason == "refusal" else "error")
            cost = ledger.finish(row, req, resp, status, validated=resp.ok, prompt_sha="")
            u = resp.usage
            print(f"provider {client.provider}  model {model}  served by {u.model_reported or '-'}")
            print(f"stop_reason {resp.stop_reason}  text {resp.text.strip()[:60]!r}  latency {resp.latency_ms} ms")
            print(f"usage: input {u.input}, cache_write {u.cache_write}, cache_read {u.cache_read}, output {u.output}")
            print(f"cost {cost:.6f} {client.currency}  request id {resp.raw_id or '-'}")
            if resp.error:
                print(f"error: {resp.error}")
            if client.provider == "fake":
                print("provider fake: no network call was made")
            return EXIT_OK if resp.ok else EXIT_STAGE_FAILED
        note = report_unknown_pending(store)
        if note:
            print(f"notice: {note}")
        if a.batches_cmd == "list":
            rows = store.list_batches()
            print("batch_id\tprovider\tstatus\tcreated\tstage\trun_id\tn_requests\tn_done\tn_failed\tcost")
            for b in rows:
                print(f"{b['batch_id']}\t{b['provider']}\t{b['status']}\t{b['created']}\t{b['stage']}\t{b['run_id']}\t"
                      f"{b['n_requests']}\t{b['n_done']}\t{b['n_failed']}\t{b['cost']:.4f}")
            print(f"{len(rows)} batches")
            return EXIT_OK
        meta = store.get_batch(a.batch_id)
        if meta is None:
            raise KeyError(f"batch {a.batch_id} is not in this project's ledger")
        client = make_client(proj, meta["provider"])
        if a.batches_cmd == "status":
            r = Bt.status_report(store, client, a.batch_id)
            print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
            return EXIT_OK
        prices = price_table(proj)
        if a.wait:
            r = Bt.wait_for_batch(store, client, a.batch_id, prices, poll_seconds=a.poll)
        else:
            r = Bt.collect(store, client, a.batch_id, prices)
        print(f"batch {a.batch_id} ({r['provider']}): state {r['state']}, pending before {r['pending_before']}, "
              f"collected {r['collected']}, failed {r['failed']}, missing {r['missing']}, cost {r['cost']:.4f} {client.currency}")
        if r["state"] != "ended":
            print("not ended yet; run the command again later (or with --wait)")
        return EXIT_OK
    finally:
        store.close()


def _cmd_cost(a: argparse.Namespace) -> int:
    from atlas.config import load_project
    from atlas.llm.ledger import report_unknown_pending
    from atlas.llm.tasks import read_settings
    from atlas.store.llm import LlmStore
    proj = load_project(a.root)
    store = LlmStore.open(proj.llm_db)
    try:
        if a.cost_cmd == "calibrate":
            rows = store.calibrate()
            print("task\tprompt_sha\tunit\tcost_median\tn")
            for r in rows:
                print(f"{r['task']}\t{r['prompt_sha'][:12]}\t{r['unit']}\t{r['cost_median']:.6f}\t{r['n']}")
            print(f"{len(rows)} unit costs refreshed")
            return EXIT_OK
        if a.run:
            s = read_settings(proj, a.run)
            if s is None:
                print(f"run {a.run}: no settings snapshot (work/runs/{a.run}/llm_settings.json)")
            else:
                provs = ", ".join(f"{p} {v['model']}" + (f" effort {v['effort']}" if v.get("effort") else "")
                                  for p, v in sorted(s.get("providers", {}).items()))
                split = ", ".join(f"{p} {w:.0%}" for p, w in sorted(s.get("split", {}).items()))
                tasks = ", ".join(f"{n} {v['prompt_sha'][:12]}" for n, v in sorted(s.get("tasks", {}).items()))
                print(f"run {a.run}: stage {s.get('stage')}, mode {s.get('mode')}, created {s.get('created')}")
                print(f"  providers: {provs}; split: {split}; failover {s.get('failover')}; "
                      f"pricing {str(s.get('pricing_sha', ''))[:12]}; max_tokens_cap {s.get('max_tokens_cap')}")
                print(f"  tasks (prompt_sha): {tasks or '-'}")
        t = store.totals(run_id=a.run, stage=a.stage)
        scope = " ".join(s for s in (f"run {a.run}" if a.run else "", f"stage {a.stage}" if a.stage else "") if s)
        cur = t["total"]["currency"] or "-"
        print(f"ledger{' (' + scope + ')' if scope else ''}: {t['total']['rows']} rows, cost {t['total']['cost']:.4f} {cur}")
        for p in t["by_provider"]:
            print(f"  {p['provider'] or '-'}: {p['rows']} rows, in {p['in_tokens']} (+cache write {p['cache_write_tokens']}, "
                  f"read {p['cache_read_tokens']}), out {p['out_tokens']}, cost {p['cost']:.4f} {p['currency']}"
                  + (f", failover rows {p['failover']}" if p["failover"] else ""))
        print("provider\tstage\ttask\trows\tok\tcache_local\tcache_batch\tfailed\tpending\tin_tokens\tcache_write\tcache_read\tout_tokens\tcost\tcurrency")
        for r in t["rows"]:
            s = r["by_status"]
            failed = sum(v for k, v in s.items() if k in ("truncated", "malformed", "refused", "error", "budget_stop"))
            print(f"{r['provider']}\t{r['stage']}\t{r['task']}\t{r['rows']}\t{s.get('ok', 0)}\t{s.get('cache_local', 0)}\t"
                  f"{s.get('cache_batch', 0)}\t{failed}\t{s.get('pending', 0)}\t{r['in_tokens']}\t{r['cache_write_tokens']}\t"
                  f"{r['cache_read_tokens']}\t{r['out_tokens']}\t{r['cost']:.4f}\t{r['currency']}")
        print(f"cache: local hit rate {t['cache_local_rate']:.1%} (rows), prompt cache read ratio {t['cache_read_ratio']:.1%} (tokens)")
        note = report_unknown_pending(store)
        print(f"unknown spend: {note}" if note else "unknown spend: none (no pending rows)")
        return EXIT_OK
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    _reconfigure_streams()
    ap = build_parser()
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:          # argparse exits 2 on usage errors, 0 on --help/--version
        return EXIT_USAGE if e.code else EXIT_OK
    level = logging.WARNING - 10 * min(int(a.verbose), 2)
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    from atlas.llm.base import NetworkForbidden
    from atlas.llm.client import LlmError
    from atlas.llm.governor import BudgetExceeded
    from atlas.pipeline.build import BuildError
    from atlas.pipeline.gates import GateError, MergeError
    from atlas.pipeline.runner import StageError
    from atlas.store.db import BuildSchemaError
    from atlas.tools.gc import GcError
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
        if a.cmd == "qa":
            return _cmd_qa(a)
        if a.cmd == "legacy":
            return _cmd_legacy(a)
        if a.cmd == "serve":
            return _cmd_serve(a)
        if a.cmd == "evidence":
            return _cmd_evidence(a)
        if a.cmd == "fixture":
            return _cmd_fixture_make(a)
        if a.cmd == "ucl":
            return _cmd_ucl_subset(a)
        if a.cmd == "notes":
            return _cmd_notes_export(a)
        if a.cmd == "llm":
            return _cmd_llm(a)
        if a.cmd == "cost":
            return _cmd_cost(a)
        ap.print_usage(sys.stderr)
        return EXIT_USAGE
    except (FileNotFoundError, FileExistsError, KeyError, ValueError, NotImplementedError, OSError, BuildError,
            GateError, MergeError, StageError, BuildSchemaError, GcError, NetworkForbidden, LlmError,
            BudgetExceeded) as e:
        log.debug("command failed", exc_info=True)
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
