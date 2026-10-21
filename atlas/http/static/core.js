// concept-atlas SPA — shared helpers: API client, i18n, DOM builder, Markdown renderer, hash router.
// Vanilla ES module, no build step. Every piece of data is rendered through textContent or escaped first.

export const VERSION = "0.1.0";
export const API = "/api/v1/";

// ---------------------------------------------------------------- API client
export class ApiError extends Error {
  constructor(status, message, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

function qs(params) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v === undefined || v === null || v === "") continue;
    u.set(k, String(v));
  }
  const s = u.toString();
  return s ? "?" + s : "";
}

async function call(method, path, params, body) {
  const url = API + path + qs(params);
  const init = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json; charset=utf-8";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(url, init);
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!res.ok) throw new ApiError(res.status, (data && data.error) || res.statusText, data);
  return data;
}

export const api = {
  get: (path, params) => call("GET", path, params),
  post: (path, body) => call("POST", path, undefined, body),
  put: (path, body) => call("PUT", path, undefined, body),
  del: (path) => call("DELETE", path),
  url: (path, params) => API + path + qs(params),
  async png(path, params) {
    const res = await fetch(API + path + qs(params));
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).error || msg; } catch { /* binary or empty */ }
      throw new ApiError(res.status, msg);
    }
    const blob = await res.blob();
    return {
      blob,
      page: Number(res.headers.get("X-Page") || 0),
      pages: Number(res.headers.get("X-Pages") || 0),
      hits: Number(res.headers.get("X-Hits") || 0),
      hitY: res.headers.get("X-Hit-Y") ? Number(res.headers.get("X-Hit-Y")) : null,
    };
  },
};

// ---------------------------------------------------------------- i18n
const LANG_KEY = "atlas.lang";
const listeners = new Set();

export const i18n = {
  lang: "zh",
  ui: {},
  sections: {},
  tags: {},
  kinds: {},
  async load() {
    try { this.lang = localStorage.getItem(LANG_KEY) || this.detect(); } catch { this.lang = this.detect(); }
    const data = await api.get("i18n");
    this.ui = data.ui || {};
    this.sections = data.sections || {};
    this.tags = data.tags || {};
    this.kinds = data.kinds || {};
    document.documentElement.lang = this.lang === "zh" ? "zh-CN" : "en";
  },
  detect() {
    const n = (navigator.language || "").toLowerCase();
    return n.startsWith("zh") ? "zh" : "en";
  },
  setLang(l) {
    this.lang = l === "en" ? "en" : "zh";
    try { localStorage.setItem(LANG_KEY, this.lang); } catch { /* private mode */ }
    document.documentElement.lang = this.lang === "zh" ? "zh-CN" : "en";
    for (const fn of listeners) fn(this.lang);
  },
  toggle() { this.setLang(this.lang === "zh" ? "en" : "zh"); },
  onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  pick(pair, fallback) {
    if (!pair) return fallback ?? "";
    if (typeof pair === "string") return pair;
    return pair[this.lang] ?? pair.en ?? pair.zh ?? fallback ?? "";
  },
  t(key, vars) {
    let s = this.pick(this.ui[key], key);
    if (vars) for (const [k, v] of Object.entries(vars)) s = s.split("{" + k + "}").join(String(v));
    return s;
  },
  section(key) { return this.pick(this.sections[key], key); },
  tag(key) { return this.pick(this.tags[key], key); },
  kind(letter) { return this.pick(this.kinds[letter], letter); },
};
export const t = (key, vars) => i18n.t(key, vars);

// ---------------------------------------------------------------- DOM helpers
/** h("div.card#id", {attr: v, on: {click: fn}, data: {...}}, ...children) — children: Node | string | array | null */
export function h(spec, attrs, ...children) {
  const [tag, ...rest] = spec.split(/(?=[.#])/);
  const el = document.createElement(tag || "div");
  for (const r of rest) {
    if (r[0] === ".") el.classList.add(r.slice(1));
    else if (r[0] === "#") el.id = r.slice(1);
  }
  if (attrs && (attrs instanceof Node || typeof attrs !== "object" || Array.isArray(attrs))) {
    children.unshift(attrs);
    attrs = null;
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === "data") for (const [dk, dv] of Object.entries(v)) el.dataset[dk] = dv;
    else if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v; // only ever receives output of md()/snippet() which escape
    else if (k in el && k !== "style" && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else if (c instanceof Node) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

export function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/** FTS snippet → safe HTML: everything escaped except the <mark> pairs the server inserted. */
export function snippetHtml(snippet) {
  return esc(snippet).replace(/&lt;mark&gt;/g, "<mark>").replace(/&lt;\/mark&gt;/g, "</mark>");
}

/** Plain text with a phrase highlighted (case-insensitive) → DocumentFragment. */
export function markPhrase(text, phrase) {
  const frag = document.createDocumentFragment();
  text = String(text ?? "");
  const p = (phrase || "").trim();
  if (!p) { frag.appendChild(document.createTextNode(text)); return frag; }
  const lower = text.toLowerCase(), pl = p.toLowerCase();
  let i = 0, j;
  while ((j = lower.indexOf(pl, i)) >= 0) {
    if (j > i) frag.appendChild(document.createTextNode(text.slice(i, j)));
    frag.appendChild(h("mark", text.slice(j, j + p.length)));
    i = j + p.length;
  }
  if (i < text.length) frag.appendChild(document.createTextNode(text.slice(i)));
  return frag;
}

export function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function debounce(fn, ms) {
  let timer = null;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => { timer = null; fn(...args); }, ms);
  };
  wrapped.flush = (...args) => { if (timer) { clearTimeout(timer); timer = null; fn(...args); } };
  wrapped.cancel = () => { clearTimeout(timer); timer = null; };
  return wrapped;
}

export function clip(s, n = 160) {
  s = String(s ?? "").replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

export function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* storage unavailable */ }
  return value;
}

// ---------------------------------------------------------------- Markdown (minimal, escaping)
const REF_RE = /\[\[((?:D|U|S|C|ST|SD|A|N|K|Q):[^\]\s]+)\]\]/g;

function inline(text) {
  // split on code spans so formatting never applies inside them
  const parts = text.split(/(`[^`]*`)/);
  return parts.map((seg, i) => {
    if (i % 2 === 1) return "<code>" + esc(seg.slice(1, -1)) + "</code>";
    // links and [[refs]] become placeholders first, so the emphasis passes never touch an href / data-ref
    // ('_' and '*' are common inside URLs and ids)
    const saved = [];
    const keep = (html) => { saved.push(html); return `\u0000${saved.length - 1}\u0000`; };
    let s = esc(seg);
    s = s.replace(REF_RE, (_, ref) => keep(`<a class="reflink" href="#/ref/${encodeURIComponent(ref)}" data-ref="${ref}">${ref}</a>`));
    s = s.replace(/\[([^\]]+)\]\(((?:https?:\/\/|#\/|\/)[^)\s]+)\)/g, (_, txt, url) =>
      keep(`<a href="${url}" ${url.startsWith("http") ? 'target="_blank" rel="noopener"' : ""}>${txt}</a>`));
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>");
    s = s.replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, n) => saved[Number(n)]);
  }).join("");
}

/** Markdown → HTML string (headings, paragraphs, lists, code, blockquote, bold/italic/code, links, [[refs]]). */
export function md(src) {
  const lines = String(src ?? "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let para = [], list = null, quote = [], fence = null;
  const flushPara = () => { if (para.length) { out.push("<p>" + inline(para.join(" ")) + "</p>"); para = []; } };
  const flushList = () => { if (list) { out.push(`<${list.tag}>` + list.items.map((x) => "<li>" + inline(x) + "</li>").join("") + `</${list.tag}>`); list = null; } };
  const flushQuote = () => { if (quote.length) { out.push("<blockquote>" + inline(quote.join(" ")) + "</blockquote>"); quote = []; } };
  const flushAll = () => { flushPara(); flushList(); flushQuote(); };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (fence !== null) {
      if (/^```/.test(line)) { out.push("<pre><code>" + esc(fence.join("\n")) + "</code></pre>"); fence = null; }
      else fence.push(raw);
      continue;
    }
    let m;
    if (/^```/.test(line)) { flushAll(); fence = []; continue; }
    if (!line.trim()) { flushAll(); continue; }
    if ((m = /^(#{1,6})\s+(.*)$/.exec(line))) { flushAll(); out.push(`<h${m[1].length}>` + inline(m[2]) + `</h${m[1].length}>`); continue; }
    if (/^(?:-{3,}|\*{3,})$/.test(line.trim())) { flushAll(); out.push("<hr>"); continue; }
    if ((m = /^\s*[-*+]\s+(.*)$/.exec(line))) {
      flushPara(); flushQuote();
      if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; }
      list.items.push(m[1]); continue;
    }
    if ((m = /^\s*\d+[.)]\s+(.*)$/.exec(line))) {
      flushPara(); flushQuote();
      if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; }
      list.items.push(m[1]); continue;
    }
    if ((m = /^>\s?(.*)$/.exec(line))) { flushPara(); flushList(); quote.push(m[1]); continue; }
    if (list && /^\s{2,}\S/.test(line)) { list.items[list.items.length - 1] += " " + line.trim(); continue; }
    flushList(); flushQuote();
    para.push(line.trim());
  }
  if (fence !== null) out.push("<pre><code>" + esc(fence.join("\n")) + "</code></pre>");
  flushAll();
  return out.join("\n");
}

// ---------------------------------------------------------------- refs → routes
/** Where a ref object (from /resolve or a snapshot) should open in the SPA. */
export function refHref(obj) {
  if (!obj || !obj.ref) return null;
  const kind = obj.kind || obj.ref.split(":")[0];
  if (kind === "S" && obj.doc_id) {
    return `#/docs/${encodeURIComponent(obj.doc_id)}` + qs({ page: obj.page || 1, sid: obj.sid });
  }
  if ((kind === "D" || kind === "U") && obj.doc_id) return `#/docs/${encodeURIComponent(obj.doc_id)}`;
  if (kind === "N") return `#/notes/${encodeURIComponent(obj.ref.slice(2))}`;
  if (kind === "K") return `#/collections/${encodeURIComponent(obj.ref.slice(2))}`;
  return null;
}

/** Resolve every [[ref]] anchor inside `root` and replace its label with the target's text. */
export async function resolveRefLinks(root) {
  const anchors = Array.from(root.querySelectorAll("a.reflink[data-ref]"));
  await Promise.all(anchors.map(async (a) => {
    try {
      const obj = await api.get("resolve", { ref: a.dataset.ref });
      const href = refHref(obj);
      if (href) a.href = href;
      if (obj.status === "orphan") { a.classList.add("orphan"); a.title = t("ref.orphan"); return; }
      const label = obj.text || obj.title;
      if (label) {
        a.textContent = clip(label, 90);
        a.title = `${a.dataset.ref}` + (obj.title && obj.kind === "S" ? ` — ${obj.title}` : "");
        a.classList.add("resolved");
      }
    } catch { a.classList.add("orphan"); }
  }));
}

// ---------------------------------------------------------------- hash router helpers
export function parseHash(hash) {
  const raw = (hash ?? location.hash).replace(/^#\/?/, "");
  const [pathPart, query = ""] = raw.split("?");
  const path = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
  const params = {};
  for (const [k, v] of new URLSearchParams(query)) params[k] = v;
  return { path, params, key: pathPart };
}

export function buildHash(path, params) {
  return "#/" + path.map(encodeURIComponent).join("/") + qs(params);
}

export function go(path, params) { location.hash = buildHash(path, params); }

/** Update the hash without creating a history entry (page flips inside the reader). */
export function replaceHash(path, params) {
  const next = buildHash(path, params);
  if (location.hash !== next) history.replaceState(null, "", next);
}
