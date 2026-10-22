// Framework views (increment 2): the three trees (term tree, content framework, sub-domains) and the sub-domain page.
import { api, h, clear, t, i18n, clip, go, replaceHash } from "./core.js";
import { noBuildBox, tierBadge, dataLabel, domainLabel, notesCard } from "./concepts.js";

function node(summaryChildren, body, open = false) {
  const d = h("details", { open: open ? "" : null }, h("summary", summaryChildren), h("div", body));
  return d;
}

function conceptLeaf(c) {
  return h("div.leaf", h("a", { href: `#/concepts/${encodeURIComponent(c.id)}` }, c.label || c.id), c.zh ? h("span.muted", c.zh) : null,
    c.papers !== undefined ? h("span.n", t("fw.papers", { n: c.papers })) : null, c.tier ? tierBadge(c.tier) : null);
}

function expandButtons(root) {
  return h("div.row.tight", { style: "margin:6px 0" },
    h("button.btn.small", { type: "button", on: { click: () => root.querySelectorAll("details").forEach((d) => { d.open = true; }) } }, t("fw.expandAll")),
    h("button.btn.small", { type: "button", on: { click: () => root.querySelectorAll("details").forEach((d) => { d.open = false; }) } }, t("fw.collapseAll")));
}

async function termsTree() {
  const res = await api.get("framework/terms");
  const root = h("div.tree");
  for (const d of res.domains) {
    const secs = d.sections.map((s) => node([h("b", i18n.section(s.section)), h("span.n", t("fw.nTerms", { n: s.n }))],
      s.subs.map((sub) => node([h("span", dataLabel(sub.name)), h("span.n", t("fw.nTerms", { n: sub.n }))],
        sub.terms.map((x) => h("div.leaf", x.concept_id ? h("a", { href: `#/concepts/${encodeURIComponent(x.concept_id)}` }, x.term) : h("span", x.term),
          x.zh ? h("span.muted", x.zh) : null, x.type ? h("span.badge", dataLabel(x.type)) : null, x.note ? h("span.muted", { style: "font-size:12px" }, "— " + dataLabel(x.note)) : null))))));
    root.appendChild(node([h("b", dataLabel(d.zh, d.en)), h("span.mono", d.id), h("span.n", t("fw.nTerms", { n: d.n }))], secs, true));
  }
  return h("div", h("div.muted", { style: "font-size:12.5px" }, t("fw.terms.hint")), expandButtons(root), root);
}

async function categoriesTree() {
  const res = await api.get("framework/categories");
  const root = h("div.tree");
  for (const d of res.domains) {
    const cats = d.categories.map((c) => node([h("span.mono", c.id), h("b", dataLabel(c.zh, c.en)), h("span.n", t("fw.nConcepts", { n: c.n })), c.papers ? h("span.n", t("fw.papers", { n: c.papers })) : null,
      h("a.chip", { href: `#/concepts?cat=${encodeURIComponent(c.id)}`, on: { click: (ev) => ev.stopPropagation() } }, t("nav.concepts"))],
      [c.desc_zh || c.desc_en ? h("div.muted", { style: "font-size:12.5px;margin:2px 6px 6px" }, dataLabel(c.desc_zh, c.desc_en)) : null,
        ...c.concepts.map((x) => h("div.leaf", h("a", { href: `#/concepts/${encodeURIComponent(x.id)}` }, x.label), x.zh ? h("span.muted", x.zh) : null,
          h("span.n", t("fw.papers", { n: x.papers })), tierBadge(x.tier), x.secondary ? h("span.badge", t("concept.secondary")) : null))]));
    root.appendChild(node([h("b", dataLabel(d.zh, d.en)), h("span.mono", d.id), h("span.n", t("fw.n", { n: d.categories.length }))], cats, true));
  }
  return h("div", expandButtons(root), root);
}

async function subdomainsTree() {
  const res = await api.get("framework/subdomains");
  const root = h("div.tree");
  for (const g of res.groups) {
    const subs = g.subs.map((s) => h("div.leaf", h("a", { href: `#/subdomains/${encodeURIComponent(s.id)}` }, dataLabel(s.zh, s.en)), h("span.mono", s.id),
      h("span.n", t("fw.papers", { n: s.papers })), h("span.n", t("fw.core", { n: s.core })), s.desc_zh || s.desc_en ? h("span.muted", { style: "font-size:12px" }, "— " + clip(dataLabel(s.desc_zh, s.desc_en), 120)) : null));
    root.appendChild(node([h("b", dataLabel(g.zh, g.en)), g.papers ? h("span.n", t("fw.papers", { n: g.papers })) : null, h("span.n", t("fw.n", { n: g.subs.length }))], subs, true));
  }
  return h("div", expandButtons(root), root);
}

export async function frameworkView(params) {
  const tab = ["terms", "categories", "subdomains"].includes(params.tab) ? params.tab : "subdomains";
  const el = h("div.page", h("h1", t("fw.title")));
  const tabs = h("div.tabs", ...[["subdomains", "fw.subdomains"], ["categories", "fw.categories"], ["terms", "fw.terms"]].map(([k, lk]) =>
    h("a" + (k === tab ? ".active" : ""), { href: `#/framework?tab=${k}` }, t(lk))));
  el.appendChild(tabs);
  const box = h("div", h("div.loading", t("common.loading")));
  el.appendChild(box);
  try {
    const view = tab === "terms" ? await termsTree() : tab === "categories" ? await categoriesTree() : await subdomainsTree();
    clear(box).appendChild(view);
  } catch (e) { clear(box).appendChild(noBuildBox(e)); }
  return { el };
}

// ---------------------------------------------------------------- sub-domain page
function years(obj) {
  const ys = Object.keys(obj || {}).sort();
  if (!ys.length) return h("div.muted", t("common.none"));
  const max = Math.max(1, ...ys.map((y) => obj[y]));
  const bars = h("div.bars");
  for (const y of ys) {
    bars.appendChild(h("span", y)); bars.appendChild(h("span.bar", h("i", { style: `width:${(100 * obj[y] / max).toFixed(1)}%` }))); bars.appendChild(h("span.n", String(obj[y])));
  }
  return bars;
}

export async function subdomainView(id, params) {
  let s;
  try { s = await api.get(`subdomains/${encodeURIComponent(id)}`); }
  catch (e) { return { el: h("div.page", noBuildBox(e)) }; }
  const el = h("div.page");
  el.appendChild(h("div.crumb", h("a", { href: "#/framework?tab=subdomains" }, t("fw.subdomains")), " / ", h("span.mono", s.id)));
  const main = h("div");
  main.appendChild(h("div.concept-head", h("div", h("span.lab", dataLabel(s.zh, s.en)), h("span.zh", i18n.lang === "en" ? (s.zh || "") : (s.en || "")),
    h("div.row.tight", { style: "margin-top:4px" }, h("span.badge", s.kind === "cross" ? t("fw.cross") : domainLabel(s.domain)), h("span.badge.accent", t("sd.stats", { papers: s.papers, core: s.core, base: s.base })),
      h("span.muted", { style: "font-size:12px" }, s.kind === "cross" ? t("sd.baseCross") : t("sd.baseWithin"))),
    s.desc_zh || s.desc_en ? h("div.muted", { style: "margin-top:6px" }, dataLabel(s.desc_zh, s.desc_en)) : null)));
  const anchors = h("div.card", h("div.card-h", t("sd.anchors")));
  s.anchors.forEach((g, i) => anchors.appendChild(h("div.row.tight", { style: "margin:3px 0" }, s.anchors.length > 1 ? h("span.badge", t("sd.group", { n: i + 1 })) : null,
    g.map((c) => h("a.chip", { href: `#/concepts/${encodeURIComponent(c.id)}`, title: c.zh || "" }, c.label || c.id)))));
  const overlaps = h("div.card", h("div.card-h", t("sd.overlaps")), s.overlaps.length ? h("div.chips", s.overlaps.map((o) => h("a.chip", { href: `#/subdomains/${encodeURIComponent(o.id)}` }, dataLabel(o.zh, o.en), h("span.n", String(o.papers))))) : h("div.muted", t("common.none")));
  main.appendChild(h("div.grid2", anchors, overlaps, h("div.card", h("div.card-h", t("sd.years")), years(s.years))));

  // concepts
  main.appendChild(h("div.row", { style: "margin-top:16px" }, h("h2", { style: "margin:0" }, t("sd.concepts")), h("span.muted", t("fw.nConcepts", { n: s.n_concepts })), h("span.spacer"),
    h("a.btn", { href: `#/concepts?sub=${encodeURIComponent(s.id)}` }, t("concepts.title"))));
  main.appendChild(h("div.tw", h("table.t", h("thead", h("tr", h("th.num", t("concept.rank")), h("th", t("concepts.col.label")), h("th", t("concepts.col.zh")), h("th", t("fw.categories")), h("th.num", t("concept.inSub")), h("th.num", t("concept.coverage")), h("th.num", t("concept.restCoverage")), h("th.num", t("concept.lift")), h("th.num", t("concept.topicRate")), h("th.num", t("concept.score")))),
    h("tbody", s.concepts.map((c) => h("tr", h("td.num", String(c.rank)), h("td", h("a", { href: `#/concepts/${encodeURIComponent(c.concept_id)}` }, c.label)), h("td", c.zh || ""), h("td", c.category ? h("span.badge", c.category) : ""),
      h("td.num", String(c.papers)), h("td.num", (c.coverage * 100).toFixed(1) + "%"), h("td.num", (c.coverage_rest * 100).toFixed(1) + "%"), h("td.num", Number(c.lift).toFixed(2)), h("td.num", (c.topic_rate * 100).toFixed(0) + "%"), h("td.num", Number(c.score).toFixed(3))))))));

  // documents
  const tierSel = h("select", h("option", { value: "" }, t("sd.allTiers")), h("option", { value: "core" }, t("sd.core")), h("option", { value: "related" }, t("sd.related")));
  const q = h("input", { type: "search", placeholder: t("docs.filter"), style: "width:260px" });
  const list = h("div.results");
  const total = h("span.muted");
  const more = h("button.btn", { type: "button", hidden: true, on: { click: () => load(false) } }, t("search.more"));
  let offset = 0, seq = 0;
  async function load(first = true) {
    const my = ++seq;
    if (first) { offset = 0; clear(list).appendChild(h("div.loading", t("common.loading"))); }
    try {
      const res = await api.get(`subdomains/${encodeURIComponent(s.id)}/docs`, { tier: tierSel.value || undefined, q: q.value.trim() || undefined, limit: 50, offset });
      if (my !== seq) return;
      if (first) clear(list);
      total.textContent = t("sd.docTotal", { n: res.total });
      if (!res.rows.length && first) list.appendChild(h("div.empty", t("docs.empty")));
      for (const r of res.rows) list.appendChild(h("div.hit", { on: { click: () => go(["docs", r.doc_id], { c: r.hits.map((x) => x.id).join(",") }) } },
        h("div.src", h("b", clip(r.title || r.doc_id, 110)), r.year ? h("span", String(r.year)) : null, h("span.mono", r.doc_id), r.kind === "chapter" ? h("span.badge", `${t("unit.chapter")}: ${clip(r.heading || "", 50)}`) : null,
          h("span.badge" + (r.tier === "core" ? ".accent" : ""), t("sd." + r.tier))),
        h("div.chips", { style: "margin-top:3px" }, r.hits.map((x) => h("span.chip", x.label, h("span.n", String(x.n)))))));
      offset += res.rows.length;
      more.hidden = offset >= res.total;
    } catch (e) { clear(list).appendChild(noBuildBox(e)); }
  }
  tierSel.addEventListener("change", () => load(true));
  q.addEventListener("input", (() => { let tm; return () => { clearTimeout(tm); tm = setTimeout(() => load(true), 300); }; })());
  main.appendChild(h("div.row", { style: "margin-top:16px" }, h("h2", { style: "margin:0" }, t("sd.docs")), tierSel, q, total));
  main.appendChild(list); main.appendChild(h("div.pager", more));
  load(true);
  const side = h("div.side", notesCard(`SD:${s.id}`, s.zh));
  el.appendChild(h("div.concept-page", main, side));
  return { el };
}
