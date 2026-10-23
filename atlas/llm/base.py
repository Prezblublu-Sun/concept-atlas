"""LLM layer base: request/response dataclasses, the ChatClient protocol, the request hash and the fake client.

Frozen contract for increment 4 (docs/INCREMENT4.md §1.1): providers (client.py), tasks, ledger, governor and the
curation stages all code against these types. Nothing here performs network I/O.

Guard: when the environment variable ATLAS_LLM_NO_NETWORK is set to a non-empty value, real providers must raise
NetworkForbidden before any HTTP call (the test suite sets it in tests/conftest.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

NO_NETWORK_ENV = "ATLAS_LLM_NO_NETWORK"


class NetworkForbidden(RuntimeError):
    """Raised by real providers when ATLAS_LLM_NO_NETWORK is set."""


def network_forbidden() -> bool:
    return bool(os.environ.get(NO_NETWORK_ENV))


@dataclass(frozen=True)
class ChatRequest:
    """One structured call. `system` blocks are the stable prefix (the last block gets the cache breakpoint);
    `user` is the rendered payload; `output_schema` becomes the provider's JSON-schema output format."""
    model: str
    system: tuple[str, ...]
    user: str
    max_tokens: int
    output_schema: dict | None = None
    effort: str | None = None
    task: str = ""
    key: str = ""


@dataclass
class Usage:
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    model_reported: str | None = None

    @property
    def total_input(self) -> int:
        return self.input + self.cache_write + self.cache_read


@dataclass
class ChatResponse:
    text: str
    stop_reason: str                 # end_turn | max_tokens | refusal | error
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    raw_id: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.stop_reason == "end_turn" and self.error is None


@dataclass
class BatchStatus:
    batch_id: str
    state: str                       # in_progress | ended | canceling | unknown
    n_requests: int = 0
    n_done: int = 0
    n_failed: int = 0


@runtime_checkable
class ChatClient(Protocol):
    provider: str
    currency: str

    def complete(self, req: ChatRequest) -> ChatResponse: ...

    def count_tokens(self, req: ChatRequest) -> int | None: ...

    def submit_batch(self, reqs: Sequence[ChatRequest]) -> str: ...

    def batch_status(self, batch_id: str) -> BatchStatus: ...

    def batch_results(self, batch_id: str) -> dict[str, ChatResponse]: ...


def canonical_request(provider: str, req: ChatRequest) -> dict:
    """The fields that identify a call for caching: provider, model, system, user, max_tokens, schema, effort.
    Task and key are bookkeeping and stay out."""
    return {"provider": provider, "model": req.model, "system": list(req.system), "user": req.user,
            "max_tokens": req.max_tokens, "output_schema": req.output_schema, "effort": req.effort}


def request_hash(provider: str, req: ChatRequest) -> str:
    blob = json.dumps(canonical_request(provider, req), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """Cheap local estimate used when no provider count is available: ~4 characters per token for English and
    ~1.5 for CJK, computed per character class."""
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    return int((len(text) - cjk) / 4 + cjk / 1.5) + 1


Responder = str | Callable[[ChatRequest], "str | ChatResponse"]


class FakeChatClient:
    """Deterministic stand-in for tests and dry runs.

    `responses` maps a request key or task name to a string (the response text) or a callable returning a string
    or a full ChatResponse (to simulate `max_tokens`, `refusal`, errors). Lookup order: key, then task, then
    `default`. Every request is recorded in `requests`; batches resolve immediately on submit and are returned by
    `batch_results`. Usage is derived from the local token estimate so cost arithmetic can be tested.
    """
    provider = "fake"
    currency = "USD"

    def __init__(self, responses: dict[str, Responder] | None = None, default: Responder = "{}",
                 token_counts: bool = True) -> None:
        self.responses: dict[str, Responder] = dict(responses or {})
        self.default = default
        self.token_counts = token_counts
        self.requests: list[ChatRequest] = []
        self.batches: dict[str, dict[str, ChatResponse]] = {}
        self._n = 0

    def _answer(self, req: ChatRequest) -> ChatResponse:
        r = self.responses.get(req.key, self.responses.get(req.task, self.default))
        out = r(req) if callable(r) else r
        if isinstance(out, ChatResponse):
            if out.usage.input == 0 and out.usage.output == 0:
                out.usage = Usage(input=estimate_tokens("\n".join(req.system) + req.user),
                                  output=estimate_tokens(out.text))
            return out
        return ChatResponse(text=str(out), stop_reason="end_turn",
                            usage=Usage(input=estimate_tokens("\n".join(req.system) + req.user),
                                        output=estimate_tokens(str(out)), model_reported=req.model),
                            latency_ms=1, raw_id=f"fake-{len(self.requests)}")

    def complete(self, req: ChatRequest) -> ChatResponse:
        self.requests.append(req)
        return self._answer(req)

    def count_tokens(self, req: ChatRequest) -> int | None:
        if not self.token_counts:
            return None
        return estimate_tokens("\n".join(req.system) + req.user)

    def submit_batch(self, reqs: Sequence[ChatRequest]) -> str:
        self._n += 1
        batch_id = f"fakebatch_{self._n:04d}"
        results: dict[str, ChatResponse] = {}
        for req in reqs:
            self.requests.append(req)
            results[req.key] = self._answer(req)
        self.batches[batch_id] = results
        return batch_id

    def batch_status(self, batch_id: str) -> BatchStatus:
        res = self.batches.get(batch_id)
        if res is None:
            return BatchStatus(batch_id, "unknown")
        failed = sum(1 for r in res.values() if r.error)
        return BatchStatus(batch_id, "ended", n_requests=len(res), n_done=len(res) - failed, n_failed=failed)

    def batch_results(self, batch_id: str) -> dict[str, ChatResponse]:
        return dict(self.batches.get(batch_id, {}))


def now_ms() -> int:
    return int(time.time() * 1000)


def chunked(items: Iterable, size: int) -> list[list]:
    out: list[list] = []
    cur: list = []
    for it in items:
        cur.append(it)
        if len(cur) >= size:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out
