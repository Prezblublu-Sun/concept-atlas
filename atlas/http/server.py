"""ThreadingHTTPServer adapter. Implemented by: increment 1, agent "service+http".

- `serve(project, host, port)` builds one AtlasService, serves routes.py + static files, logs to
  <project>/work/logs/serve.log (RotatingFileHandler, 5 MB × 3) and prints the URL once. A port that is already in
  use is a hard error (OSError): on Windows the socket is bound with SO_EXCLUSIVEADDRUSE, because SO_REUSEADDR
  would silently let a second server bind a port that another process is listening on.
- Explicit MIME table (Windows registry maps .mjs/.json wrongly). Static files get `?v=<atlas.__version__>`
  cache-busting: the server rewrites the `?v=` tokens of index.html on the fly; it sends `Cache-Control: no-cache`
  for static files anyway.
- Only JSON bodies (`Content-Type: application/json`, else 415); body size cap 20 MB; `Transfer-Encoding: chunked`
  → 411; the declared body is always read (or the connection closed) so an error response never leaves unread
  bytes on a keep-alive connection. Every handler wrapped: exceptions → 500 {"error"}; KeyError/None → 404;
  ValueError → 400. Errors the stdlib would answer with HTML (501 unsupported method, 400 bad request line) are
  JSON too. A concept/framework route without a promoted build answers 409 {"error": "no build promoted"}.
- JSON body values are validated by type (see _coerce): wrong types → 400, never a 500 from the store.
- Parameter precedence: path parameters > JSON body (POST/PUT) or query string (GET/DELETE); the body is
  authoritative for POST/PUT, query parameters are ignored there.
- No auth (single user, loopback or tailnet); cross-site browser writes are refused (Sec-Fetch-Site / Origin
  check), which is what stops a web page the user visits from posting notes to the local API.
- Each request runs inside `AtlasService.lease()` (pooled store connections per request thread).

`make_server(service, host, port)` returns the (unstarted) ThreadingHTTPServer so tests can run it in a thread on
port 0.
"""
from __future__ import annotations

import inspect
import json
import logging
import logging.handlers
import math
import mimetypes
import re
import socket
import sys
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import atlas
from atlas.config import Project
from atlas.http.routes import API_PREFIX, BODY_ALIASES, CREATED, match
from atlas.service.api import AtlasService, NoBuildError

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 20 * 1024 * 1024
DEFAULT_PORT = 8770         # 8765–8767 are taken by the previous term viewers on the author's machine
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
# ---- parameter types: every service parameter the adapter may fill from a query string or a JSON body
_INT_PARAMS = {"limit", "offset", "page_no", "context", "rev"}
_FLOAT_PARAMS = {"zoom", "seq"}
_BOOL_PARAMS = {"explicit", "standalone", "ids"}
_LIST_PARAMS = {"tags", "target_refs"}
_STR_PARAMS = {"title", "body_md", "comment_md", "name", "description", "ref", "after_item", "phrase", "q", "tag",
               "highlight", "doc_id", "note_id", "coll_id", "item_id", "sid", "concept_id", "sub_id", "domain",
               "tier", "type", "sub", "cat", "scope", "sort", "dir", "section", "doc", "c", "use"}
MAX_LIMIT = 1000
_INT_LIMIT = 2 ** 63 - 1
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off", "")
_CONNECTION_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError)
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_SLUG_RE = re.compile(r"[^\w\-]+", re.UNICODE)
_VERSION_TOKEN = re.compile(rb"\?v=[\w.\-]+")
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


def _to_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"parameter {name!r} must be an integer")
    if isinstance(value, int):
        v = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"parameter {name!r} must be an integer")
        v = int(value)
    elif isinstance(value, str):
        try:
            v = int(value.strip())
        except ValueError as e:
            raise ValueError(f"parameter {name!r} must be a number") from e
    else:
        raise ValueError(f"parameter {name!r} must be an integer")
    if abs(v) > _INT_LIMIT:
        raise ValueError(f"parameter {name!r} is out of range")
    return v


def _to_float(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"parameter {name!r} must be a number")
    if isinstance(value, (int, float)):
        v = float(value)
    elif isinstance(value, str):
        try:
            v = float(value.strip())
        except ValueError as e:
            raise ValueError(f"parameter {name!r} must be a number") from e
    else:
        raise ValueError(f"parameter {name!r} must be a number")
    if not math.isfinite(v):
        raise ValueError(f"parameter {name!r} must be a finite number")
    return v


def _to_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    raise ValueError(f"parameter {name!r} must be a boolean")


def _coerce(name: str, value: Any) -> Any:
    """Validate/convert one parameter from a query string (str) or a JSON body (any JSON type): wrong JSON types
    are a ValueError (→ 400) instead of a TypeError/ProgrammingError deep in the store (→ 500)."""
    if value is None:
        return None
    if name in _INT_PARAMS:
        v = _to_int(name, value)
        if name == "limit":
            return max(1, min(MAX_LIMIT, v))
        if name == "offset":
            return max(0, v)
        return v
    if name in _FLOAT_PARAMS:
        return _to_float(name, value)
    if name in _BOOL_PARAMS:
        return _to_bool(name, value)
    if name in _LIST_PARAMS:
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError(f"parameter {name!r} must be a list of strings")
        return value
    if not isinstance(value, str):
        raise ValueError(f"parameter {name!r} must be a string")
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


def _slug(name: str | None, limit: int = 80) -> str:
    s = _SLUG_RE.sub("-", " ".join((name or "").split())).strip("-_")
    return s[:limit].rstrip("-_")


def _content_disposition(kind: str, filename: str, ascii_fallback: str = "file") -> str:
    """`inline; filename="ascii"; filename*=UTF-8''percent-encoded` — the ASCII name falls back to
    `ascii_fallback` when the real name has no ASCII letters/digits (e.g. a CJK collection name)."""
    ascii_name = _SAFE_FILENAME.sub("_", filename)
    stem, dot, ext = ascii_name.rpartition(".")
    if not re.search(r"[A-Za-z0-9]", stem if dot else ascii_name):
        ascii_name = ascii_fallback + (("." + ext) if dot else "")
    quoted = urllib.parse.quote(filename, safe="")
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


class AtlasHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"concept-atlas/{atlas.__version__}"
    service: AtlasService  # set on the server; read through self.server

    # ---- plumbing -------------------------------------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: D401 - BaseHTTPRequestHandler API
        log.debug("%s %s", self.address_string(), fmt % args)

    def log_error(self, fmt: str, *args) -> None:  # noqa: D401 - BaseHTTPRequestHandler API
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Atlas-Build", self.server.service.build_label())  # type: ignore[attr-defined]
        hdrs = dict(headers or {})
        cache = hdrs.pop("Cache-Control", None)
        if cache is None:
            cache = "max-age=600" if ctype in ("image/png", "application/pdf") else "no-cache"
        self.send_header("Cache-Control", cache)
        self.send_header("Access-Control-Expose-Headers", _EXPOSE)
        for k, v in hdrs.items():
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, JSON_CT, headers)

    def _error(self, code: int, msg: str) -> None:
        self._json(code, {"error": msg, "status": code})

    def _fail(self, code: int, msg: str) -> None:
        """_error() for the except branches: a client that went away must not raise out of the handler."""
        try:
            self._error(code, msg)
        except _CONNECTION_ERRORS:
            log.debug("client went away while sending %d", code)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """JSON instead of the stdlib HTML page for errors raised below the route table (bad request line,
        unsupported method, header limits); the connection is closed because the request may be half-read."""
        self.close_connection = True
        try:
            phrase = HTTPStatus(int(code)).phrase
        except ValueError:
            phrase = "error"
        log.debug("%s: %s %s", self.address_string(), code, message or phrase)
        body = json.dumps({"error": message or phrase, "status": int(code)}, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(int(code), phrase)
            self.send_header("Content-Type", JSON_CT)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD" and int(code) >= 200 and int(code) not in (204, 304):
                self.wfile.write(body)
        except _CONNECTION_ERRORS:
            log.debug("client went away while sending error %s", code)

    # ---- request body ---------------------------------------------------------------------------------------
    def _prepare_body(self) -> None:
        """Validate Content-Length / Transfer-Encoding and read the declared body up front (any method), so that
        no error path leaves body bytes on a keep-alive connection to be parsed as the next request."""
        self._body_raw = b""
        te = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if te:
            self.close_connection = True
            raise _HttpError(HTTPStatus.LENGTH_REQUIRED,
                             "chunked request bodies are not supported; send Content-Length")
        cl = (self.headers.get("Content-Length") or "").strip()
        length = 0
        if cl:
            try:
                length = int(cl)
            except ValueError:
                self.close_connection = True
                raise _HttpError(HTTPStatus.BAD_REQUEST, "Content-Length must be an integer") from None
            if length < 0:
                self.close_connection = True
                raise _HttpError(HTTPStatus.BAD_REQUEST, "Content-Length must not be negative")
        if length > MAX_BODY:
            self.close_connection = True
            raise _HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"body larger than {MAX_BODY} bytes")
        if length:
            self._body_raw = self.rfile.read(length)
            if len(self._body_raw) < length:
                self.close_connection = True
                raise _HttpError(HTTPStatus.BAD_REQUEST, "request body shorter than Content-Length")

    def _read_body(self) -> dict:
        raw = getattr(self, "_body_raw", b"")
        if not raw:
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise _HttpError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "request body must be application/json")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise _HttpError(HTTPStatus.BAD_REQUEST, f"body is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise _HttpError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
        return data

    def _check_site(self) -> None:
        """Refuse browser requests from another site (CSRF on the unauthenticated local API). Fetch metadata is
        authoritative when present; the Origin header is the fallback for clients without it."""
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site in ("cross-site", "same-site"):
            raise _HttpError(HTTPStatus.FORBIDDEN, "cross-site requests are not allowed")
        if site:
            return
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return
        host = (self.headers.get("Host") or "").strip().lower()
        o_host = urllib.parse.urlsplit(origin).netloc.lower()
        if origin.lower() == "null" or not o_host or (host and o_host != host):
            raise _HttpError(HTTPStatus.FORBIDDEN, "cross-site requests are not allowed")

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

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_OPTIONS(self) -> None:
        self._dispatch("OPTIONS")

    def _dispatch(self, method: str) -> None:
        url = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(url.path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        service: AtlasService = self.server.service  # type: ignore[attr-defined]
        try:
            self._prepare_body()
            if method not in ("GET", "HEAD"):
                self._check_site()
            with service.lease():
                if path.startswith(API_PREFIX):
                    self._api(service, method, path[len(API_PREFIX):].strip("/"), query)
                elif method == "GET":
                    self._static(path)
                else:
                    self._error(405, "method not allowed")
        except _HttpError as e:
            self._fail(int(e.code), e.msg)
        except NoBuildError as e:
            self._fail(409, str(e))
        except KeyError as e:
            self._fail(404, _key_msg(e))
        except (IndexError, FileNotFoundError) as e:
            self._fail(404, str(e) or "not found")
        except ValueError as e:
            self._fail(400, str(e) or "bad request")
        except _CONNECTION_ERRORS:
            log.debug("client went away on %s %s", method, path)
            self.close_connection = True
        except Exception as e:  # noqa: BLE001 - the adapter must never leak a traceback to the socket
            log.exception("%s %s failed", method, path)
            self._fail(500, f"{type(e).__name__}: {e}")

    # ---- API ------------------------------------------------------------------------------------------------
    def _api(self, service: AtlasService, method: str, rel: str, query: dict[str, str]) -> None:
        name, params, known = match(method, rel)
        if name is None:
            if known:
                self._error(405, f"{method} not allowed on {rel}")
            else:
                self._error(404, f"no route {method} /api/v1/{rel}")
            return
        if name == "page_png":
            self._png(service, params, query)
            return
        if name == "pdf_path":
            self._pdf(service, params, query)
            return
        if name == "export_collection_md":
            coll = service.collection(params["coll_id"])
            md = service.export_collection_md(params["coll_id"])
            name_for_file = (_slug(coll.get("name")) or params["coll_id"]) + ".md"
            kind = "attachment" if query.get("download") else "inline"
            self._send(200, md.encode("utf-8"), "text/markdown; charset=utf-8",
                       {"Content-Disposition": _content_disposition(kind, name_for_file, params["coll_id"])})
            return
        body = self._read_body() if method in ("POST", "PUT") else {}
        kwargs = self._kwargs(service, name, method, params, query, body)
        result = getattr(service, name)(**kwargs)
        if name == "update_note" and isinstance(result, dict) and result.get("conflict"):
            self._json(409, result)
            return
        self._json(201 if name in CREATED else 200, result)

    @staticmethod
    def _kwargs(service: AtlasService, name: str, method: str, params: dict[str, str], query: dict[str, str],
                body: dict) -> dict[str, Any]:
        """Keyword arguments for the service call: path parameters win, then the JSON body (POST/PUT) or the
        query string (other methods); unknown body fields are ignored; every value is type-checked."""
        fn = getattr(service, name)
        sig = inspect.signature(fn)
        aliases = BODY_ALIASES.get(name, {})
        raw: dict[str, Any] = {}
        sources = (body, params) if method in ("POST", "PUT") else (query, params)
        for src in sources:
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
        page_no = _coerce("page_no", params["page_no"])
        png, info = service.page_png(params["doc_id"], page_no, highlight=query.get("hl") or None,
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
        if path.endswith("/") and path.strip("/"):
            raise KeyError("not found")
        rel = path.strip("/")
        if rel == "":
            rel = "index.html"
        elif rel.startswith("static/"):
            rel = rel[len("static/"):]
        if not rel or ".." in rel.split("/") or rel.startswith("/") or ":" in rel or "\\" in rel:
            raise KeyError("not found")
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            raise KeyError("not found") from None
        if not target.is_file():
            raise KeyError(f"no such file: /{rel}")
        ctype = MIME.get(target.suffix.lower()) or "application/octet-stream"
        data = target.read_bytes()
        if rel == "index.html":
            data = _VERSION_TOKEN.sub(b"?v=" + atlas.__version__.encode("ascii"), data)
        self._send(200, data, ctype, {"Cache-Control": "no-cache"})


class _HttpError(Exception):
    def __init__(self, code: int | HTTPStatus, msg: str):
        super().__init__(msg)
        self.code = int(code)
        self.msg = msg


def _key_msg(e: KeyError) -> str:
    return str(e.args[0]) if e.args else "not found"


class AtlasServer(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR on Windows lets a second process bind a port another server is listening on (review fix);
    # POSIX keeps it so a restart does not wait for TIME_WAIT connections.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address: tuple[str, int], service: AtlasService):
        super().__init__(address, AtlasHandler)
        self.service = service

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address) -> None:
        """Exceptions escaping a handler thread (client disconnects mid-response, request-line read errors) go
        to the log instead of the stdlib's traceback print on stderr."""
        exc = sys.exception()
        if isinstance(exc, _CONNECTION_ERRORS):
            log.debug("connection from %s dropped: %s", client_address, exc)
        else:
            log.warning("request from %s failed", client_address, exc_info=True)


def make_server(service: AtlasService, host: str = "127.0.0.1", port: int = 0) -> AtlasServer:
    """Bind (port 0 → an ephemeral port, see server.server_address) without starting to serve. Raises OSError
    when the address is already in use."""
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
    port = int(port if port is not None else project.get("serve.port", DEFAULT_PORT))
    log_path = setup_logging(project)
    service = AtlasService(project)
    try:
        srv = make_server(service, host, port)
    except OSError as e:
        log.error("cannot bind %s:%d: %s", host, port, e)
        raise OSError(f"cannot bind {host}:{port} ({e}); is another server using the port? "
                      f"Use --port or serve.port in project.yaml") from e
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
        service.shutdown()


def run_in_thread(service: AtlasService, host: str = "127.0.0.1", port: int = 0) -> tuple[AtlasServer, threading.Thread]:
    """Start a server on a daemon thread (tests)."""
    srv = make_server(service, host, port)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    return srv, t
