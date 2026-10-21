"""`atlas` command line. Implemented by: increment 1, agent "ingest" (verbs below); thin: parse → call → print.

atlas init <root> [--name N] [--doc-type paper|thesis] [--pdf-dir pdf]
atlas docs add <root> [--manifest manifest.csv] [--pattern '**/*.pdf']     register PDFs (no extraction)
atlas docs list <root> [--q text] [--limit 50]
atlas run ingest <root> [--reextract] [--limit N] [--doc-ids a,b] [--workers 1]
atlas serve <root> [--host 127.0.0.1] [--port 8767]
atlas fixture make <dir> [--seed 1]
atlas notes export <root> <collection_id> [-o file.md]
Exit codes: 0 ok, 1 usage/project error, 2 stage reported failures. Prints Report.summary() for stages.
Windows: stdout is reconfigured to UTF-8 (errors='replace').
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError("increment 1 / ingest")


if __name__ == "__main__":
    sys.exit(main())
