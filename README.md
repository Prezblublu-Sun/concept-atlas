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
```

Bring your own LLM key (OpenAI-compatible endpoint); nothing is sent anywhere until you run an LLM stage, and
every call is metered. Corpus text, databases and keys never enter this repository.

License: MIT.
