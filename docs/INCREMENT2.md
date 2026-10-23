# Increment 2 — derived layer (term system) + thesis doctype + UCL subset

Status: contract for implementation (2026-10-22). Architecture: `H:\UCL_theses\2610 opt\架构设计_2026-10-21\概念体系代码架构设计_2026-10-21.md` §3, §4, §6 (stages 5 `match`, 6 `assign`, 12 `finish`), §7, §8, §14, §16, §17 item 2. Increment 1 (evidence + reader + notes) is in `docs/INCREMENT1.md` and is the base: do not change its ids, schemas or routes except where this document says so.

Goal: the **term system of the previous viewer (v3) runs on the new evidence base** — the 1,605 curated concepts, the term tree (domain → section → sub-structure), the content framework (43 categories) and the 31 sub-domains are imported as `curated/` files, matched on the pinned sentences of a build, counted exactly by the v2 rules (coverage, topic rate, tiers), and browsed in the web UI (concept list with sortable headers, concept page with prev/next, three trees, sub-domain pages). No LLM, 0 RMB. In the same increment the **thesis doctype** is implemented and a **UCL thesis subset** (~120 theses) is registered and ingested so that R1 (chapter units) is validated early.

Legacy data: `H:\UCL_theses\2610 opt\术语阅览系统_v3_2026-10-20\terms_v3.db` (read-only for us). Its matcher is `matcher.py` + `rules_v2.py` in that folder (read them; port the rules, not the code). Counting semantics come from `H:\UCL_theses\2610 opt\术语整理_v2_2026-10-17\_scripts\frequency_importance.py` and `…\术语阅览系统_v2_2026-10-19\_scripts\assign_subdomains.py`.

Conventions: Python 3.12, stdlib + pymupdf/pyyaml/numpy only; `PYTHONUTF8=1`; Windows paths; layering enforced by `tests/test_layering.py` (core/doctypes → store/ingest/llm → query/user/pipeline/verify → service → cli/http/mcp/tools); only `store/db.py` connects to SQLite; stages never write `curated/` (only `atlas curate …` / `atlas legacy export-curated`, both in `tools/` + `pipeline/gates.py`); every rule change bumps a version string.

---

## 1. Curated files (`<project>/curated/`)

All YAML, UTF-8, keys sorted where the writer controls them, lists in a stable order. Ids: concepts `C_<slug>` (keep the v3 ids verbatim — they already have that form), domains `DOM_<slug>`, categories keep v3 ids (`O1`, `F7`, `S3`, `X2` …), sub-domains keep v3 ids (`SUR-GP`, `X-SAO` …).

| file | content |
|---|---|
| `domains.yaml` | `domains: [{id: DOM_ortho, zh: 骨科, en: Orthopaedics, order: 0, scope: true}, …]`. v3 → id map: 骨科→`DOM_ortho`, 有限元→`DOM_fe`, 代理模型→`DOM_surrogate`, 优化→`DOM_opt`, 通用写作句式→`DOM_writing` (`scope: false`: not a counting scope, its entries are generic phrases). |
| `concepts.yaml` | `concepts: [{id, label, zh, type, mode: forms\|literal, forms: [...], gen_forms: [...], case_forms: [...], rx: null\|str, probe: str, readable: str, domains: [DOM ids], place: [str], shared_with: [ids], origin: str, merged_into: null, source: legacy-v3}]` — one per v3 `item` (1,605). `mode`: v3 `词形`→`forms`, `字面`→`literal`. |
| `patterns.yaml` | Matcher data (see §3): `bad_next`, `bad_prev`, `short_caps_max_letters: 3`, `stem_caps_rule: true`, `token_regex`, `version: v2`. |
| `term_tree.yaml` | The 分结构术语库 tree from v3 `entry`: `domains: [{id: DOM_x, sections: [{section: title\|keywords\|abstract\|introduction\|methods\|results\|discussion, subs: [{name, order, terms: [{term, zh, type, concept_id, note, example, example_doc}]}]}]}]` (2,752 entries). |
| `categories.yaml` | Content framework: `categories: [{id, domain: DOM_x, name_zh, name_en, desc_zh, desc_en, order}]` (43). |
| `concept_categories.yaml` | `rows: [{concept_id, domain, primary, secondary: null\|id, reason, source: legacy-v3}]` (1,513; the accept-gate output of the later `classify` stage, imported as confirmed). |
| `subdomains.yaml` | `subdomains: [{id, kind: within\|cross, domain: DOM_x\|null, name_zh, name_en, desc_zh, desc_en, order, anchors: [C_ids]  (within), groups: [[C_ids], …] (cross), notes}]` (31; v3 `kind` 领域内→within, 交叉→cross; cross groups come from the v2 spec: anchors of a cross sub-domain are stored in v3 as one list — recover the groups from `H:\UCL_theses\2610 opt\术语阅览系统_v2_2026-10-19\_work\subdomain_spec.json` if present, else split by domain of the anchor concepts and record `groups_recovered: true`). |
| `doc_domains.yaml` | `docs: {P123: [DOM_ortho, DOM_fe], …}` from v3 `paper.domains` (the user's folder-based assignment). |
| `i18n.yaml` | `strings: [{zh, en, grp}]` from v3 `i18n` (1,963; groups framework/description/note/ui/predicate/qualifier). |
| `confirmations.json` | `{name: {sha, confirmed_at, from_draft_sha}}` for every file above; the legacy export confirms them (they are the user's own curated system). |

`atlas legacy export-curated <terms_v3.db> <project> [--force]` (tools/legacy.py) writes all of these and prints a report (counts, items whose domain is unknown, cross sub-domains whose groups were recovered). Refuses to overwrite an existing `curated/` without `--force`. `atlas curate confirm <name>` and `atlas curate status` (pipeline/gates.py) implement the sha check used by gated stages (§7 of the architecture); in this increment the only gated stages are `match` (needs `concepts`, `patterns`) and `assign` (needs `subdomains`, `domains`, `doc_domains`).

`atlas legacy import-highlights <highlights.sqlite> <sentences.db> <project>`: v3 user highlights (`hl(kind='sent', sid, start, end, quote, color, note)`) become notes targeting `S:<new sid>` (title "Highlight", body = note, tags [`highlight`, `<color>`]); the v3 integer `sid` resolves through `sentences.db` (paper, page, text) to the new sid by `(doc_id, norm(text))`; unmatched rows are reported. Statement highlights (`kind='stmt'`) are skipped with a count (overviews come in increment 5).

---

## 2. Builds (`<project>/builds/`)

`store/build.py` (`BuildStore`) + `store/sql/build.sql`. `atlas build new [--note …]` creates `builds/b<YYYY-MM-DD>_<seq3>/` (`seq` = 1 + number of existing folders of that day; refuses to overwrite) with `build.sqlite` (status `open`), `manifest.json` (`{build_id, created, status, project, evidence_sha_at_pin, curated: {name: sha}, docs: n, sentences: n, stages: {}}`), `curated/` = snapshot copy of the project's curated files, and pins the evidence: `build_docs(doc_id, ext_id)` = every document's current extraction; `build_sentences(sid PK, doc_id, unit_id, page_no, section, is_body, tags)` copied from `extraction_sentences` of those extractions (no text); `build_units(unit_id PK, doc_id, kind, ordinal, heading, page_from, page_to, unit_kind, confidence)`; `build_doc_meta(doc_id PK, title, year, authors, venue, doc_type, domains_json)` (domains from `doc_domains.yaml`). Scheme tables are loaded from the build's own `curated/` snapshot: `concepts(id, label, zh, type, mode, domains_json, place_json, origin)`, `concept_forms(concept_id, form, kind: form\|gen\|case\|literal, rx)`, `patterns(key, json)`, `domains(id, zh, en, ord, scope)`, `categories(id, domain, zh, en, desc_zh, desc_en, ord)`, `concept_categories(concept_id, domain, primary_cat, secondary_cat, reason)`, `subdomains(id, kind, domain, zh, en, desc_zh, desc_en, ord)`, `anchors(sub_id, group_no, concept_id)`, `term_tree(domain, section, sub_name, sub_ord, term, zh, type, concept_id, note, ord)`, `i18n(zh PK, en, grp)`.

Stage outputs (all with real foreign keys to `build_sentences`/`concepts`):

- `occurrences(sid, concept_id, form, cstart, cend, nested_in, PRIMARY KEY(sid, concept_id, cstart))` — `cstart/cend` index the **stored sentence text** (`sentences.text`), `nested_in` = the concept id of the longer match that contains this one (NULL when standalone). Covering indexes `(concept_id, sid)` and `(sid, concept_id)`.
- `topic_hits(unit_id, concept_id, field: title\|keywords\|abstract, n, n_nested)` — matches inside `topic_fields(unit)` texts (not sentence-based; the paper doctype's topic fields are the manifest/meta title, keywords line and abstract).
- `unit_subdomains(unit_id, sub_id, tier: core\|related, hits_json, n_hits)`.
- `counts(concept_id, doc_id, section, n, n_nested)` — per concept × document × section (`section` ∈ SECTIONS plus `body` = sum over BODY_SECTIONS, plus `title`/`keywords`/`abstract` from `topic_hits` of the document unit when the doctype has no such sentence section; `references` counted but never part of `body`).
- `importance(concept_id, scope, papers, any_papers, coverage, topic_papers, topic_rate, abstract_rate, occ, median_per_paper, refs_papers, main_section, lift, year_cov_json, trend, trend_flag, tier)` — one row per concept × scope (`all` + each domain with `scope: true`).
- `framework_stats(kind: subdomain\|category, id, scope, papers, core, base, years_json, overlaps_json, concepts_json)`.
- `stage_items(stage, item_key, fingerprint, status, started, finished, detail_json)`; `check_results(target_kind, target_id, seq, check, severity, detail)`.

`atlas run match [--build <id>] [--doc-ids …] [--workers N]`, `atlas run assign [--build <id>]`, `atlas run finish [--build <id>]` operate on the open build (default: the newest open build; error when none). `finish` runs the build-level checks (every `occurrences.sid` exists in `build_sentences`; every anchor resolves to a concept; counts consistent with occurrences on 20 sampled concepts), writes `report.md` (counts per stage, tier histogram, top concepts, warnings), then freezes: `wal_checkpoint(TRUNCATE)` → `VACUUM` → `journal_mode=DELETE` → read-only file attribute → sha256 into `manifest.json` (`status: finished`). `atlas build promote <id>` writes `builds/current.txt` (temp file + `os.replace`); `relink` of user refs is increment 3 (write a TODO, keep the function signature `relink(project, old_build, new_build) -> dict` returning `{}`). `atlas build list`, `atlas build status [<id>]`. `atlas build all [--note …]` = new → match → assign → finish → promote in one go (what the verifier and the user will run).

Runner (`pipeline/runner.py`): `STAGES = ["ingest", "match", "assign", "finish"]`; a stage object has `name, version, llm, requires, gate` and `run(project, build, **opts) -> Report`; `ingest` ignores `build`. `run_stage` resolves the build, checks `requires` (stage_items status) and the gate (`curate status` sha check: stop with the message "`<name>` changed after confirmation — run `atlas curate confirm <name>`").

The service opens the promoted build read-only (`mode=ro&immutable=1` once finished); each response carries `X-Atlas-Build`; `GET info` includes `build: {id, created, docs, sentences, concepts}`; when no build is promoted the concept routes answer 409 `{error: "no build promoted"}` and the UI shows a hint (the document/reader/notes routes keep working without a build, exactly as in increment 1).

---

## 3. Matcher (`core/matcher.py`, pure)

Port of v3 `matcher.py` + `rules_v2.py` semantics; the data (forms, bad-neighbour lists, thresholds) comes from `concepts.yaml`/`patterns.yaml`, never hard-coded.

- Tokeniser: `TOKEN = [A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*` over the sentence with `-` replaced by a space (`6Al`, `4V`, `3D` are tokens; bare numbers are not). Keys: lower-cased tokens joined by one space; `norm_key(label)` NFKC → `’`→`'` → `'s\b`→`s` → tokens.
- Forms mode: `forms + gen_forms` are case-insensitive keys; `case_forms` are case-sensitive exact tokens (abbreviations); a case form that is all-caps with ≤ 3 letters (after stripping a trailing `s`) does **not** match inside a run of all-caps words (`in_caps_run`: both neighbouring alphabetic tokens all-caps with ≥ 2 letters). Single-token keys obey `bad_next` / `bad_prev` (`prior` + to/work/…, `stem(s)` + from/cell(s), `gap` after research/knowledge/this/the) and the stem-caps rule (an all-caps token longer than 2 letters never matches `stem`/`stems`). Longest match wins per start token; overlapping matches of different concepts are both kept; a match fully inside a longer match of another concept is `nested_in` that concept (v2 "body_nested").
- Literal mode: `rx` applied to `lit_norm(sentence)` (NFKC lower, quotes removed, dashes/slashes → space, `:<>=±` spaced, other punctuation → space, collapsed) with the `probe` substring as a cheap pre-check; `cstart/cend` are mapped back to the stored sentence text through an offset map (test it).
- API: `Matcher.from_curated(concepts, patterns)`; `matcher.sentence(text) -> list[Occurrence(concept_id, form, cstart, cend, nested_in)]`; `matcher.text(text)` (same, for topic fields); deterministic order. Unit tests: every rule above with positive/negative cases; a golden test on 12 real sentences from the femstem evidence (store them in the test as strings) with expected occurrences derived by reading v3's behaviour.

`core/stats.py` (pure): the v2 metric functions over plain dicts — `coverage = papers/n_scope`, `topic_papers` = papers with a standalone hit in title or keywords (literal concepts: any hit), `abstract_rate`, `occ`, `median_per_paper`, `refs_papers`, `main_section` (argmax over 引言/方法/结果/讨论与结论 paper counts), `lift = ((k+0.5)/(n+1)) / ((ko+0.5)/(no+1))` vs the complement, `year_cov` by bins, `trend = ((late_k+0.5)/(late_n+1)) / ((early_k+0.5)/(early_n+1))` with flags 上升 (≥2, late_k ≥ 10) / 下降 (≤0.5, early_k ≥ 10), tiers `A` (coverage ≥ 0.10 and topic_rate ≥ 0.10) / `B` (coverage only) / `C` (topic only) / `D` / `E` (< 5 papers). Parameters in `defaults.yaml` under `stats:` (`cov_hi: 0.10`, `topic_hi: 0.10`, `min_papers: 5`, year bins, trend windows). Sub-domain membership (v2 `assign_subdomains.py`): a document is in a within-sub-domain of its domain when any anchor has a topic hit (title/keywords/abstract); `core` when an anchor is in the title or keywords or anchors are mentioned ≥ 2 times in the abstract, else `related`; a cross sub-domain needs every group to hit; core needs every group strong. For theses the unit is the chapter as well as the document (topic fields per unit).

Fidelity target (verifier): for 30 concepts (10 per tier A/B/C) the `all`-scope `papers` count must be within ±5 % of v3 `importance.papers` (`scope='全库'`), and the tier must agree for ≥ 27 of 30; deviations are listed with the reason (new sentence split, dehyphenation, section labels).

---

## 4. Thesis doctype (`doctypes/thesis.py`, version `thesis1`)

Pure, like `paper`. Rules (architecture §8):

- Front matter masks (`reason: front_matter`): title page, declaration/copyright, acknowledgements block in the first 10 % of pages, table of contents, list of figures/tables/abbreviations, until the first chapter start; the **abstract is kept** (section `abstract`).
- TOC parsing: lines like `Chapter 3 … 57`, `3.2 Methods … 61`, `CHAPTER THREE`, dotted leaders; calibrate the page offset (printed page → physical page) from ≥ 3 TOC entries whose heading text is found on a physical page; fall back to scanning for `^(Chapter|CHAPTER)\s+(\d+|[IVX]+|One|Two…)` headings, then to numbered top-level headings `^\d+\s+[A-Z]`.
- Units: one `document` unit + one `chapter` unit per detected chapter (`unit_id = doc_id + "/" + slug(heading)[:24]`, `~k` on duplicates; `ordinal`, `page_from/page_to`, `unit_kind` by heading keywords: intro | lit_review | methods | results | discussion | conclusion | other; `confidence` 0–1 with `reasons` such as `toc_matched`, `chapter_regex`, `no_toc`, `page_offset_unresolved`). Appendices and bibliography after the last chapter are units of kind `chapter` with `unit_kind: other` and sections `appendix`/`references`.
- Sections per chapter: the paper rules of `segment.detect_heading_ex` applied inside each chapter (chapter heading line itself → `other`), references per chapter when a `References/Bibliography` heading occurs inside the chapter, else at the end.
- `topic_fields`: document = manifest title + front-matter abstract (the text between an `Abstract` heading and the next front-matter heading, ≤ 600 words); chapter = heading + first 300 words of the chapter.
- Thresholds: `max_drop_ratio 0.30`, `min_chapter_confidence 0.7`. `ingest` warns per document when confidence < 0.7 and writes `work/drafts/chapters/<doc_id>.yaml` (units with pages and reasons) so the user can correct them later via `curated/units_override.yaml` (the override mechanism itself exists in `ingest` already: `_override_sha`; reading the override file into units is part of this increment: `units_override.yaml: {doc_id: {chapters: [{heading, page_from, page_to, unit_kind}]}}`).
- Fixture: `atlas fixture make --kind thesis <dir>` writes 3 synthetic theses (TOC with printed page numbers offset from physical pages, 4–6 chapters incl. a literature review and per-chapter references, running headers) and `tests/test_fixture_thesis.py` asserts units, sections, masks and `topic_fields`.

---

## 5. UCL subset (`tools/ucl_subset.py`, project `H:\projects\atlas-projects\ucl`)

Inputs: `H:\Weikang\01_Literature\UCL_theses\UCL_theses_inventory.csv` (1,030 rows: eprint_id, title, discipline, year, pages, has_text_layer, relpath) and `UCL_theses_by_proximity.csv` (1,401 rows: tier 1–3 proximity to hip/ortho/AI, rubric scores, relpath); PDFs under `H:\Weikang\01_Literature\UCL_theses\pdf\<bucket>\…`.

Rule (deterministic, documented in the manifest's `reason` column): (1) every proximity tier-1 and tier-2 thesis with a text layer and ≤ 450 pages; (2) fill to 120 with discipline `工程 / 计算机 / 机器学习`, year ≥ 2010, text layer, ≤ 450 pages, highest rubric total first; (3) add 10 from `物理 / 天文 / 数学 / 统计` and 10 from `化学 / 材料科学` (year ≥ 2010, text layer) so the thesis doctype is tested on other layouts. `doc_id = T<eprint_id>`, `doc_type = thesis`, `venue = discipline`, `file` absolute. Writes `manifest.csv`, `project.yaml` (`doc_type: thesis`, `pdf_dir` = the UCL pdf folder), `subset_report.md` (counts per rule, disciplines, years, pages). The verifier registers and ingests it and reports chapter-detection confidence statistics (share of theses with confidence ≥ 0.7, median chapters, documents with no chapters).

---

## 6. Service, routes, UI

Service (`service/api.py`): gains a `build()` accessor (BuildStore of the promoted build, pooled like the evidence store; reloads when `current.txt` changes), and the methods behind the routes below. Routes (`http/routes.py`, all under `/api/v1/`, JSON, bilingual labels from `/i18n` + the build's `i18n` table):

| route | returns |
|---|---|
| `GET concepts?q=&domain=&tier=&type=&sub=&cat=&scope=all\|DOM_x&sort=papers\|coverage\|topic_rate\|label\|tier&dir=&limit=&offset=` | `{rows: [{id, label, zh, type, mode, papers, coverage, topic_rate, tier, main_section, notes}], total, scope}` |
| `GET concepts/{id}?scope=` | concept page: curated fields, `importance` (all scopes), `sections` [{section, papers, occ}], `domains_use` [{domain, papers, total}], `years` [{year, n}] + `year_totals`, `categories`, `subdomains` (ranked by score as v3: papers, coverage, coverage_rest, lift), `anchor_of`, `term_tree_places`, `examples` (6 body sentences: highest standalone count docs first, deterministic), `notes` count |
| `GET concepts/{id}/sentences?section=&domain=&sub=&doc=&tag=&standalone=1&sort=doc\|year\|section&limit=&offset=` | every sentence with an occurrence: `{sid, doc_id, title, year, page, section, text, spans: [[cstart,cend,form]], tags}` (cursor-less offset paging, total) |
| `GET concepts/{id}/docs?scope=&sort=&limit=&offset=` | documents using the concept with per-section counts (v3 `api_item_papers`) |
| `GET framework/terms` | the term tree with per-node counts (`n` terms) |
| `GET framework/categories?domain=` | domain → categories → concepts (with `papers`, tier) |
| `GET framework/subdomains` and `GET subdomains/{id}` | sub-domain trees and page: concepts ranked, documents core/related (with hits), overlaps, years |
| `GET docs/{id}` (extended) | adds `domains`, `subdomains` [{sub_id, tier}], `top_concepts` [{id, label, n}] and, for theses, `units` with `unit_kind`/confidence |
| `GET docs/{id}/pages/{n}` (extended) | each sentence gets `concepts: [{id, cstart, cend}]` when `?c=` is given (occurrences of those concepts), so the reader can mark them |
| `GET resolve?ref=C:…`, `SD:…`, `U:…` | resolves through the build |

SPA (`http/static/`, same vanilla ES-module style; new `concepts.js`, `framework.js`; extend `views.js`, `reader.js`, `i18n.json`): header gains tabs 概念 / 框架 / 文献 / 笔记 / 摘录册; **concept list** = table with sortable headers (click = sort, second click = reverse; current column marked; sort state in the URL), filter bar (search, domain, tier, type, scope), paging, "综述" column placeholder (increment 5); **concept page** = header (label, zh, type, forms; tier badge per scope), importance table (scopes as columns), section bars, domain use, year sparkline (plain SVG), categories, sub-domains, anchor-of, term-tree places, example sentences (each with doc link and page link opening the reader with `?c=` highlighting), "全部原句" button → sentences view with the filters of the route, **prev/next** through the list the user came from (list order kept in `sessionStorage` like v3) ; **framework views** = three trees (collapsible; counts; links), sub-domain page; **document page/reader** = concept chips, occurrence marks in the text view when `?c=` is present; **notes** can target `C:` and `SD:` refs (resolve works, editor accepts them). Bilingual: every chrome string in `i18n.json`; data labels (domain/category/sub-domain names, descriptions) from the build's `i18n`.

Tests: `tests/test_http.py` extended against a fixture project with a finished build (built in the test from the fixture corpus + a tiny curated set written by the test, through the real commands); every new route exercised; a layering test still green.

---

## 7. Work packages and ownership

| WP | agent | owns (creates/edits) | must not touch |
|---|---|---|---|
| **A derived core** | `derive` | `core/matcher.py`, `core/stats.py`, `store/build.py`, `store/sql/build.sql`, `pipeline/runner.py` (STAGES, build resolution, gate check), `pipeline/gates.py`, `pipeline/stages/{match,assign,finish}.py`, `tools/legacy.py`, `cli.py` (verbs `legacy`, `curate`, `build`, `run match/assign/finish`), `defaults.yaml` (`stats:`), tests `tests/unit/test_matcher.py`, `test_stats.py`, `test_store_build.py`, `tests/test_build_pipeline.py` (fixture corpus → curated set written by the test → build all → asserts) | `service/`, `http/`, `doctypes/` |
| **B thesis + UCL** | `thesis` | `doctypes/thesis.py`, `tools/fixture.py` (thesis kind), `tools/ucl_subset.py`, `cli.py` only the `fixture make --kind` flag and `ucl subset` verb (small, disjoint edits), `pipeline/stages/ingest.py` only for `units_override.yaml` reading + chapter drafts, tests `tests/unit/test_thesis_doctype.py`, `tests/test_fixture_thesis.py`, `tests/unit/test_ucl_subset.py` (rule on a synthetic inventory) | everything else |
| **C service + UI** | `ui` | `service/api.py` (build accessor + new methods), `http/routes.py`, `http/server.py` (header `X-Atlas-Build`), `http/static/*`, `tests/test_http.py` — codes against the **build.sqlite schema of §2** (creates test builds by inserting rows with sqlite3 directly in a test helper until A's `BuildStore` exists; the integrator switches the helper to `BuildStore`) | `core/`, `store/`, `pipeline/` |

Integration (agent `integrate`): merge the three worktrees, resolve `cli.py` conflicts, switch C's test helper to `BuildStore`, make `tests` green, run `atlas legacy export-curated` + `atlas build all` on **the femstem project** (`H:\projects\atlas-projects\femstem`; evidence already ingested; the v3 db path above), serve on port 8771 (8770 may be in use) and smoke-test every route with curl. Then three reviewers (fidelity vs v3 and statistics; architecture/layering/ids/robustness; UI/i18n/UX) → fix → verifier (fidelity targets of §3, UCL subset registration + ingest + chapter stats, browser-level checks of the concept list/page/trees).

Acceptance: all tests green; `atlas build all` on femstem finishes without failures and promotes; fidelity targets met; the UI shows the concept list (sortable), concept pages with prev/next, three trees, sub-domain pages, bilingual; UCL subset (~120 theses) ingested with the thesis doctype, ≥ 70 % of theses at chapter confidence ≥ 0.7, every thesis has ≥ 1 chapter unit or a warning; docs updated (`docs/INCREMENT2.md` status section + README commands).

---

## Status

Increment 2 is merged (HEAD 65164ad: packages A/B/C integrated, 521 tests green, femstem built and promoted).

### Performance (2026-10-23)

Problem: on the femstem project (2,545 papers; evidence.sqlite 1.5 GB holding three extraction generations;
build 002 with 1.23 M pinned sentences and 2.31 M occurrences; H: is a hard disk) the first request after a server
start took tens of seconds: `GET /info` 61.9 s (the SPA calls it on every load), `concepts/C_micromotion` 15.5 s,
`concepts/C_micromotion/sentences` 23.4 s, `search?phrase=stress+shielding` 39.0 s, `search?phrase=finite+element`
50.9 s — warm they were 0.1–0.6 s. The causes, verified with `EXPLAIN QUERY PLAN` and timing: `stats()` scanned
`extraction_sentences` twice and `pages` once through joins; `phrase_search` computed `snippet()` for every FTS
match, joined every match to sentences / membership / meta and sorted them all before `LIMIT`, twice (count + rows);
`concept_sentences` and `examples()` joined every occurrence of a concept through `build_sentences` (random reads
over 1.2 M rows) and the attached evidence before `ORDER BY … LIMIT`, twice; and every first touch paid random
reads into a 1.5 GB file of which two thirds were superseded generations.

What changed (in order of preference: query shape first, then less data, then the OS cache):

1. **Query shape.**
   - `EvidenceStore.stats()` sums `extractions.n_pages` / `n_sentences` and `stats_json.body_sentences` (written
     by ingest since increment 1; an extraction without it is counted from its rows) over the current extractions —
     no scan. `AtlasService` caches the result per evidence file state (size + mtime of the main file and of a
     non-empty WAL) and the build counts / `year_totals` per build id.
   - `phrase_search` / `page_search`: candidate set first — every FTS match in rowid order (the FTS index alone:
     4 ms for the 9,818 rowids of "finite element", 164 ms for the 623,749 of "the"; `ORDER BY rank` was measured
     at 0.7–5 s because bm25 needs a docsize lookup per match), or, inside one document, the FTS matches
     intersected with the document's own sentence rowids; then at most `SEARCH_CAP = 50_000` candidates are joined
     (≈19 ms per 1,000 rows), counted, sorted and paged in Python — beyond the cap an every-k-th subset over the
     whole corpus is joined (review fix: the first version joined the first 5,000 rowids only, which hid 55 % of
     the documents containing "finite element"), `total` and `documents` are counted over every match, and the
     response carries `total_capped: true` + `listed` (the SPA explains that a sample is listed); `snippet()` runs
     for the returned page rows only (`WHERE … MATCH ? AND rowid IN (…)`).
   - Build schema 2: `occurrences` carries `doc_id, seq, page_no, section, is_body, tags` (copied from
     `build_sentences` by `BuildStore.add_occurrences` itself, so the match stage and every caller keep the six-tuple
     shape) and the covering index `occ_concept_doc(concept_id, doc_id, seq, section, is_body, nested_in, page_no,
     tags, sid)` replaces `occ_concept`. `concept_sentences` filters, counts (`GROUP BY doc_id, seq`), sorts and
     pages on that index alone and reads `build_sentences` (unit id), `build_doc_meta` and the evidence text for
     the page rows only; `examples()` goes `counts` → index → text; `page_concepts`, `rebuild_counts` and
     `recount_body` no longer join `build_sentences`. Stage version `match3`; `BUILD_SCHEMA_VERSION = 2`; a build
     file of schema 1 is refused for writing and the service answers 409 "promoted build … was written by an older
     schema" for it — `atlas build upgrade <id>` (review fix) brings such a file to schema 2 in place in seconds
     (the six `ALTER TABLE … ADD COLUMN`, the `UPDATE … FROM build_sentences`, the covering index, `user_version
     = 2`, re-freeze with a new sha256 in manifest.json), the `/info` response carries `build_stale: {id, message}`
     and the SPA header / concept pages name the build and the command instead of "no build"; prewarm skips a
     build the service will not serve. Migrations are now named per schema (`sql/migrations/user_2.sql`).
2. **Less data: `atlas evidence gc [--vacuum] [--dry-run] [--rebuild-fts]`** (`tools/gc.py`) deletes extractions
   that are not current, not pinned by any build folder's `build_docs` and not needed by a note / collection
   (`S:<sid>` refs whose sentence belongs to no kept extraction keep the newest extraction holding it): their
   pages / line_masks / units / extraction_sentences, then sentences no extraction references (FTS rows via the
   existing triggers), then unreferenced page_text, then the extractions row — one transaction per extraction, so
   it can be interrupted and rerun; it refuses while a build is open. `--vacuum` then VACUUMs and rebuilds the FTS
   indexes (VACUUM may renumber the TEXT-keyed rowids the external-content FTS tables use).
3. **OS cache: `atlas serve --prewarm` (default on; `--no-prewarm` to skip)** reads evidence.sqlite (+ WAL) and the
   promoted build.sqlite sequentially in 8 MB chunks on a background thread at startup and logs
   `prewarm done in N s`: one sequential pass over ~1 GB costs seconds on a hard disk; the same pages fetched on
   demand by B-tree and FTS seeks cost minutes.

Tests added: capped totals and snippet scope (`tests/unit/test_store_evidence.py`, timing-free: the rowids handed to
the snippet helper are counted and the search SQL is checked for the `rowid IN` shape), the stats aggregates, the
gc rules on the fixture corpus (`tests/test_gc.py`: three generations, a pinning build, a note-protected sentence,
VACUUM + FTS rebuild, idempotence, CLI), the stats cache invalidation and the old-schema refusal
(`tests/unit/test_service_cache.py`).

femstem after this work: see the measurements below (filled in by the run of 2026-10-23).

MEASUREMENTS_PLACEHOLDER
