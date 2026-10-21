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
 * Switching notes (load / reset) first flushes a pending autosave of the previous note; a save that completes
 * after the editor moved on never re-attaches its note (session token), so the next keystroke cannot land in
 * the wrong note.
 */
export function createEditor(opts = {}) {
  const state = { noteId: null, rev: null, targets: opts.targets || [], dirty: false, saving: false, conflict: null, session: 0 };
  const lab = h("div.lab");
  const title = h("input", { type: "text", placeholder: t("notes.titlePh"), maxlength: 300 });
  const body = h("textarea", { placeholder: t("notes.bodyPh"), spellcheck: false });
  const tags = h("input", { type: "text", placeholder: t("notes.tagsPh") });
  const status = h("div.status");
  const saveBtn = h("button.btn.small", { type: "button", on: { click: () => save(true) } }, t("notes.save"));
  const actions = h("div.row.tight", saveBtn);
  const el = h("div.editor", lab, title, body, tags, status, actions);

  function setStatus(text, cls) {
    status.className = "status" + (cls ? " " + cls : "");
    clear(status);
    status.appendChild(document.createTextNode(text));
  }

  function renderLab() {
    clear(lab);
    if (state.noteId !== null) lab.appendChild(document.createTextNode(t("notes.editing", { id: state.noteId })));
    else lab.appendChild(document.createTextNode(t("notes.newFor", { ref: (state.targets || []).join(", ") || t("notes.noTargets") })));
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
    const session = state.session;
    const noteId = state.noteId;
    state.saving = true;
    state.dirty = false;
    setStatus(t("notes.saving"));
    try {
      let note;
      let created = false;
      if (noteId === null) {
        note = await api.post("notes", { ...p, targets: state.targets });
        created = true;
      } else {
        const res = await api.put(`notes/${encodeURIComponent(noteId)}`, { rev: state.rev, ...p, explicit });
        note = res.note;
      }
      if (state.session === session) {        // the editor still shows this note
        state.noteId = note.note_id;
        state.rev = note.rev;
        setStatus(t("notes.saved", { rev: note.rev }), "ok");
        renderLab();
      }
      if (opts.onSaved) opts.onSaved(note, created);
    } catch (e) {
      if (state.session !== session) return;  // the editor moved on; nothing to show for the old note
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
    autosave.flush();                          // save what was typed in the previous note first
    state.session += 1;
    state.noteId = note ? note.note_id : null;
    state.rev = note ? note.rev : null;
    state.conflict = null;
    state.dirty = false;
    title.value = note ? note.title || "" : "";
    body.value = note ? note.body_md || "" : "";
    tags.value = note ? (note.tags || []).join(", ") : "";
    setStatus(note ? t("notes.saved", { rev: note.rev }) : "");
    renderLab();
  }

  function reset(targets) {
    load(null);                                // flushes a non-empty draft before clearing (see load)
    if (targets) state.targets = targets;
    renderLab();
  }

  function flush() { autosave.flush(); }

  if (opts.note) load(opts.note); else renderLab();
  return { el, load, reset, flush, focus: () => body.focus(), get noteId() { return state.noteId; },
           setTargets: (tg) => { state.targets = tg; renderLab(); } };
}
