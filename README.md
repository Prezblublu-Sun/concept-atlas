# concept-atlas

Turn a corpus of research PDFs (journal papers or PhD theses) into a browsable, queryable, citable concept
system. Every derived statement cites the exact sentences it came from; the sentences are stored once, with
stable ids, in an append-only evidence store. Builds are disposable snapshots; your notes survive rebuilds.

The toolkit counts and organises. It does not rate papers.

Status: increment 2 (increment 1 = evidence store, reader with highlights, phrase search, notes; increment 2 =
curated term system matched and counted on builds, thesis doctype with chapter units, UCL thesis subset,
concept / framework / sub-domain views). See `docs/`.

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
# write curated/ — only `curate merge` and `curate confirm` do. --dry-run prints items, tokens and low/high cost
# (live and batch) without any call; --pilot N limits a run (terms/scheme: N documents, concepts/classify: N items);
# a full run of terms / classify needs a passing blind review for the same task + prompt (atlas qa …).
atlas run terms <project-dir> [--dry-run] [--pilot N] [--mode live|batch] [--budget X] [--wait] [--limit N] [--seed S]
atlas run concepts <project-dir> [--draft <terms draft>] …     # rules first, then concepts_merge; delta draft when concepts exist
atlas run scheme <project-dir> …                               # domains / categories / sub-domains draft (gate: concepts)
atlas run classify <project-dir> [--build <id>] [--force] …    # on an open build with assign done (gate: the scheme)
atlas curate merge <project-dir> work/drafts/<stage>/<run>.yaml [--domain DOM_x]   # three-way merge → curated/
atlas qa sample <project-dir> --stage terms|classify [--n 50] [--seed 1] [--run id]  # evalset/<stage>_<run>.xlsx
atlas qa record <project-dir> <xlsx>          # the filled judgement column → evalset/judgments/
atlas qa report <project-dir>                 # accuracy per stage / stratum; the accept gate reads it
```

Bring your own LLM key; nothing is sent anywhere until you run an LLM stage, and every call is metered. With
`ATLAS_LLM_NO_NETWORK=1` in the environment no provider is ever called (the test suite runs that way). Corpus text,
databases and keys never enter this repository.

License: MIT.
