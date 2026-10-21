"""ThreadingHTTPServer adapter. Implemented by: increment 1, agent "service+http".

- `serve(project, host, port)` builds one AtlasService, serves routes.py + static files, logs to
  <project>/work/logs/serve.log (RotatingFileHandler, 5 MB × 3) and prints the URL once.
- Explicit MIME table (Windows registry maps .mjs/.json wrongly). Static files get `?v=<atlas.__version__>`
  cache-busting from the SPA; the server sends `Cache-Control: no-cache` for them anyway.
- Only JSON bodies; body size cap 20 MB; every handler wrapped: exceptions → 500 {"error"}; KeyError/None → 404.
- No auth (single user, loopback or tailnet).

`make_server(service, host, port)` returns the (unstarted) ThreadingHTTPServer so tests can run it in a thread on
port 0.
"""
from __future__ import annotations

import inspect
import json
import logging
import logging.handlers
import mimetypes
import re
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import atlas
from atlas.config import Project
from atlas.http.routes import API_PREFIX, BODY_ALIASES, CREATED, match
from atlas.service.api import BUILD, AtlasService

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 20 * 1024 * 1024
MIME: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".wasm": "application/wasm",
    ".map": "application/json; charset=utf-8",
    ".ftl": "text/plain; charset=utf-8",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}
JSON_CT = "application/json; charset=utf-8"
_EXPOSE = "X-Page, X-Pages, X-Hits, X-Hit-Y, X-Atlas-Build"
_INT_PARAMS = {"limit", "offset", "page_no", "context", "rev"}
_FLOAT_PARAMS = {"zoom", "seq"}
_BOOL_PARAMS = {"explicit"}
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
# Parameters without a default in the service contract: which must be present (→ 400) and what the rest get.
REQUIRED: dict[str, tuple[str, ...]] = {
    "search": ("phrase",), "resolve": ("ref",), "notes_for": ("ref",), "create_collection": ("name",),
    "update_note": ("rev",),
}
DEFAULTS: dict[str, dict[str, Any]] = {
    "create_note": {"title": "", "body_md": "", "tags": [], "target_refs": []},
    "update_note": {"title": None, "body_md": None, "tags": None, "explicit": False},
    "add_to_collection": {"ref": None, "comment_md": "", "after_item": None},
}


def _coerce(name: str, value: Any) -> Any:
    """Query strings arrive as str; convert the parameters the service expects as numbers/bools."""
    if value is None or not isinstance(value, str):
        return value
    try:
        if name in _INT_PARAMS:
            return int(value)
        if name in _FLOAT_PARAMS:
            return float(value)
    except ValueError as e:
        raise ValueError(f"parameter {name!r} must be a number") from e
    if name in _BOOL_PARAMS:
        return value.strip().lower() in ("1", "true", "yes", "on")
    return value


def _parse_spans(s: str | None) -> list[tuple[int, int]] | None:
    """'12-40,100-130' → [(12, 40), (100, 130)]; empty → None."""
    if not s:
        return None
    out: list[tuple[int, int]] = []
    for part in str(s).split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        try:
            x, y = int(a), int(b)
        except ValueError as e:
            raise ValueError(f"bad spans value {s!r}") from e
        if y > x:
            out.append((x, y))
    return out or None


def _content_disposition(kind: str, filename: str) -> str:
    ascii_name = _SAFE_FILENAME.sub("_", filename) or "file"
    quoted = urllib.parse.quote(filename, safe="")
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


class AtlasHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"concept-atlas/{atlas.__version__}"
    service: AtlasService  # set on the server; read through self.server

    # ---- plumbing -------------------------------------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: D401 - BaseHTTPRequestHandler API
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Atlas-Build", BUILD)
        hdrs = dict(headers or {})
        cache = hdrs.pop("Cache-Control", None)
        if cache is None:
            cache = "max-age=600" if ctype in ("image/png", "application/pdf") else "no-cache"
        self.send_header("Cache-Control", cache)
        self.send_header("Access-Control-Expose-Headers", _EXPOSE)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, JSON_CT, headers)

    def _error(self, code: int, msg: str) -> None:
        self._json(code, {"error": msg, "status": code})

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.close_connection = True
            raise _HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"body larger than {MAX_BODY} bytes")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise _HttpError(HTTPStatus.BAD_REQUEST, f"body is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise _HttpError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
        return data

    # ---- HTTP verbs -----------------------------------------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        url = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(url.path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        try:
            if path.startswith(API_PREFIX):
                self._api(method, path[len(API_PREFIX):].strip("/"), query)
            elif method == "GET":
                self._static(path)
            else:
                self._error(405, "method not allowed")
        except _HttpError as e:
            self._error(int(e.code), e.msg)
        except KeyError as e:
            self._error(404, _key_msg(e))
        except (IndexError, FileNotFoundError) as e:
            self._error(404, str(e) or "not found")
        except ValueError as e:
            self._error(400, str(e) or "bad request")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            log.debug("client went away on %s %s", method, path)
        except Exception as e:  # noqa: BLE001 - the adapter must never leak a traceback to the socket
            log.exception("%s %s failed", method, path)
            self._error(500, f"{type(e).__name__}: {e}")

    # ---- API ------------------------------------------------------------------------------------------------
    def _api(self, method: str, rel: str, query: dict[str, str]) -> None:
        name, params, known = match(method, rel)
        if name is None:
            if known:
                self._error(405, f"{method} not allowed on {rel}")
            else:
                self._error(404, f"no route {method} /api/v1/{rel}")
            return
        service: AtlasService = self.server.service  # type: ignore[attr-defined]
        if name == "page_png":
            self._png(service, params, query)
            return
        if name == "pdf_path":
            self._pdf(service, params, query)
            return
        if name == "export_collection_md":
            md = service.export_collection_md(params["coll_id"])
            name_for_file = f"{params['coll_id']}.md"
            kind = "attachment" if query.get("download") else "inline"
            self._send(200, md.encode("utf-8"), "text/markdown; charset=utf-8",
                       {"Content-Disposition": _content_disposition(kind, name_for_file)})
            return
        body = self._read_body() if method in ("POST", "PUT") else {}
        kwargs = self._kwargs(service, name, params, query, body)
        result = getattr(service, name)(**kwargs)
        if name == "update_note" and isinstance(result, dict) and result.get("conflict"):
            self._json(409, result)
            return
        self._json(201 if name in CREATED else 200, result)

    @staticmethod
    def _kwargs(service: AtlasService, name: str, params: dict[str, str], query: dict[str, str],
                body: dict) -> dict[str, Any]:
        fn = getattr(service, name)
        sig = inspect.signature(fn)
        aliases = BODY_ALIASES.get(name, {})
        raw: dict[str, Any] = {}
        for src in (query, body, params):
            for k, v in src.items():
                raw[aliases.get(k, k)] = v
        kwargs: dict[str, Any] = {}
        required = REQUIRED.get(name, ())
        defaults = DEFAULTS.get(name, {})
        for pname, p in sig.parameters.items():
            if pname in raw:
                kwargs[pname] = _coerce(pname, raw[pname])
            elif p.default is not inspect.Parameter.empty:
                continue
            elif pname in required:
                raise ValueError(f"'{pname}' is required")
            else:
                # contract methods without defaults: a partial body still works
                kwargs[pname] = defaults.get(pname)
        return kwargs

    def _png(self, service: AtlasService, params: dict[str, str], query: dict[str, str]) -> None:
        zoom = _coerce("zoom", query.get("zoom") or None)
        png, info = service.page_png(params["doc_id"], int(params["page_no"]), highlight=query.get("hl") or None,
                                     spans=_parse_spans(query.get("spans")), zoom=zoom)
        hit_y = info.get("hit_y")
        headers = {
            "X-Page": str(info.get("page", params["page_no"])),
            "X-Pages": str(info.get("pages", "")),
            "X-Hits": str(info.get("hits", 0)),
            "X-Hit-Y": "" if hit_y in (None, "") else f"{float(hit_y):.4f}",
        }
        self._send(200, png, "image/png", headers)

    def _pdf(self, service: AtlasService, params: dict[str, str], query: dict[str, str]) -> None:
        path = service.pdf_path(params["doc_id"])
        if path is None:
            raise KeyError(f"no PDF for {params['doc_id']!r}")
        data = Path(path).read_bytes()
        kind = "attachment" if query.get("download") else "inline"
        self._send(200, data, "application/pdf",
                   {"Content-Disposition": _content_disposition(kind, f"{params['doc_id']}.pdf")})

    # ---- static ---------------------------------------------------------------------------------------------
    def _static(self, path: str) -> None:
        rel = path.strip("/")
        if rel == "":
            rel = "index.html"
        elif rel.startswith("static/"):
            rel = rel[len("static/"):]
        if not rel or ".." in rel.split("/") or rel.startswith("/") or ":" in rel:
            raise KeyError("not found")
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            raise KeyError("not found") from None
        if not target.is_file():
            raise KeyError(f"no such file: /{rel}")
        ctype = MIME.get(target.suffix.lower()) or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"})


class _HttpError(Exception):
    def __init__(self, code: int | HTTPStatus, msg: str):
        super().__init__(msg)
        self.code = int(code)
        self.msg = msg


def _key_msg(e: KeyError) -> str:
    return str(e.args[0]) if e.args else "not found"


class AtlasServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: AtlasService):
        super().__init__(address, AtlasHandler)
        self.service = service


def make_server(service: AtlasService, host: str = "127.0.0.1", port: int = 0) -> AtlasServer:
    """Bind (port 0 → an ephemeral port, see server.server_address) without starting to serve."""
    mimetypes.init()
    srv = AtlasServer((host, int(port)), service)
    log.info("bound http://%s:%d/", *srv.server_address[:2])
    return srv


def setup_logging(project: Project, level: int = logging.INFO) -> Path:
    """Rotating file log at <project>/work/logs/serve.log (5 MB × 3) on the 'atlas' logger."""
    project.logs_dir.mkdir(parents=True, exist_ok=True)
    path = project.logs_dir / "serve.log"
    root = logging.getLogger("atlas")
    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler) and Path(getattr(h, "baseFilename", "")) == path:
            return path
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    return path


def serve(project: Project, host: str | None = None, port: int | None = None) -> None:
    host = host or str(project.get("serve.host", "127.0.0.1"))
    port = int(port if port is not None else project.get("serve.port", 8767))
    log_path = setup_logging(project)
    service = AtlasService(project)
    srv = make_server(service, host, port)
    h, p = srv.server_address[:2]
    url = f"http://{h}:{p}/"
    log.info("serving %s (project %s, log %s)", url, project.root, log_path)
    print(f"concept-atlas serving {url}  (project: {project.root}; log: {log_path})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping on keyboard interrupt")
    finally:
        srv.server_close()


def run_in_thread(service: AtlasService, host: str = "127.0.0.1", port: int = 0) -> tuple[AtlasServer, threading.Thread]:
    """Start a server on a daemon thread (tests)."""
    srv = make_server(service, host, port)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    return srv, t
