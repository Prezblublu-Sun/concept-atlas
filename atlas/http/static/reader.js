// Reader view: page list | rendered page (PNG with highlight) or sentence text view | notes panel.
import { api, h, clear, t, i18n, markPhrase, clip, store, replaceHash, go, fmtDate } from "./core.js";
import { createEditor } from "./editor.js";

/** Data labels from the build's i18n table (duplicated from concepts.js to keep the module graph acyclic:
 *  concepts.js imports the badges below). */
function dataLabel(zh, en) { return i18n.lang === "en" ? (en || i18n.data(zh) || zh || "") : (zh || en || ""); }
function domainLabel(id) {
  const d = (i18n.domains || []).find((x) => x.id === id);
  return d ? dataLabel(d.zh, d.en) : (id || "");
}

const ZOOM_KEY = "atlas.zoom";
const MODE_KEY = "atlas.readerMode";

export function sectionBadge(section, isBody) {
  return h("span.badge" + (isBody === false ? ".warn" : ""), i18n.section(section));
}

export function tagBadges(tags) {
  return (tags || []).map((tg) => h("span.badge.tag", i18n.tag(tg)));
}

export async function readerView(docId, params) {
  let doc;
  try {
    doc = await api.get(`docs/${encodeURIComponent(docId)}`);
  } catch (e) {
    return { el: h("div.page", h("div.error-box", `${t("common.error")}: ${e.message}`)) };
  }
  const nPages = doc.pages.length || doc.n_pages || 0;
  const state = {
    page: clampPage(Number(params.page) || 1, nPages),
    mode: store(MODE_KEY) || "image",
    zoom: Number(store(ZOOM_KEY)) || 1.6,
    hl: params.hl || "",
    sid: params.sid || null,
    c: (params.c || "").split(",").map((x) => x.trim()).filter(Boolean),   // concept ids to mark (increment 2)
    pageCache: new Map(),
    objectUrl: null,
  };
  if (!doc.has_pdf && state.mode === "image" && nPages) state.mode = "text";

  // ---- left: pages
  const pagesPane = h("div.pages-pane", h("div.pane-title", t("reader.pages")));
  const pageButtons = new Map();
  for (const p of doc.pages) {
    const b = h("div.pg", { on: { click: () => setPage(p.page_no) } }, h("span", String(p.page_no)), h("span.n", String(p.n_sentences)));
    pageButtons.set(p.page_no, b);
    pagesPane.appendChild(b);
  }
  if (!doc.pages.length) pagesPane.appendChild(h("div.empty", t("reader.notIngested")));

  // ---- center: header + toolbar + viewer
  const metaBits = [doc.year, doc.authors, doc.venue].filter(Boolean).join(" · ");
  const chips = h("div.chips", { style: "margin-top:4px" });
  function renderChips() {
    clear(chips);
    for (const d of doc.domains || []) chips.appendChild(h("span.badge", { title: t("doc.domains") }, domainLabel(d)));
    for (const s of doc.subdomains || []) chips.appendChild(h("a.chip" + (s.tier === "core" ? ".on" : ""),
      { href: `#/subdomains/${encodeURIComponent(s.sub_id)}`, title: `${t("doc.subdomains")} · ${s.sub_id} · ${t("sd." + s.tier)}` }, dataLabel(s.zh, s.en)));
    for (const c of doc.top_concepts || []) {
      const on = state.c.includes(c.id);
      chips.appendChild(h("a.chip" + (on ? ".on" : ""), { href: "#", title: (c.zh || "") + " · " + t("doc.markHint"), on: { click: (ev) => { ev.preventDefault(); toggleConcept(c.id); } } }, c.label, h("span.n", String(c.n))));
    }
    if (state.c.length) chips.appendChild(h("button.btn.small", { type: "button", on: { click: () => { state.c = []; syncHash(); renderChips(); state.pageCache.clear(); render(); } } }, t("doc.clearMarks")));
  }
  // chapter units (thesis doctype): heading, pages, kind, confidence; click → first page of the chapter
  const chapters = (doc.units || []).filter((u) => u.kind !== "document");
  const unitsBox = chapters.length ? h("details.units", h("summary", t("doc.chapters", { n: chapters.length })),
    h("div.chapter-list", chapters.map((u) => h("div.chapter", { on: { click: () => setPage(u.page_from || 1) } },
      h("span.no", u.ordinal !== null && u.ordinal !== undefined ? String(u.ordinal) : ""), h("span.hd", u.heading || u.unit_id),
      u.unit_kind ? h("span.badge", t("unit.kind." + u.unit_kind)) : null,
      h("span.muted", u.page_from ? (u.page_to && u.page_to !== u.page_from ? t("doc.pagesRange", { a: u.page_from, b: u.page_to }) : t("common.page", { p: u.page_from })) : ""),
      u.confidence !== null && u.confidence !== undefined ? h("span.badge" + (u.confidence < 0.7 ? ".warn" : ""), t("unit.confidence", { c: Math.round(u.confidence * 100) })) : null)))) : null;
  function toggleConcept(cid) {
    state.c = state.c.includes(cid) ? state.c.filter((x) => x !== cid) : [...state.c, cid];
    syncHash(); renderChips(); state.pageCache.clear(); render();
  }
  const head = h("div.reader-head",
    h("div.crumb", h("a", { href: "#/docs" }, t("nav.docs")), " / ", h("span.mono", doc.doc_id),
      doc.doc_type && doc.doc_type !== "paper" ? [" · ", h("span.badge", t("doctype." + doc.doc_type))] : null),
    h("div.reader-title", doc.title || t("common.untitled")),
    h("div.reader-meta", metaBits, doc.doi ? [" · ", h("a", { href: "https://doi.org/" + doc.doi, target: "_blank", rel: "noopener" }, doc.doi)] : null),
    chips, unitsBox,
  );
  renderChips();
  const pageInput = h("input.pageno", { type: "number", min: 1, max: Math.max(1, nPages), value: state.page,
    on: { change: () => setPage(Number(pageInput.value)) } });
  const pageOf = h("span.muted");
  const hlInfo = h("span.hl-info");
  const modeImg = h("button.btn.small", { type: "button", on: { click: () => setMode("image") } }, t("reader.modeImage"));
  const modeTxt = h("button.btn.small", { type: "button", on: { click: () => setMode("text") } }, t("reader.modeText"));
  const hlInput = h("input", { type: "search", placeholder: t("reader.highlight"), value: state.hl, style: "width:160px;height:28px",
    on: { change: () => { state.hl = hlInput.value.trim(); syncHash(); render(); } } });
  const pdfLink = doc.has_pdf
    ? h("a.btn.small", { href: api.url(`pdf/${encodeURIComponent(doc.doc_id)}`), target: "_blank", rel: "noopener" }, t("reader.openPdf"))
    : h("span.badge.warn", t("reader.noPdf"));
  const toolbar = h("div.toolbar",
    h("button.btn.small", { type: "button", title: t("reader.prev"), on: { click: () => setPage(state.page - 1) } }, "‹"),
    pageInput, pageOf,
    h("button.btn.small", { type: "button", title: t("reader.next"), on: { click: () => setPage(state.page + 1) } }, "›"),
    h("span.seg", modeImg, modeTxt),
    h("button.btn.small", { type: "button", title: t("reader.zoomOut"), on: { click: () => setZoom(state.zoom / 1.25) } }, "−"),
    h("button.btn.small", { type: "button", title: t("reader.zoomIn"), on: { click: () => setZoom(state.zoom * 1.25) } }, "+"),
    hlInput, hlInfo,
    h("span.spacer"),
    h("button.btn.small", { type: "button", title: t("reader.searchInDocTitle"),
      on: { click: () => go(["search"], { phrase: hlInput.value.trim() || state.hl || undefined, doc: doc.doc_id }) } },
      t("reader.searchInDoc")),
    pdfLink,
  );
  const viewer = h("div.viewer");
  const center = h("div.center", head, toolbar, viewer);

  // ---- right: notes panel
  const panel = createNotesPanel(doc, state);

  const el = h("div.reader", pagesPane, center, panel.el);

  function clampPage(p, n) { return Math.max(1, Math.min(p || 1, Math.max(1, n))); }
  function syncHash() {
    replaceHash(["docs", doc.doc_id], { page: state.page, hl: state.hl || undefined, sid: state.sid || undefined, c: state.c.length ? state.c.join(",") : undefined });
  }
  function setPage(p) {
    p = clampPage(p, nPages);
    if (p === state.page) return;
    state.page = p;
    state.sid = null;
    syncHash();
    render();
    panel.setSelection(null);
  }
  function setMode(m) { state.mode = m; store(MODE_KEY, m); render(); }
  function setZoom(z) {
    state.zoom = Math.max(0.5, Math.min(4, Math.round(z * 100) / 100));
    store(ZOOM_KEY, String(state.zoom));
    if (state.mode === "image") render();
  }

  async function pageData(n) {
    if (!state.pageCache.has(n)) {
      state.pageCache.set(n, api.get(`docs/${encodeURIComponent(doc.doc_id)}/pages/${n}`, { c: state.c.length ? state.c.join(",") : undefined }));
    }
    return state.pageCache.get(n);
  }

  /** Stored sentence text with the concept occurrences (and the phrase highlight) marked. */
  function sentenceText(s) {
    const occ = (s.concepts || []).filter((o) => o.cend > o.cstart).sort((a, b) => a.cstart - b.cstart);
    if (!occ.length) return markPhrase(s.text, state.hl);
    const frag = document.createDocumentFragment();
    const text = String(s.text ?? "");
    let i = 0;
    for (const o of occ) {
      if (o.cstart < i) continue;
      if (o.cstart > i) frag.appendChild(markPhrase(text.slice(i, o.cstart), state.hl));
      const m = h("mark.occ" + (o.nested_in ? ".nested" : ""), text.slice(o.cstart, o.cend));
      m.title = o.id + (o.nested_in ? ` (${t("concept.nested")} ${o.nested_in})` : "");
      frag.appendChild(m);
      i = o.cend;
    }
    if (i < text.length) frag.appendChild(markPhrase(text.slice(i), state.hl));
    return frag;
  }

  let renderSeq = 0;
  async function render() {
    const seq = ++renderSeq;
    pageInput.value = state.page;
    pageInput.max = Math.max(1, nPages);
    clear(pageOf).appendChild(document.createTextNode(t("reader.pageOf", { p: state.page, n: nPages })));
    for (const [n, b] of pageButtons) b.classList.toggle("active", n === state.page);
    modeImg.classList.toggle("active", state.mode === "image");
    modeTxt.classList.toggle("active", state.mode === "text");
    pageButtons.get(state.page)?.scrollIntoView({ block: "nearest" });
    clear(hlInfo);
    clear(viewer).appendChild(h("div.loading", t("common.loading")));
    if (!nPages) { clear(viewer).appendChild(h("div.empty", t("reader.notIngested"))); return; }
    try {
      if (state.mode === "image") await renderImage(seq);
      else await renderText(seq);
    } catch (e) {
      if (seq === renderSeq) clear(viewer).appendChild(h("div.error-box", `${t("common.error")}: ${e.message}`));
    }
  }

  async function renderImage(seq) {
    if (!doc.has_pdf) { clear(viewer).appendChild(h("div.empty", t("reader.noPdf"))); return; }
    let spans;
    if (state.sid || state.c.length) {
      const pd = await pageData(state.page);
      const parts = [];
      const s = pd.sentences.find((x) => x.sid === state.sid);
      if (s) parts.push(`${s.cstart}-${s.cend}`);
      if (state.c.length) for (const x of pd.sentences) if ((x.concepts || []).length && x.sid !== state.sid) parts.push(`${x.cstart}-${x.cend}`);
      if (parts.length) spans = parts.slice(0, 200).join(",");
    }
    const res = await api.png(`docs/${encodeURIComponent(doc.doc_id)}/pages/${state.page}.png`,
      { hl: state.hl || undefined, spans, zoom: state.zoom });
    if (seq !== renderSeq) return;
    if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
    state.objectUrl = URL.createObjectURL(res.blob);
    const img = h("img", { src: state.objectUrl, alt: `${doc.doc_id} p.${state.page}` });
    clear(viewer).appendChild(img);
    if (state.hl || spans) {
      hlInfo.textContent = res.hits ? t("reader.hits", { n: res.hits }) : t("reader.noHits");
    }
    if (res.hitY !== null && res.hits) {
      img.addEventListener("load", () => { viewer.scrollTop = Math.max(0, res.hitY * img.clientHeight - 80); }, { once: true });
    }
  }

  async function renderText(seq) {
    const pd = await pageData(state.page);
    if (seq !== renderSeq) return;
    const box = h("div.sents");
    if (!pd.sentences.length) box.appendChild(h("div.empty", t("reader.noText")));
    let selectedEl = null;
    let nMarked = 0;
    for (const s of pd.sentences) {
      nMarked += (s.concepts || []).length;
      const row = h("div.sent" + (s.is_body ? "" : ".dim"), { data: { sid: s.sid } },
        h("div.meta", h("span.seq", "#" + s.seq), sectionBadge(s.section, s.is_body), tagBadges(s.tags),
          s.notes ? h("span.badge.accent", t("sent.notes", { n: s.notes })) : null,
          s.is_body ? null : h("span.badge.warn", t("sent.reference"))),
        h("div.txt", sentenceText(s)),
      );
      row.addEventListener("click", () => select(s, row));
      if (s.sid === state.sid) { row.classList.add("sel"); selectedEl = row; }
      box.appendChild(row);
    }
    clear(viewer).appendChild(box);
    viewer.insertBefore(h("div.muted", { style: "font-size:12px;margin:0 0 6px 4px" }, t("reader.selectHint"), " · ", t("reader.sentences", { n: pd.sentences.length }),
      state.c.length ? [" · ", t("reader.marked", { n: nMarked })] : null), box);
    if (selectedEl) selectedEl.scrollIntoView({ block: "center" });
  }

  /** Push the sentence named by state.sid (or nothing) to the notes panel — independent of the view mode. */
  function syncSelection() {
    if (!state.sid) { panel.setSelection(null); return; }
    const sid = state.sid;
    pageData(state.page).then((pd) => {
      if (state.sid !== sid) return;
      const s = pd.sentences.find((x) => x.sid === sid);
      panel.setSelection(s || null, false);
    }).catch(() => {});
  }

  function select(s, row) {
    const was = state.sid === s.sid;
    state.sid = was ? null : s.sid;
    viewer.querySelectorAll(".sent.sel").forEach((x) => x.classList.remove("sel"));
    if (!was) row.classList.add("sel");
    syncHash();
    panel.setSelection(was ? null : s);
  }

  // initial selection → notes panel (works in both modes)
  if (state.sid) syncSelection();
  render();

  return {
    el,
    update(p) {
      const page = clampPage(Number(p.page) || 1, nPages);
      const sid = p.sid || null;
      const cs = (p.c || "").split(",").map((x) => x.trim()).filter(Boolean);
      const cChanged = cs.join(",") !== state.c.join(",");
      const changed = page !== state.page || (p.hl || "") !== state.hl || sid !== state.sid || cChanged;
      const sidChanged = sid !== state.sid;
      state.page = page; state.hl = p.hl || ""; state.sid = sid; state.c = cs;
      hlInput.value = state.hl;
      if (cChanged) { state.pageCache.clear(); renderChips(); }
      if (changed) { render(); if (sidChanged) syncSelection(); }
    },
    destroy() { if (state.objectUrl) URL.revokeObjectURL(state.objectUrl); panel.flush(); },
  };
}

// ---------------------------------------------------------------- notes panel
function createNotesPanel(doc, state) {
  let selection = null;   // sentence dict from the page endpoint or null (→ whole document)
  const targetBox = h("div.target");
  const list = h("div.list");
  const editor = createEditor({ targets: [docRef()], onSaved: (note, created) => { if (created) reloadList(); else refreshCard(note); } });
  const collSelect = h("select", { style: "max-width:180px;height:28px" });
  const addBtn = h("button.btn.small", { type: "button", on: { click: addToCollection } }, t("coll.add"));
  const addMsg = h("span.muted", { style: "font-size:12px" });
  const el = h("div.notes-pane",
    h("h3", t("notes.panelTitle")),
    targetBox,
    h("div.row", h("span.muted", t("notes.addTo")), collSelect, addBtn, addMsg),
    list,
    h("div.row", h("button.btn.small", { type: "button", on: { click: () => { editor.reset(); editor.focus(); } } }, t("notes.new"))),
    editor.el,
  );

  function docRef() { return `D:${doc.doc_id}`; }
  function ref() { return selection ? `S:${selection.sid}` : docRef(); }

  async function loadCollections() {
    try {
      const cols = await api.get("collections");
      clear(collSelect);
      collSelect.appendChild(h("option", { value: "" }, t("coll.choose")));
      for (const c of cols) collSelect.appendChild(h("option", { value: c.coll_id }, c.name));
      const last = store("atlas.lastColl");
      if (last && cols.some((c) => c.coll_id === last)) collSelect.value = last;
    } catch { /* keep empty */ }
  }

  async function addToCollection() {
    const id = collSelect.value;
    if (!id) { collSelect.focus(); return; }
    store("atlas.lastColl", id);
    try {
      await api.post(`collections/${encodeURIComponent(id)}/items`, { ref: ref() });
      addMsg.textContent = t("notes.added", { name: collSelect.selectedOptions[0].textContent });
      setTimeout(() => { addMsg.textContent = ""; }, 2500);
    } catch (e) { addMsg.textContent = `${t("common.error")}: ${e.message}`; }
  }

  function renderTarget() {
    clear(targetBox);
    const lab = h("div.lab", h("span", t("notes.target") + ": " + (selection ? t("notes.targetSentence") : t("notes.targetDoc"))),
      h("span.mono", ref()));
    targetBox.appendChild(lab);
    if (selection) {
      targetBox.appendChild(h("div.txt", clip(selection.text, 300)));
      targetBox.appendChild(h("div.row.tight", { style: "margin-top:4px" }, sectionBadge(selection.section, selection.is_body), tagBadges(selection.tags),
        h("button.btn.small", { type: "button", on: { click: () => { state.sid = null; setSelection(null); replaceHash(["docs", doc.doc_id], { page: state.page, hl: state.hl || undefined }); document.querySelectorAll(".sent.sel").forEach((x) => x.classList.remove("sel")); } } }, t("notes.clearSel"))));
    } else {
      targetBox.appendChild(h("div.txt", doc.title || doc.doc_id));
    }
  }

  function noteCard(n) {
    const card = h("div.note-card", { data: { id: n.note_id } },
      h("div.title", n.title || t("common.untitled")),
      h("div.excerpt", clip(n.body_md ?? n.excerpt ?? "", 220)),
      h("div.foot", h("span.badges", (n.tags || []).map((tg) => h("span.badge.tag", tg))),
        h("span.muted", fmtDate(n.updated)),
        h("span.spacer"),
        h("a", { href: `#/notes/${encodeURIComponent(n.note_id)}` }, t("notes.open")),
        h("button.btn.small", { type: "button", on: { click: async () => { const full = await api.get(`notes/${encodeURIComponent(n.note_id)}`); editor.load(full); editor.focus(); } } }, t("notes.edit"))),
    );
    return card;
  }

  async function reloadList() {
    clear(list).appendChild(h("div.loading", t("common.loading")));
    const r = ref();
    try {
      const notes = await api.get("notes/for", { ref: r });
      if (ref() !== r) return;
      clear(list);
      if (!notes.length) list.appendChild(h("div.muted", t("notes.none")));
      for (const n of notes) list.appendChild(noteCard(n));
    } catch (e) { clear(list).appendChild(h("div.error-box", e.message)); }
  }

  function refreshCard(note) {
    const old = list.querySelector(`.note-card[data-id="${CSS.escape(note.note_id)}"]`);
    if (old) old.replaceWith(noteCard(note));
  }

  function setSelection(s, resetEditor = true) {
    selection = s || null;
    renderTarget();
    // a user-made selection change always starts a fresh draft for the new target — also when an existing note
    // was loaded through "Edit" (its pending changes are flushed first), so keystrokes never land on the old
    // target; hash/initial selections (resetEditor = false) only retarget a new draft
    if (resetEditor) editor.reset([ref()]);
    else editor.setTargets([ref()]);
    reloadList();
  }

  renderTarget();
  reloadList();
  loadCollections();
  return { el, setSelection, flush: () => editor.flush() };
}

export function docsLink(docId) { return `#/docs/${encodeURIComponent(docId)}`; }
export { go };
