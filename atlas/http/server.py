"""ThreadingHTTPServer adapter. Implemented by: increment 1, agent "service+http".

- `serve(project, host, port)` builds one AtlasService, serves routes.py + static files, logs to
  <project>/work/logs/serve.log (RotatingFileHandler, 5 MB × 3) and prints the URL once.
- Explicit MIME table (Windows registry maps .mjs/.json wrongly). Static files get `?v=<atlas.__version__>`
  cache-busting from the SPA; the server sends `Cache-Control: no-cache` for them anyway.
- Only JSON bodies; body size cap 20 MB; every handler wrapped: exceptions → 500 {"error"}; KeyError/None → 404.
- No auth (single user, loopback or tailnet).
"""
from __future__ import annotations

from atlas.config import Project


def serve(project: Project, host: str | None = None, port: int | None = None) -> None:
    raise NotImplementedError("increment 1 / service+http")
