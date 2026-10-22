// Concept views (increment 2): sortable concept list, concept page with prev/next, all-sentences view.
import { api, ApiError, h, clear, t, i18n, clip, debounce, go, replaceHash, fmtDate, store } from "./core.js";
import { createEditor } from "./editor.js";
import { sectionBadge, tagBadges } from "./reader.js";

const LIST_KEY = "atlas.conceptList";       // sessionStorage: {ids, query} of the list the user came from
const LIMIT = 100;
const errBox = (e) => h("div.error-box", `${t("common.error")}: ${e.message}`);

export function noBuildBox(e) {
  if (e instanceof ApiError && e.status === 409) return h("div.hint", t("app.noBuild"));
  return errBox(e);
}

export function tierBadge(tier) {
  if (!tier) return h("span.tier.E", "–");
  const b = h("span.tier." + tier, tier);
  b.title = i18n.pick(i18n.tiers[tier], tier);
  return b;
}

export function conceptLink(c, extra) {
  const a = h("a", { href: `#/concepts/${encodeURIComponent(c.id)}` }, c.label || c.id);
  if (c.zh) a.title = c.zh;
  return extra ? h("span", a, " ", h("span.muted", c.zh || "")) : a;
}

function pct(x) { return x === null || x === undefined ? "" : (x * 100).toFixed(1) + "%"; }
function num(x, d = 2) { return x === null || x === undefined ? "" : Number(x).toFixed(d); }
export function dataLabel(zh, en) { return i18n.lang === "en" ? (en || i18n.data(zh) || zh || "") : (zh || en || ""); }
export function domainLabel(id) {
  const d = (i18n.domains || []).find((x) => x.id === id);
  return d ? dataLabel(d.zh, d.en) : (id || "");
}
function mainSection(s) { return s ? i18n.section(s) : ""; }

function saveList(ids, query) { try { sessionStorage.setItem(LIST_KEY, JSON.stringify({ ids, query })); } catch { /* ignore */ } }
function loadList() { try { return JSON.parse(sessionStorage.getItem(LIST_KEY) || "null"); } catch { return null; } }

// ---------------------------------------------------------------- concept list
export async function conceptsView(params) {
  const state = {
    q: params.q || "", domain: params.domain || "", tier: params.tier || "", type: params.type || "",
    scope: params.scope || "all", sort: params.sort || "papers", dir: params.dir || "",
    offset: Number(params.offset) || 0, sub: params.sub || "", cat: params.cat || "",
  };
  const COLS = [
    ["label", "concepts.col.label", ""], ["zh", "concepts.col.zh", null], ["type", "concepts.col.type", null],
    ["papers", "concepts.col.papers", "num"], ["coverage", "concepts.col.coverage", "num"],
    ["topic_rate", "concepts.col.topicRate", "num"], ["tier", "concepts.col.tier", ""],
    ["main_section", "concepts.col.mainSection", null], ["notes", "concepts.col.notes", "num"],
    ["overview", "concepts.col.overview", null],
  ];
  const SORTABLE = new Set(["label", "papers", "coverage", "topic_rate", "tier"]);
  const input = h("input", { type: "search", placeholder: t("concepts.search"), value: state.q, style: "flex:1;min-width:220px;max-width:420px" });
  const domainSel = h("select", h("option", { value: "" }, t("concepts.allDomains")));
  const tierSel = h("select", h("option", { value: "" }, t("concepts.allTiers")), ...["A", "B", "C", "D", "E"].map((x) => h("option", { value: x }, x + " · " + i18n.pick(i18n.tiers[x], x))));
  const typeSel = h("select", h("option", { value: "" }, t("concepts.allTypes")));
  const scopeSel = h("select", h("option", { value: "all" }, t("concepts.scopeAll")));
  for (const d of i18n.domains || []) {
    domainSel.appendChild(h("option", { value: d.id }, dataLabel(d.zh, d.en)));
    if (d.scope) scopeSel.appendChild(h("option", { value: d.id }, dataLabel(d.zh, d.en)));
  }
  domainSel.value = state.domain; tierSel.value = state.tier; scopeSel.value = state.scope;
  const total = h("span.muted");
  const table = h("div.tw");
  const prev = h("button.btn.small", { type: "button", on: { click: () => { state.offset = Math.max(0, state.offset - LIMIT); load(); } } }, "‹ " + t("common.prevPage"));
  const next = h("button.btn.small", { type: "button", on: { click: () => { state.offset += LIMIT; load(); } } }, t("common.nextPage") + " ›");
  const pageInfo = h("span.muted");
  const extra = h("span.chips");
  const el = h("div.page", h("h1", t("concepts.title")),
    h("div.filters", input, domainSel, tierSel, typeSel, h("span.muted", t("concepts.scope") + ":"), scopeSel, extra, total),
    h("div.muted", { style: "font-size:12px;margin:-6px 0 8px" }, t("concepts.sortHint")),
    table, h("div.pager", prev, pageInfo, next));
  let seq = 0;
  const types = new Set();

  function syncHash() {
    replaceHash(["concepts"], { q: state.q || undefined, domain: state.domain || undefined, tier: state.tier || undefined,
      type: state.type || undefined, scope: state.scope !== "all" ? state.scope : undefined, sort: state.sort !== "papers" ? state.sort : undefined,
      dir: state.dir || undefined, offset: state.offset || undefined, sub: state.sub || undefined, cat: state.cat || undefined });
  }

  function header() {
    const tr = h("tr");
    for (const [key, i18nKey, cls] of COLS) {
      const th = h("th" + (cls === "num" ? ".num" : ""), t(i18nKey));
      if (SORTABLE.has(key)) {
        th.classList.add("sortable");
        if (state.sort === key) { th.classList.add("sorted"); if (state.dir === "asc" || (!state.dir && key === "label")) th.classList.add("asc"); }
        th.addEventListener("click", () => {
          const defaultDir = (key === "label" || key === "tier") ? "asc" : "desc";
          if (state.sort === key) state.dir = (state.dir || defaultDir) === "desc" ? "asc" : "desc";
          else { state.sort = key; state.dir = ""; }
          state.offset = 0;
          load();
        });
      }
      tr.appendChild(th);
    }
    return h("thead", tr);
  }

  function row(c) {
    return h("tr.click", { on: { click: (ev) => { if (ev.target.tagName !== "A") go(["concepts", c.id]); } } },
      h("td", h("a", { href: `#/concepts/${encodeURIComponent(c.id)}` }, c.label)),
      h("td", c.zh || ""),
      h("td", h("span.badge", dataLabel(c.type))),
      h("td.num", String(c.papers)),
      h("td.num", pct(c.coverage)),
      h("td.num", pct(c.topic_rate)),
      h("td", tierBadge(c.tier)),
      h("td", mainSection(c.main_section)),
      h("td.num", c.notes ? h("span.badge.accent", String(c.notes)) : ""),
      h("td", h("span.muted", { style: "font-size:11px" }, t("concepts.overviewLater"))));
  }

  async function load() {
    const mySeq = ++seq;
    state.q = input.value.trim(); state.domain = domainSel.value; state.tier = tierSel.value; state.type = typeSel.value; state.scope = scopeSel.value;
    syncHash();
    clear(table).appendChild(h("div.loading", t("common.loading")));
    clear(extra);
    if (state.sub) extra.appendChild(h("a.chip.on", { href: `#/subdomains/${encodeURIComponent(state.sub)}` }, "SD: " + state.sub, h("span", { on: { click: (ev) => { ev.preventDefault(); state.sub = ""; load(); } } }, " ×")));
    if (state.cat) extra.appendChild(h("span.chip.on", "cat: " + state.cat, h("span", { on: { click: () => { state.cat = ""; load(); } } }, " ×")));
    try {
      const res = await api.get("concepts", { q: state.q || undefined, domain: state.domain || undefined, tier: state.tier || undefined,
        type: state.type || undefined, scope: state.scope, sort: state.sort, dir: state.dir || undefined, limit: LIMIT, offset: state.offset,
        sub: state.sub || undefined, cat: state.cat || undefined });
      if (mySeq !== seq) return;
      total.textContent = t("concepts.total", { n: res.total });
      clear(table);
      if (!res.rows.length) { table.appendChild(h("div.empty", t("concepts.empty"))); prev.disabled = next.disabled = true; pageInfo.textContent = ""; return; }
      const tbody = h("tbody", res.rows.map(row));
      table.appendChild(h("table.t", header(), tbody));
      for (const r of res.rows) if (r.type && !types.has(r.type)) { types.add(r.type); typeSel.appendChild(h("option", { value: r.type }, dataLabel(r.type))); }
      typeSel.value = state.type;
      saveList(res.rows.map((r) => r.id), { ...state });
      const pages = Math.max(1, Math.ceil(res.total / LIMIT));
      pageInfo.textContent = t("concepts.page", { p: Math.floor(state.offset / LIMIT) + 1, n: pages });
      prev.disabled = state.offset <= 0;
      next.disabled = state.offset + LIMIT >= res.total;
    } catch (e) { if (mySeq === seq) clear(table).appendChild(noBuildBox(e)); }
  }
  input.addEventListener("input", debounce(() => { state.offset = 0; load(); }, 250));
  for (const s of [domainSel, tierSel, typeSel, scopeSel]) s.addEventListener("change", () => { state.offset = 0; load(); });
  load();
  return { el, update(p) { /* hash changes come from this view itself */ } };
}

// ---------------------------------------------------------------- helpers for the concept page
function sparkline(years, totals) {
  const ys = Object.keys(totals || {}).map(Number).filter((y) => y > 1900).sort((a, b) => a - b);
  if (!ys.length) return h("div.muted", t("common.none"));
  const n = Object.fromEntries((years || []).map((r) => [Number(r.year), r.n]));
  const W = 320, H = 70, P = 14;
  const maxN = Math.max(1, ...ys.map((y) => n[y] || 0));
  const x = (i) => P + (i * (W - 2 * P)) / Math.max(1, ys.length - 1);
  const yv = (v) => H - P - (v / maxN) * (H - 2 * P);
  const pts = ys.map((y, i) => `${x(i).toFixed(1)},${yv(n[y] || 0).toFixed(1)}`).join(" ");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("class", "spark");
  const base = document.createElementNS("http://www.w3.org/2000/svg", "line");
  base.setAttribute("x1", P); base.setAttribute("x2", W - P); base.setAttribute("y1", H - P); base.setAttribute("y2", H - P); base.setAttribute("class", "base");
  svg.appendChild(base);
  const pl = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  pl.setAttribute("points", pts); svg.appendChild(pl);
  for (const [i, y] of [[0, ys[0]], [ys.length - 1, ys[ys.length - 1]]]) {
    const tx = document.createElementNS("http://www.w3.org/2000/svg", "text");
    tx.setAttribute("x", x(i)); tx.setAttribute("y", H - 2); tx.setAttribute("text-anchor", i === 0 ? "start" : "end"); tx.textContent = String(y);
    svg.appendChild(tx);
  }
  const tm = document.createElementNS("http://www.w3.org/2000/svg", "text");
  tm.setAttribute("x", W - P); tm.setAttribute("y", P - 2); tm.setAttribute("text-anchor", "end"); tm.textContent = `max ${maxN}`;
  svg.appendChild(tm);
  const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
  title.textContent = ys.map((y) => `${y}: ${n[y] || 0}/${totals[y]}`).join("\n");
  svg.appendChild(title);
  return svg;
}

export function markedText(text, spans) {
  // spans: [[cstart, cend, form, nested_in]] into the stored text → fragment with <mark class="occ">
  const frag = document.createDocumentFragment();
  const s = String(text ?? "");
  const sp = (spans || []).filter((x) => x[1] > x[0]).sort((a, b) => a[0] - b[0]);
  let i = 0;
  for (const [a, b, form, nested] of sp) {
    if (a < i) continue;
    if (a > i) frag.appendChild(document.createTextNode(s.slice(i, a)));
    const m = h("mark.occ" + (nested ? ".nested" : ""), s.slice(a, b));
    if (nested) m.title = `${t("concept.nested")} ${nested}`;
    frag.appendChild(m);
    i = b;
  }
  if (i < s.length) frag.appendChild(document.createTextNode(s.slice(i)));
  return frag;
}

function readerHref(docId, page, sid, conceptId) {
  const q = new URLSearchParams();
  if (page) q.set("page", page);
  if (sid) q.set("sid", sid);
  if (conceptId) q.set("c", conceptId);
  return `#/docs/${encodeURIComponent(docId)}?${q}`;
}

function exampleRow(e, conceptId) {
  return h("div.ex",
    h("div.src", h("a", { href: `#/docs/${encodeURIComponent(e.doc_id)}` }, h("b", clip(e.title || e.doc_id, 90))),
      e.year ? h("span", String(e.year)) : null, h("span.mono", e.doc_id),
      h("a", { href: readerHref(e.doc_id, e.page, e.sid, conceptId), title: t("concept.openReader") }, t("common.page", { p: e.page })),
      sectionBadge(e.section, true), tagBadges(e.tags)),
    h("div", markedText(e.text, e.spans)));
}

// ---------------------------------------------------------------- concept page
export async function conceptView(id, params) {
  let c;
  try { c = await api.get(`concepts/${encodeURIComponent(id)}`); }
  catch (e) { return { el: h("div.page", noBuildBox(e)) }; }
  const el = h("div.page");
  const main = h("div");
  const side = h("div.side");
  el.appendChild(h("div.crumb", h("a", { href: "#/concepts" }, t("nav.concepts")), " / ", h("span.mono", c.id)));

  // prev / next through the list the user came from
  const lst = loadList();
  const pn = h("div.pn");
  if (lst && lst.ids && lst.ids.includes(c.id)) {
    const i = lst.ids.indexOf(c.id);
    const mk = (idx, label) => {
      const a = h("a.btn.small", { href: idx >= 0 && idx < lst.ids.length ? `#/concepts/${encodeURIComponent(lst.ids[idx])}` : "#" }, label);
      if (idx < 0 || idx >= lst.ids.length) a.setAttribute("aria-disabled", "true");
      return a;
    };
    pn.appendChild(mk(i - 1, "‹ " + t("common.prev")));
    pn.appendChild(h("span.muted", `${i + 1} / ${lst.ids.length}`));
    pn.appendChild(mk(i + 1, t("common.next") + " ›"));
  }
  const all = c.importance.find((m) => m.scope === "all") || {};
  const head = h("div.concept-head",
    h("div", h("span.lab", c.label), h("span.zh", c.zh || ""),
      h("div.row.tight", { style: "margin-top:4px" }, h("span.badge", dataLabel(c.type)), h("span.badge", t("mode." + (c.mode || "forms"))),
        ...c.importance.map((m) => h("span.row.tight", { style: "gap:2px" }, h("span.muted", { style: "font-size:11px" }, m.scope === "all" ? t("concepts.scopeAll") : domainLabel(m.scope)), tierBadge(m.tier)))),
      h("div.forms", c.forms.map((f) => h("span.badge", f)), c.case_forms.map((f) => h("span.badge.accent", { title: t("concept.caseForms") }, f)),
        c.mode === "literal" ? h("span.badge.warn", { title: c.rx || "" }, t("concept.literal") + ": " + (c.readable || c.rx || "")) : null)),
    h("span.spacer"), pn);
  main.appendChild(head);

  // importance table
  const metrics = [["papers", "concept.papers", (m) => m.papers], ["any_papers", "concept.anyPapers", (m) => m.any_papers],
    ["coverage", "concept.coverage", (m) => pct(m.coverage)], ["topic_rate", "concept.topicRate", (m) => pct(m.topic_rate)],
    ["abstract_rate", "concept.abstractRate", (m) => pct(m.abstract_rate)], ["occ", "concept.occ", (m) => m.occ],
    ["median", "concept.median", (m) => num(m.median_per_paper, 1)], ["refs", "concept.refsPapers", (m) => m.refs_papers],
    ["main", "concept.mainSection", (m) => mainSection(m.main_section)], ["lift", "concept.lift", (m) => m.lift === null ? "–" : num(m.lift)],
    ["trend", "concept.trend", (m) => (m.trend === null ? "–" : num(m.trend)) + (m.trend_flag ? " " + t("trend." + m.trend_flag) : "")],
    ["tier", "concept.tier", (m) => tierBadge(m.tier)]];
  const imp = h("table.t", h("thead", h("tr", h("th", t("concept.scope")), ...c.importance.map((m) => h("th.num", m.scope === "all" ? t("concepts.scopeAll") : domainLabel(m.scope))))),
    h("tbody", metrics.map(([k, lk, f]) => h("tr", h("td", t(lk)), ...c.importance.map((m) => h("td.num", f(m)))))));
  main.appendChild(h("h2", t("concept.importance")));
  main.appendChild(h("div.tw", imp));

  // sections + domains + years
  const maxSec = Math.max(1, ...c.sections.map((s) => s.papers));
  const bars = h("div.bars");
  for (const s of c.sections) {
    bars.appendChild(h("span", i18n.section(s.section)));
    bars.appendChild(h("span.bar", h("i", { style: `width:${(100 * s.papers / maxSec).toFixed(1)}%` })));
    bars.appendChild(h("span.n", `${s.papers} / ${s.occ}`));
  }
  const dom = h("div.bars");
  const maxDom = Math.max(1, ...c.domains_use.map((d) => d.total));
  for (const d of c.domains_use) {
    dom.appendChild(h("span", d.domain ? domainLabel(d.domain) : dataLabel(d.zh, d.en)));
    dom.appendChild(h("span.bar", h("i", { style: `width:${(100 * d.papers / maxDom).toFixed(1)}%` })));
    dom.appendChild(h("span.n", `${d.papers} / ${d.total}`));
  }
  main.appendChild(h("div.grid2",
    h("div.card", h("div.card-h", t("concept.sections")), bars),
    h("div.card", h("div.card-h", t("concept.domainsUse")), dom),
    h("div.card", h("div.card-h", t("concept.years")), sparkline(c.years, c.year_totals))));

  // categories / sub-domains / anchor-of / term tree
  const cats = h("div.card", h("div.card-h", t("concept.categories")));
  if (!c.categories.length) cats.appendChild(h("div.muted", t("common.none")));
  for (const k of c.categories) {
    cats.appendChild(h("div.row.tight", { style: "margin:3px 0" }, h("span.badge", domainLabel(k.domain)),
      h("a", { href: `#/concepts?cat=${encodeURIComponent(k.primary_cat)}` }, `${k.primary_cat} ${dataLabel(k.primary_zh, k.primary_en)}`),
      k.secondary_cat ? h("span.muted", `· ${t("concept.secondary")}: ${k.secondary_cat} ${dataLabel(k.secondary_zh, k.secondary_en)}`) : null,
      k.reason ? h("span.muted", { style: "font-size:12px" }, "— " + dataLabel(k.reason)) : null));
  }
  const subs = h("div.card", h("div.card-h", t("concept.subdomains")));
  if (!c.subdomains.length) subs.appendChild(h("div.muted", t("common.none")));
  else subs.appendChild(h("div.tw", h("table.t", h("thead", h("tr", h("th", t("fw.subdomains")), h("th.num", t("concept.rank")), h("th.num", t("concept.inSub")), h("th.num", t("concept.coverage")), h("th.num", t("concept.restCoverage")), h("th.num", t("concept.lift")), h("th.num", t("concept.score")))),
    h("tbody", c.subdomains.map((s) => h("tr", h("td", h("a", { href: `#/subdomains/${encodeURIComponent(s.sub_id)}` }, dataLabel(s.zh, s.en)), " ", h("span.mono", s.sub_id)),
      h("td.num", String(s.rank)), h("td.num", `${s.papers} / ${s.sub_papers ?? ""}`), h("td.num", pct(s.coverage)), h("td.num", pct(s.coverage_rest)), h("td.num", num(s.lift)), h("td.num", num(s.score, 3))))))));
  if (c.anchor_of.length) subs.appendChild(h("div.row.tight", { style: "margin-top:6px" }, h("span.muted", t("concept.anchorOf") + ":"),
    c.anchor_of.map((s) => h("a.chip", { href: `#/subdomains/${encodeURIComponent(s.sub_id)}` }, dataLabel(s.zh, s.en)))));
  const places = h("div.card", h("div.card-h", t("concept.termTree")));
  if (!c.term_tree_places.length) places.appendChild(h("div.muted", t("common.none")));
  for (const p of c.term_tree_places) {
    places.appendChild(h("div.row.tight", { style: "margin:3px 0;font-size:13px" }, h("span.badge", domainLabel(p.domain)), h("span.badge", i18n.section(p.section)),
      h("span", dataLabel(p.sub_name)), h("span.muted", "· " + p.term), p.note ? h("span.muted", { style: "font-size:12px" }, "— " + dataLabel(p.note)) : null));
  }
  main.appendChild(h("div.grid2", cats, subs));
  main.appendChild(places);

  // examples
  main.appendChild(h("div.row", { style: "margin-top:18px" }, h("h2", { style: "margin:0" }, t("concept.examples")), h("span.spacer"),
    h("a.btn.primary", { href: `#/concepts/${encodeURIComponent(c.id)}/sentences` }, `${t("concept.allSentences")} (${all.occ ?? ""})`),
    h("a.btn", { href: `#/concepts/${encodeURIComponent(c.id)}/sentences?docs=1` }, `${t("concept.docs")} (${all.papers ?? ""})`)));
  const ex = h("div.results");
  if (!c.examples.length) ex.appendChild(h("div.empty", t("concept.noExamples")));
  for (const e of c.examples) ex.appendChild(exampleRow(e, c.id));
  main.appendChild(ex);

  // side: notes
  side.appendChild(notesCard(`C:${c.id}`, c.label));
  el.appendChild(h("div.concept-page", main, side));
  return { el };
}

/** Notes card for a C:/SD: target: existing notes + a new-note editor. */
export function notesCard(ref, label) {
  const list = h("div.list");
  const editor = createEditor({ targets: [ref], onSaved: (n, created) => { if (created) reload(); } });
  const card = h("div.card", h("div.card-h", t("concept.notes"), " ", h("span.mono", ref)), list, h("div", { style: "height:8px" }), editor.el);
  async function reload() {
    clear(list).appendChild(h("div.loading", t("common.loading")));
    try {
      const notes = await api.get("notes/for", { ref });
      clear(list);
      if (!notes.length) list.appendChild(h("div.muted", t("notes.none")));
      for (const n of notes) list.appendChild(h("div.note-card", h("div.title", n.title || t("common.untitled")), h("div.excerpt", clip(n.body_md ?? n.excerpt ?? "", 200)),
        h("div.foot", h("span.badges", (n.tags || []).map((tg) => h("span.badge.tag", tg))), h("span.muted", fmtDate(n.updated)), h("span.spacer"),
          h("a", { href: `#/notes/${encodeURIComponent(n.note_id)}` }, t("notes.open")))));
    } catch (e) { clear(list).appendChild(errBox(e)); }
  }
  reload();
  return card;
}

// ---------------------------------------------------------------- all sentences / documents of a concept
export async function conceptSentencesView(id, params) {
  let c;
  try { c = await api.get(`concepts/${encodeURIComponent(id)}`); }
  catch (e) { return { el: h("div.page", noBuildBox(e)) }; }
  const docsMode = params.docs === "1";
  const state = { section: params.section || "", domain: params.domain || "", doc: params.doc || "", tag: params.tag || "",
    standalone: params.standalone === "1", sort: params.sort || (docsMode ? "count" : "doc"), offset: Number(params.offset) || 0, sub: params.sub || "" };
  const LIM = 50;
  const el = h("div.page");
  el.appendChild(h("div.crumb", h("a", { href: "#/concepts" }, t("nav.concepts")), " / ", h("a", { href: `#/concepts/${encodeURIComponent(c.id)}` }, c.label), " / ", docsMode ? t("concept.docs") : t("sent.title")));
  el.appendChild(h("h1", c.label, " ", h("span.muted", { style: "font-weight:400;font-size:15px" }, c.zh || "")));
  el.appendChild(h("div.tabs", h("a" + (docsMode ? "" : ".active"), { href: `#/concepts/${encodeURIComponent(c.id)}/sentences` }, t("sent.title")),
    h("a" + (docsMode ? ".active" : ""), { href: `#/concepts/${encodeURIComponent(c.id)}/sentences?docs=1` }, t("concept.docs"))));
  const secSel = h("select", h("option", { value: "" }, t("sent.allSections")), ...Object.keys(i18n.sections).filter((s) => s !== "body").map((s) => h("option", { value: s }, i18n.section(s))));
  const domSel = h("select", h("option", { value: "" }, t("concepts.allDomains")), ...(i18n.domains || []).map((d) => h("option", { value: d.id }, dataLabel(d.zh, d.en))));
  const docInp = h("input", { type: "search", placeholder: t("sent.doc"), value: state.doc, style: "width:130px" });
  const tagSel = h("select", h("option", { value: "" }, t("sent.allTags")), ...Object.keys(i18n.tags).map((x) => h("option", { value: x }, i18n.tag(x))));
  const saChk = h("input", { type: "checkbox", checked: state.standalone });
  const sortSel = docsMode
    ? h("select", h("option", { value: "count" }, t("concepts.col.papers")), h("option", { value: "year" }, t("sent.sortYear")), h("option", { value: "title" }, t("docs.col.title")))
    : h("select", h("option", { value: "doc" }, t("sent.sortDoc")), h("option", { value: "year" }, t("sent.sortYear")), h("option", { value: "section" }, t("sent.sortSection")));
  secSel.value = state.section; domSel.value = state.domain; tagSel.value = state.tag; sortSel.value = state.sort;
  const total = h("span.muted");
  const box = h("div.results");
  const prev = h("button.btn.small", { type: "button", on: { click: () => { state.offset = Math.max(0, state.offset - LIM); load(); } } }, "‹ " + t("common.prevPage"));
  const next = h("button.btn.small", { type: "button", on: { click: () => { state.offset += LIM; load(); } } }, t("common.nextPage") + " ›");
  el.appendChild(h("div.filters", docsMode ? null : [secSel, tagSel, h("label.row.tight", saChk, t("sent.standaloneOnly"))], domSel, docsMode ? null : docInp,
    h("span.muted", t("sent.sort") + ":"), sortSel, state.sub ? h("a.chip.on", { href: `#/subdomains/${encodeURIComponent(state.sub)}` }, "SD: " + state.sub) : null, total));
  el.appendChild(box); el.appendChild(h("div.pager", prev, next));
  let seq = 0;
  function syncHash() {
    replaceHash(["concepts", c.id, "sentences"], { docs: docsMode ? 1 : undefined, section: state.section || undefined, domain: state.domain || undefined,
      doc: state.doc || undefined, tag: state.tag || undefined, standalone: state.standalone ? 1 : undefined, sort: state.sort, offset: state.offset || undefined, sub: state.sub || undefined });
  }
  async function load() {
    const my = ++seq;
    state.section = secSel.value; state.domain = domSel.value; state.doc = docInp.value.trim(); state.tag = tagSel.value; state.standalone = saChk.checked; state.sort = sortSel.value;
    syncHash();
    clear(box).appendChild(h("div.loading", t("common.loading")));
    try {
      if (docsMode) {
        const res = await api.get(`concepts/${encodeURIComponent(c.id)}/docs`, { scope: state.domain || undefined, sort: state.sort, limit: LIM, offset: state.offset });
        if (my !== seq) return;
        total.textContent = t("sd.docTotal", { n: res.total });
        clear(box);
        if (!res.rows.length) box.appendChild(h("div.empty", t("sent.empty")));
        const secs = ["title", "keywords", "abstract", "introduction", "methods", "results", "discussion", "conclusion", "other", "body"];
        box.appendChild(h("div.tw", h("table.t", h("thead", h("tr", h("th", t("docs.col.title")), h("th.num", t("docs.col.year")), h("th", t("docs.col.id")), ...secs.map((s) => h("th.num", i18n.section(s))), h("th.num", "refs"))),
          h("tbody", res.rows.map((r) => h("tr.click", { on: { click: () => go(["docs", r.doc_id], { c: c.id }) } }, h("td", clip(r.title || r.doc_id, 100)), h("td.num", r.year ?? ""), h("td.mono", r.doc_id),
            ...secs.map((s) => h("td.num", r[s] ? String(r[s]) : "")), h("td.num", r.refs ? String(r.refs) : "")))))));
        prev.disabled = state.offset <= 0; next.disabled = state.offset + LIM >= res.total;
        return;
      }
      const res = await api.get(`concepts/${encodeURIComponent(c.id)}/sentences`, { section: state.section || undefined, domain: state.domain || undefined,
        doc: state.doc || undefined, tag: state.tag || undefined, standalone: state.standalone ? 1 : undefined, sort: state.sort, limit: LIM, offset: state.offset, sub: state.sub || undefined });
      if (my !== seq) return;
      total.textContent = t("sent.total", { n: res.total });
      clear(box);
      if (!res.rows.length) box.appendChild(h("div.empty", t("sent.empty")));
      for (const r of res.rows) box.appendChild(exampleRow({ ...r, tags: r.tags }, c.id));
      prev.disabled = state.offset <= 0; next.disabled = state.offset + LIM >= res.total;
    } catch (e) { if (my === seq) clear(box).appendChild(noBuildBox(e)); }
  }
  for (const s of [secSel, domSel, tagSel, sortSel]) s.addEventListener("change", () => { state.offset = 0; load(); });
  saChk.addEventListener("change", () => { state.offset = 0; load(); });
  docInp.addEventListener("input", debounce(() => { state.offset = 0; load(); }, 300));
  load();
  return { el, update() { /* own hash */ } };
}
