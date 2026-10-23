# concept-atlas

Turn a corpus of research PDFs (journal papers or PhD theses) into a browsable, queryable, citable concept
system. Every derived statement cites the exact sentences it came from; the sentences are stored once, with
stable ids, in an append-only evidence store. Builds are disposable snapshots; your notes survive rebuilds.

The toolkit counts and organises. It does not rate papers.

Status: increment 4 built, pilot not yet run (increment 1 = evidence store, reader with highlights, phrase search,
notes; increment 2 = curated term system matched and counted on builds, thesis doctype with chapter units, UCL
thesis subset, concept / framework / sub-domain views; increment 4 = the LLM layer and the curation stages
`terms` → `concepts` → `scheme` → `classify` with drafts, gates, blind review, ledger and budgets — every test and
every dry run so far ran without a model call). See `docs/`.

```
pip install -e .[dev]
atlas init <project-dir> --name myproject --pdf-dir <folder-with-pdfs>
atlas docs add <project-dir>            # register PDFs (manifest.csv optional)
atlas run ingest <project-dir>          # pages → sentences with stable ids (paper or thesis doctype)
atlas serve <project-dir> [--no-prewarm]   # http://127.0.0.1:8770 (prewarm: the DB files are read once at start)
atlas evidence gc <project-dir> [--vacuum] [--dry-run]   # drop superseded extractions no build / note uses

# curated term system (concepts, term tree, categories, sub-domains) → counted on a build
atlas legacy export-curated <terms_v3.db> <project-dir> [--spec subdomain_spec.json] [--force]
atlas legacy import-highlights <highlights.sqlite> <sentences.db> <project-dir>
atlas curate status <project-dir>       # sha check of curated/ vs confirmations.json
atlas curate confirm <project-dir> <name>
atlas build all <project-dir> [--note …] [--workers N]   # new → match → assign → finish → promote
atlas build new|list|status|promote <project-dir> [<build-id>]
atlas build upgrade <project-dir> [<build-id>]   # a finished build of an older schema → current schema in place (seconds)
atlas run match|assign|finish <project-dir> [--build <id>]

# fixtures and the UCL thesis subset
atlas fixture make --kind paper|thesis <dir>
atlas ucl subset <ucl-root> [--inventory …] [--proximity …] [--pdf-dir …] [--target N]

# LLM curation stages (increment 4): drafts in work/drafts/<stage>/<run>.yaml (+ <run>.report.md); stages never
# write curated/ — only `curate merge` and `curate confirm` do. --dry-run prints items, known cache hits, tokens and
# low/high cost (live and batch, per provider of the split) without any call and without creating a file;
# --pilot N limits a run (terms/scheme: N documents, concepts/classify: N items); --budget X stops before the high
# estimate exceeds X; --model M uses another priced model for the primary provider (a different request hash);
# a full run of terms / classify needs a passing blind review for the same task + prompt (atlas qa …). Every call
# is metered in llm/llm.sqlite under the run id <stage>_<run> (`atlas cost report --run terms_r2026-10-23_001`),
# an identical request is answered from the ledger (cache_local, free), and the run's settings are snapshotted in
# work/runs/<stage>_<run>/llm_settings.json. --mode batch submits Message Batches and returns; run again with
# --wait to collect them into the same draft.
atlas run terms <project-dir> [--dry-run] [--pilot N] [--mode live|batch] [--budget X] [--wait] [--limit N] [--seed S] [--model M]
atlas run concepts <project-dir> [--draft <terms draft>] …     # rules first, then concepts_merge; delta draft when concepts exist
atlas run scheme <project-dir> …                               # domains / categories / sub-domains draft (gate: concepts)
atlas run classify <project-dir> [--build <id>] [--force] …    # on an open build with assign done (gate: the scheme)
atlas curate merge <project-dir> work/drafts/<stage>/<run>.yaml [--domain DOM_x]   # three-way merge → curated/
atlas qa sample <project-dir> --stage terms|classify [--n 50] [--seed 1] [--run id]  # evalset/<stage>_<run>.xlsx
atlas qa record <project-dir> <xlsx>          # the filled judgement column → evalset/judgments/
atlas qa report <project-dir>                 # accuracy per stage / stratum; the accept gate reads it

# LLM layer (increment 4): provider, ledger, batches — every call is metered in <project>/llm/llm.sqlite
atlas llm ping <project-dir> [--effort low]        # the only command that spends by itself: one 20-token request
atlas llm batches list|status|collect <project-dir> [<batch-id>] [--wait]   # Message Batches (half price)
atlas cost report <project-dir> [--run <id>] [--stage <s>]   # ledger totals, cache hit rate, unknown pending spend
atlas cost calibrate <project-dir>                 # refresh per-item cost medians (unit_costs) from the ledger
```

Bring your own LLM key: the Anthropic API by default, read from the environment variable `RS_ANTHROPIC_API_KEY`
(deliberately not `ANTHROPIC_API_KEY`; on Windows the user / system environment in the registry is consulted
too, so a freshly set variable works without restarting the host app). `llm:` in `project.yaml` selects the
models, effort, budgets and the provider split (`split: {anthropic: 0.9, deepseek: 0.1}` sends a deterministic
share of items to DeepSeek — key `RS_DEEPSEEK_API_KEY` — so the two can be compared; a stage refuses to start when a
provider of the split has no key, so either set both keys or set `split: {anthropic: 1}`; every ledger row names
its provider and every run writes `work/runs/<run_id>/llm_settings.json`). Nothing is sent anywhere until you run
an LLM stage or `atlas llm ping`, every call is metered, dry runs (`--dry-run`) never call a provider, and setting
`ATLAS_LLM_NO_NETWORK=1` makes every provider refuse (the test suite runs that way). Corpus text, databases and
keys never enter this repository.

License: MIT.
