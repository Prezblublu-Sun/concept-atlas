// Note editor with debounced autosave: POST on first content, PUT with rev afterwards, 409 → conflict UI.
import { api, ApiError, debounce, h, clear, t } from "./core.js";

const AUTOSAVE_MS = 800;

function parseTags(s) {
  return String(s || "").split(/[,，;；]/).map((x) => x.trim().toLowerCase()).filter(Boolean);
}

/**
 * createEditor({targets, note, onSaved, onDeleted}) → {el, load(note), reset(), flush()}
 *  - targets: refs used when the note is first created (ignored for an existing note)
 *  - note: existing note dict to edit (optional)
 *  - onSaved(note, created): after every successful save
 */
export function createEditor(opts = {}) {
  const state = { noteId: null, rev: null, targets: opts.targets || [], dirty: false, saving: false, conflict: null };
  const title = h("input", { type: "text", placeholder: t("notes.titlePh"), maxlength: 300 });
  const body = h("textarea", { placeholder: t("notes.bodyPh"), spellcheck: false });
  const tags = h("input", { type: "text", placeholder: t("notes.tagsPh") });
  const status = h("div.status");
  const saveBtn = h("button.btn.small", { type: "button", on: { click: () => save(true) } }, t("notes.save"));
  const actions = h("div.row.tight", saveBtn);
  const el = h("div.editor", title, body, tags, status, actions);

  function setStatus(text, cls) {
    status.className = "status" + (cls ? " " + cls : "");
    clear(status);
    status.appendChild(document.createTextNode(text));
  }

  function payload() {
    return { title: title.value.trim(), body_md: body.value, tags: parseTags(tags.value) };
  }

  function isEmpty(p) { return !p.title && !p.body_md.trim() && !p.tags.length; }

  async function save(explicit = false) {
    if (state.saving) { state.dirty = true; return; }
    if (state.conflict) return;
    const p = payload();
    if (state.noteId === null && isEmpty(p)) { setStatus(t("notes.empty")); return; }
    state.saving = true;
    state.dirty = false;
    setStatus(t("notes.saving"));
    try {
      let note;
      let created = false;
      if (state.noteId === null) {
        note = await api.post("notes", { ...p, targets: state.targets });
        created = true;
      } else {
        const res = await api.put(`notes/${encodeURIComponent(state.noteId)}`, { rev: state.rev, ...p, explicit });
        note = res.note;
      }
      state.noteId = note.note_id;
      state.rev = note.rev;
      setStatus(t("notes.saved", { rev: note.rev }), "ok");
      if (opts.onSaved) opts.onSaved(note, created);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.body && e.body.server) {
        showConflict(e.body.server);
      } else {
        setStatus(`${t("common.error")}: ${e.message}`, "err");
      }
    } finally {
      state.saving = false;
      if (state.dirty && !state.conflict) autosave();
    }
  }

  function showConflict(server) {
    state.conflict = server;
    status.className = "status err";
    clear(status);
    status.appendChild(document.createTextNode(t("notes.conflict", { rev: server.rev })));
    status.appendChild(h("button.btn.small", { type: "button", on: { click: () => { load(server); setStatus(t("notes.saved", { rev: server.rev }), "ok"); } } }, t("notes.reload")));
    status.appendChild(h("button.btn.small", { type: "button", on: { click: () => { state.rev = server.rev; state.conflict = null; save(true); } } }, t("notes.overwrite")));
  }

  const autosave = debounce(() => save(false), AUTOSAVE_MS);
  const onInput = () => { state.dirty = true; if (!state.conflict) { setStatus(t("notes.unsaved")); autosave(); } };
  for (const inp of [title, body, tags]) inp.addEventListener("input", onInput);

  function load(note) {
    state.noteId = note ? note.note_id : null;
    state.rev = note ? note.rev : null;
    state.conflict = null;
    state.dirty = false;
    title.value = note ? note.title || "" : "";
    body.value = note ? note.body_md || "" : "";
    tags.value = note ? (note.tags || []).join(", ") : "";
    setStatus(note ? t("notes.saved", { rev: note.rev }) : "");
  }

  function reset(targets) {
    autosave.cancel();
    if (targets) state.targets = targets;
    load(null);
  }

  function flush() { autosave.flush(); }

  if (opts.note) load(opts.note);
  return { el, load, reset, flush, focus: () => body.focus(), get noteId() { return state.noteId; },
           setTargets: (tg) => { state.targets = tg; } };
}
