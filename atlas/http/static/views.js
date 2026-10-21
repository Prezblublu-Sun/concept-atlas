// Documents, search, notes (list + page) and collections (list + page) views.
import { api, h, clear, t, i18n, snippetHtml, md, resolveRefLinks, refHref, fmtDate, clip, debounce, go, store, replaceHash } from "./core.js";
import { createEditor } from "./editor.js";
import { sectionBadge } from "./reader.js";

const errBox = (e) => h("div.error-box", `${t("common.error")}: ${e.message}`);

/** Re-render the children of a scroll container without losing its scroll position. */
function rerender(el, fill) {
  const top = el.scrollTop;
  clear(el);
  fill();
  el.scrollTop = top;
}

// ---------------------------------------------------------------- documents
export async function documentsView(params) {
  const LIMIT = 200;
  const input = h("input", { type: "search", placeholder: t("docs.filter"), value: params.q || "", style: "flex:1;max-width:520px" });
  const total = h("span.muted");
  const table = h("div.tw");
  const more = h("button.btn", { type: "button", hidden: true, on: { click: () => load(false) } }, t("search.more"));
  const el = h("div.page", h("h1", t("docs.title")), h("div.row", { style: "margin:8px 0 12px" }, input, total), table, h("div.pager", more));
  let offset = 0, tbody = null, seq = 0;

  function row(d) {
    return h("tr.click", { on: { click: () => go(["docs", d.doc_id]) } },
      h("td", d.title || h("span.muted", t("common.untitled"))),
      h("td.num", d.year ?? ""),
      h("td", clip(d.authors || "", 60)),
      h("td.mono", d.doc_id),
      h("td.num", d.n_pages ? String(d.n_pages) : h("span.badge.warn", t("docs.notIngested"))),
      h("td.num", String(d.n_sentences || 0)),
      h("td.num", d.notes_count ? h("span.badge.accent", String(d.notes_count)) : ""));
  }

  async function load(first = true) {
    const mySeq = ++seq;
    const q = input.value.trim();
    if (first) {
      offset = 0; tbody = null;
      clear(table).appendChild(h("div.loading", t("common.loading")));
      replaceHash(["docs"], { q: q || undefined });
    }
    more.disabled = true;
    try {
      const res = await api.get("docs", { q: q || undefined, limit: LIMIT, offset });
      if (mySeq !== seq) return;
      total.textContent = t("docs.total", { n: res.total });
      if (first) {
        clear(table);
        if (!res.rows.length) { table.appendChild(h("div.empty", t("docs.empty"))); more.hidden = true; return; }
        tbody = h("tbody");
        table.appendChild(h("table.t",
          h("thead", h("tr", h("th", t("docs.col.title")), h("th.num", t("docs.col.year")), h("th", t("docs.col.authors")),
            h("th", t("docs.col.id")), h("th.num", t("docs.col.pages")), h("th.num", t("docs.col.sentences")), h("th.num", t("docs.col.notes")))),
          tbody));
      }
      for (const d of res.rows) tbody.appendChild(row(d));
      offset += res.rows.length;
      more.hidden = !res.rows.length || offset >= res.total;
    } catch (e) { if (mySeq === seq) clear(table).appendChild(errBox(e)); }
    more.disabled = false;
  }
  input.addEventListener("input", debounce(() => load(true), 250));
  load(true);
  return { el };
}

// ---------------------------------------------------------------- search
export async function searchView(params) {
  const phrase = (params.phrase || "").trim();
  const docId = params.doc || "";
  const el = h("div.page", h("h1", t("search.title")));
  if (docId) {
    // in-document search: its own phrase box (the header box searches the whole corpus)
    const inp = h("input", { type: "search", value: phrase, placeholder: t("search.inDocPh"), style: "flex:1;max-width:520px" });
    const form = h("form.row", { style: "margin:4px 0 12px", on: { submit: (ev) => {
      ev.preventDefault();
      const v = inp.value.trim();
      if (v) go(["search"], { phrase: v, doc: docId });
    } } }, h("span.badge.accent", t("search.inDoc") + " " + docId), inp,
      h("button.btn.primary", { type: "submit" }, t("search.button")),
      h("a.btn", { href: `#/docs/${encodeURIComponent(docId)}` }, t("common.back")),
      phrase ? h("a.btn", { href: "#/search?" + new URLSearchParams({ phrase }) }, t("search.allDocs")) : null);
    el.appendChild(form);
    if (!phrase) { el.appendChild(h("div.empty", t("search.hint"))); return { el, focus: () => inp.focus() }; }
  } else if (!phrase) {
    el.appendChild(h("div.empty", t("search.hint")));
    return { el };
  }
  const summary = h("div.stats");
  const box = h("div.results");
  const more = h("button.btn", { type: "button", hidden: true, on: { click: () => load(false) } }, t("search.more"));
  el.appendChild(summary); el.appendChild(box); el.appendChild(h("div.pager", more));
  let offset = 0;
  const LIMIT = 50;
  async function load(first) {
    more.disabled = true;
    if (first) clear(box).appendChild(h("div.loading", t("common.loading")));
    try {
      const path = docId ? `docs/${encodeURIComponent(docId)}/search` : "search";
      const res = await api.get(path, { phrase, limit: LIMIT, offset });
      if (first) clear(box);
      clear(summary).appendChild(h("span", t("search.summary", { phrase, total: res.total, docs: res.documents })));
      if (!res.rows.length && first) box.appendChild(h("div.empty", t("search.empty")));
      for (const r of res.rows) {
        box.appendChild(h("div.hit", { on: { click: () => go(["docs", r.doc_id], { page: r.page, hl: phrase, sid: r.sid }) } },
          h("div.src", h("b", r.title || r.doc_id), r.year ? h("span", String(r.year)) : null,
            h("span.mono", r.doc_id), h("span", t("common.page", { p: r.page })), sectionBadge(r.section, r.is_body)),
          h("div.snip", { html: snippetHtml(r.snippet || "") })));
      }
      offset += res.rows.length;
      more.hidden = offset >= res.total;
    } catch (e) { clear(box).appendChild(errBox(e)); }
    more.disabled = false;
  }
  load(true);
  return { el };
}

// ---------------------------------------------------------------- notes list
export async function notesView(params) {
  const LIMIT = 100;
  const q = h("input", { type: "search", placeholder: t("notes.searchPh"), value: params.q || "", style: "flex:1;max-width:420px" });
  const tag = h("input", { type: "text", placeholder: t("notes.tagPh"), value: params.tag || "", style: "width:150px" });
  const total = h("span.muted");
  const list = h("div.list");
  const more = h("button.btn", { type: "button", hidden: true, on: { click: () => load(false) } }, t("search.more"));
  const el = h("div.page.narrow", h("h1", t("notes.title")), h("div.row", { style: "margin:8px 0 12px" }, q, tag, total), list, h("div.pager", more));
  let offset = 0, seq = 0;

  function card(n) {
    return h("div.card.click", { on: { click: () => go(["notes", n.note_id]) } },
      h("div.card-h", n.title || t("common.untitled")),
      h("div.muted", { style: "white-space:pre-wrap" }, clip(n.excerpt || "", 200)),
      h("div.row.tight", { style: "margin-top:4px;font-size:12px" },
        h("span.badges", (n.tags || []).map((tg) => h("span.badge.tag", tg))),
        h("span.muted", fmtDate(n.updated)),
        (n.targets || []).length ? h("span.badge", `${n.targets.length} ${t("notes.targets")}`) : h("span.badge", t("notes.noTargets"))));
  }

  async function load(first = true) {
    const mySeq = ++seq;
    const qv = q.value.trim(), tv = tag.value.trim();
    if (first) {
      offset = 0;
      clear(list).appendChild(h("div.loading", t("common.loading")));
      replaceHash(["notes"], { q: qv || undefined, tag: tv || undefined });
    }
    more.disabled = true;
    try {
      const res = await api.get("notes", { q: qv || undefined, tag: tv || undefined, limit: LIMIT, offset });
      if (mySeq !== seq) return;
      total.textContent = t("notes.total", { n: res.total });
      if (first) clear(list);
      if (!res.rows.length && first) { list.appendChild(h("div.empty", t("notes.listEmpty"))); more.hidden = true; return; }
      for (const n of res.rows) list.appendChild(card(n));
      offset += res.rows.length;
      more.hidden = !res.rows.length || offset >= res.total;
    } catch (e) { if (mySeq === seq) clear(list).appendChild(errBox(e)); }
    more.disabled = false;
  }
  const reload = debounce(() => load(true), 250);
  q.addEventListener("input", reload);
  tag.addEventListener("input", reload);
  load(true);
  return { el };
}

// ---------------------------------------------------------------- note page
export async function noteView(noteId) {
  let note;
  try { note = await api.get(`notes/${encodeURIComponent(noteId)}`); }
  catch (e) { return { el: h("div.page.narrow", errBox(e)) }; }
  const el = h("div.page.narrow");
  const editorHost = h("div", { hidden: true });
  let editor = null;

  function targetRow(tg) {
    const snap = tg.snapshot_json || {};
    const href = refHref({ ...snap, ref: tg.ref, kind: snap.kind || tg.ref.split(":")[0] });
    const status = tg.status || "live";
    return h("div.target-row",
      h("span.mono", tg.ref),
      h("span.badge" + (status === "orphan" ? ".bad" : status === "aliased" ? ".warn" : ""), t("status." + status)),
      h("span", clip(tg.snapshot_text || snap.title || "", 160)),
      snap.doc_id ? h("span.muted", `${snap.doc_id}${snap.page ? " p." + snap.page : ""}`) : null,
      href ? h("a.btn.small", { href }, t("notes.goto")) : null);
  }

  function render() {
    rerender(el, () => {
      el.appendChild(h("div.crumb", h("a", { href: "#/notes" }, t("nav.notes")), " / ", h("span.mono", note.note_id)));
      el.appendChild(h("h1", note.title || t("common.untitled")));
      el.appendChild(h("div.stats", h("span", t("notes.rev", { rev: note.rev })), h("span", t("notes.updated", { t: fmtDate(note.updated) })),
        h("span", t("notes.created", { t: fmtDate(note.created) })), h("span.badges", (note.tags || []).map((tg) => h("span.badge.tag", tg)))));
      const body = h("div.card.md", { html: md(note.body_md || "") });
      el.appendChild(h("div.row", { style: "margin-bottom:10px" },
        h("button.btn", { type: "button", on: { click: toggleEdit } }, t("notes.edit")),
        collectionAdder(`N:${note.note_id}`),
        h("span.spacer"),
        h("button.btn.danger", { type: "button", on: { click: del } }, t("notes.delete"))));
      el.appendChild(editorHost);
      el.appendChild(body);
      resolveRefLinks(body);
      el.appendChild(h("h2", t("notes.targets")));
      el.appendChild(h("div.card", note.targets.length ? note.targets.map(targetRow) : h("span.muted", t("notes.noTargets"))));
      if (note.links && note.links.length) {
        el.appendChild(h("h2", t("notes.links")));
        el.appendChild(h("div.card", note.links.map((r) => h("div", h("a.reflink", { href: "#/ref/" + encodeURIComponent(r), data: { ref: r } }, r)))));
        resolveRefLinks(el.lastChild);
      }
      const hist = h("div.card.hist");
      const det = h("details", h("summary", t("notes.history")), hist);
      det.addEventListener("toggle", async () => {
        if (!det.open || hist.childElementCount) return;
        try {
          const rows = await api.get(`notes/${encodeURIComponent(note.note_id)}/history`);
          for (const r of rows) hist.appendChild(h("details", h("summary", `${t("notes.rev", { rev: r.rev })} · ${fmtDate(r.saved)} · ${r.reason || ""} · ${r.title || ""}`), h("pre", r.body_md || "")));
        } catch (e) { hist.appendChild(errBox(e)); }
      });
      el.appendChild(h("h2", t("notes.history")));
      el.appendChild(det);
    });
  }

  function toggleEdit() {
    if (!editor) {
      editor = createEditor({ note, onSaved: (n) => { note = n; } });
      editorHost.appendChild(editor.el);
    } else editor.load(note);
    editorHost.hidden = !editorHost.hidden;
    if (editorHost.hidden) render(); else editor.focus();
  }

  async function del() {
    if (!confirm(t("notes.confirmDelete"))) return;
    try { await api.del(`notes/${encodeURIComponent(note.note_id)}`); go(["notes"]); }
    catch (e) { el.prepend(errBox(e)); }
  }

  render();
  return { el, destroy() { if (editor) editor.flush(); } };
}

/** "add to collection" control reused on the note page. */
function collectionAdder(ref) {
  const sel = h("select", { style: "height:32px;max-width:200px" });
  const msg = h("span.muted", { style: "font-size:12px" });
  const btn = h("button.btn", { type: "button", on: { click: async () => {
    if (!sel.value) return;
    store("atlas.lastColl", sel.value);
    try { await api.post(`collections/${encodeURIComponent(sel.value)}/items`, { ref }); msg.textContent = t("notes.added", { name: sel.selectedOptions[0].textContent }); }
    catch (e) { msg.textContent = e.message; }
  } } }, t("notes.addTo"));
  api.get("collections").then((cols) => {
    sel.appendChild(h("option", { value: "" }, t("coll.choose")));
    for (const c of cols) sel.appendChild(h("option", { value: c.coll_id }, c.name));
    const last = store("atlas.lastColl");
    if (last && cols.some((c) => c.coll_id === last)) sel.value = last;
  }).catch(() => {});
  return h("span.row.tight", sel, btn, msg);
}

// ---------------------------------------------------------------- collections list
export async function collectionsView() {
  const name = h("input", { type: "text", placeholder: t("coll.namePh"), required: true });
  const desc = h("input", { type: "text", placeholder: t("coll.descPh") });
  const list = h("div.list");
  const form = h("form.card.form", { on: { submit: async (ev) => {
    ev.preventDefault();
    if (!name.value.trim()) return;
    try { await api.post("collections", { name: name.value.trim(), description: desc.value.trim() }); name.value = ""; desc.value = ""; load(); }
    catch (e) { list.prepend(errBox(e)); }
  } } }, h("div.card-h", t("coll.new")), h("div.row", name, desc, h("button.btn.primary", { type: "submit" }, t("coll.create"))));
  const el = h("div.page.narrow", h("h1", t("coll.title")), form, h("div", { style: "height:12px" }), list);
  async function load() {
    clear(list).appendChild(h("div.loading", t("common.loading")));
    try {
      const cols = await api.get("collections");
      clear(list);
      if (!cols.length) { list.appendChild(h("div.empty", t("coll.none"))); return; }
      for (const c of cols) {
        list.appendChild(h("div.card.click", { on: { click: () => go(["collections", c.coll_id]) } },
          h("div.card-h", c.name),
          c.description ? h("div.muted", c.description) : null,
          h("div.row.tight", { style: "font-size:12px;margin-top:4px" }, h("span.badge.accent", t("coll.items", { n: c.n_items })),
            h("span.muted", t("coll.updated", { t: fmtDate(c.updated) })), h("span.mono", c.coll_id))));
      }
    } catch (e) { clear(list).appendChild(errBox(e)); }
  }
  load();
  return { el };
}

// ---------------------------------------------------------------- collection page
export async function collectionView(collId) {
  const el = h("div.page.narrow");
  let coll;
  async function load() {
    try { coll = await api.get(`collections/${encodeURIComponent(collId)}`); }
    catch (e) { clear(el).appendChild(errBox(e)); return; }
    render();
  }

  function pointer(item) {
    const snap = item.snapshot || {};
    const href = refHref({ ...snap, ref: item.ref, kind: snap.kind || (item.ref || "").split(":")[0] });
    const bits = [];
    if (snap.doc_id) bits.push(`[${snap.doc_id}${snap.page ? " p." + snap.page : ""}]`);
    if (snap.title && snap.kind !== "D") bits.push(clip(snap.title, 80));
    if (snap.year) bits.push(String(snap.year));
    return h("div.ptr", h("span.mono", item.ref), " ", bits.join(" · "), " ",
      snap.status && snap.status !== "live" ? h("span.badge" + (snap.status === "orphan" ? ".bad" : ".warn"), t("status." + snap.status)) : null,
      href ? [" ", h("a", { href }, t("notes.goto"))] : null);
  }

  async function move(idx, dir) {
    const items = coll.items;
    const j = idx + dir;
    if (j < 0 || j >= items.length) return;
    let seq;
    if (dir < 0) seq = j === 0 ? items[j].seq - 1 : (items[j - 1].seq + items[j].seq) / 2;
    else seq = j === items.length - 1 ? items[j].seq + 1 : (items[j].seq + items[j + 1].seq) / 2;
    try { await api.put(`collections/items/${encodeURIComponent(items[idx].item_id)}`, { seq }); await load(); }
    catch (e) { el.prepend(errBox(e)); }
  }

  async function remove(item) {
    if (!confirm(t("coll.confirmRemove"))) return;
    try { await api.del(`collections/items/${encodeURIComponent(item.item_id)}`); await load(); }
    catch (e) { el.prepend(errBox(e)); }
  }

  function itemCard(item, idx) {
    const snap = item.snapshot || {};
    const ta = h("textarea", { placeholder: t("coll.commentPh") }, item.comment_md || "");
    const st = h("span.muted", { style: "font-size:12px" });
    const save = debounce(async () => {
      st.textContent = t("notes.saving");
      try { await api.put(`collections/items/${encodeURIComponent(item.item_id)}`, { comment_md: ta.value }); item.comment_md = ta.value; st.textContent = t("common.saved"); }
      catch (e) { st.textContent = e.message; }
    }, 700);
    ta.addEventListener("input", () => { st.textContent = t("notes.unsaved"); save(); });
    ta.addEventListener("blur", () => save.flush());
    return h("div.card.item",
      h("div.no", String(idx + 1) + "."),
      h("div",
        item.ref ? [pointer(item), h("blockquote", clip(snap.text || snap.title || "", 600))] : h("div.ptr", t("coll.freeItem")),
        ta, st),
      h("div.ops",
        h("button.btn.small", { type: "button", title: t("coll.up"), disabled: idx === 0, on: { click: () => move(idx, -1) } }, "↑"),
        h("button.btn.small", { type: "button", title: t("coll.down"), disabled: idx === coll.items.length - 1, on: { click: () => move(idx, 1) } }, "↓"),
        h("button.btn.small.danger", { type: "button", title: t("coll.remove"), on: { click: () => remove(item) } }, "×")));
  }

  function render() {
    rerender(el, () => {
      el.appendChild(h("div.crumb", h("a", { href: "#/collections" }, t("nav.collections")), " / ", h("span.mono", coll.coll_id)));
      el.appendChild(h("h1", coll.name));
      if (coll.description) el.appendChild(h("div.muted", coll.description));
      el.appendChild(h("div.row", { style: "margin:10px 0 14px" },
        h("span.badge.accent", t("coll.items", { n: coll.items.length })),
        h("a.btn", { href: api.url(`collections/${encodeURIComponent(coll.coll_id)}/export.md`, { download: 1 }) }, t("coll.export")),
        h("a.btn.small", { href: api.url(`collections/${encodeURIComponent(coll.coll_id)}/export.md`), target: "_blank", rel: "noopener" }, ".md")));
      const list = h("div.list");
      if (!coll.items.length) list.appendChild(h("div.empty", t("coll.empty")));
      coll.items.forEach((it, i) => list.appendChild(itemCard(it, i)));
      el.appendChild(list);
      const ta = h("textarea", { placeholder: t("coll.commentPh") });
      el.appendChild(h("form.card.form", { style: "margin-top:12px", on: { submit: async (ev) => {
        ev.preventDefault();
        if (!ta.value.trim()) return;
        try { await api.post(`collections/${encodeURIComponent(coll.coll_id)}/items`, { comment_md: ta.value }); await load(); }
        catch (e) { el.prepend(errBox(e)); }
      } } }, h("div.card-h", t("coll.addComment")), ta, h("div.row", h("button.btn.primary", { type: "submit" }, t("coll.add")))));
    });
  }

  await load();
  return { el };
}

/** #/ref/<ref> → resolve and jump to the target. */
export async function refView(ref) {
  const el = h("div.page.narrow", h("div.loading", t("ref.resolving")));
  try {
    const obj = await api.get("resolve", { ref });
    const href = refHref(obj);
    if (href && obj.status !== "orphan") { location.replace(href); return { el }; }
    clear(el).appendChild(h("div.card", h("div.card-h", ref), h("div.note-box", t("ref.orphan")),
      h("pre.mono", JSON.stringify(obj, null, 2))));
  } catch (e) { clear(el).appendChild(errBox(e)); }
  return { el };
}
