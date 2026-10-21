# concept-atlas

Turn a corpus of research PDFs (journal papers or PhD theses) into a browsable, queryable, citable concept
system. Every derived statement cites the exact sentences it came from; the sentences are stored once, with
stable ids, in an append-only evidence store. Builds are disposable snapshots; your notes survive rebuilds.

The toolkit counts and organises. It does not rate papers.

Status: increment 1 (evidence store, reader with highlights, phrase search, notes). See `docs/`.

```
pip install -e .[dev]
atlas init <project-dir> --name myproject --pdf-dir <folder-with-pdfs>
atlas docs add <project-dir>            # register PDFs (manifest.csv optional)
atlas run ingest <project-dir>          # pages → sentences with stable ids
atlas serve <project-dir>               # http://127.0.0.1:8767
```

Bring your own LLM key (OpenAI-compatible endpoint); nothing is sent anywhere until you run an LLM stage, and
every call is metered. Corpus text, databases and keys never enter this repository.

License: MIT.
