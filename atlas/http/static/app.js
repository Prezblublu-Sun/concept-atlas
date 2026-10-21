// concept-atlas SPA entry: header, language toggle, hash router.
import { api, i18n, t, h, clear, parseHash, go, replaceHash } from "./core.js";
import { readerView } from "./reader.js";
import { documentsView, searchView, notesView, noteView, collectionsView, collectionView, refView } from "./views.js";

const main = document.getElementById("view");
let current = { key: null, view: null };

function applyChrome() {
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => { el.placeholder = t(el.dataset.i18nPh); });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });
  const lb = document.getElementById("lang-btn");
  lb.textContent = t("lang.toggle");
  lb.title = t("lang.toggle.title");
}

async function loadInfo() {
  try {
    const info = await api.get("info");
    document.getElementById("project-name").textContent = info.project || t("app.name");
    document.title = `${info.project} · ${t("app.name")}`;
    document.getElementById("project-sub").textContent = t("info.stats", info.stats || {});
  } catch (e) {
    document.getElementById("project-sub").textContent = `${t("common.error")}: ${e.message}`;
  }
}

function setActiveNav(top) {
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === top));
}

async function route() {
  let { path, params, key } = parseHash();
  if (!path.length) {                 // '#/' is the documents view; settle the hash so views may persist filters
    replaceHash(["docs"], params);
    ({ path, params, key } = parseHash());
  }
  const [top, id] = path;
  if (current.key === key && current.view && current.view.update) {
    current.view.update(params);
    return;
  }
  if (current.view && current.view.destroy) current.view.destroy();
  current = { key, view: null };
  clear(main).appendChild(h("div.loading", t("common.loading")));
  let view;
  try {
    if (!top || top === "docs" && !id) { setActiveNav("docs"); view = await documentsView(params); }
    else if (top === "docs") { setActiveNav("docs"); view = await readerView(id, params); }
    else if (top === "search") { setActiveNav(""); view = await searchView(params); }
    else if (top === "notes" && !id) { setActiveNav("notes"); view = await notesView(params); }
    else if (top === "notes") { setActiveNav("notes"); view = await noteView(id); }
    else if (top === "collections" && !id) { setActiveNav("collections"); view = await collectionsView(); }
    else if (top === "collections") { setActiveNav("collections"); view = await collectionView(id); }
    else if (top === "ref" && id) { view = await refView(path.slice(1).join("/")); }
    else { view = { el: h("div.page", h("div.empty", t("common.notFound"))) }; }
  } catch (e) {
    view = { el: h("div.page", h("div.error-box", `${t("common.error")}: ${e.message}`)) };
  }
  if (parseHash().key !== key) return; // navigated away while loading
  current.view = view;
  clear(main).appendChild(view.el);
}

function wireHeader() {
  const form = document.getElementById("search-form");
  const input = document.getElementById("search-input");
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const phrase = input.value.trim();
    if (phrase) go(["search"], { phrase });
  });
  document.getElementById("lang-btn").addEventListener("click", () => i18n.toggle());
  i18n.onChange(() => {
    applyChrome();
    loadInfo();
    if (current.view && current.view.destroy) current.view.destroy();   // flush autosave, release object URLs
    current = { key: null, view: null };
    route();
  });
}

async function boot() {
  try {
    await i18n.load();
  } catch (e) {
    clear(main).appendChild(h("div.page", h("div.error-box", `i18n: ${e.message}`)));
  }
  applyChrome();
  wireHeader();
  loadInfo();
  window.addEventListener("hashchange", route);
  route();
}

boot();
