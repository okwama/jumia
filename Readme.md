# PRD / Architecture: Jumia Feed Sync

**Owner:** Benjamin · **Status:** Draft v0.3 · **Date:** 23 Jul 2026

---

## 1. Problem

GizmoJunction products live in a Google Merchant XML feed. Jumia ingests via a 68-column XLSX template with strict, unpublished-until-rejection rules. Manual transcription is slow, error-prone, and rejections arrive hours after upload with terse reasons. We need a configurable system that maps feed → template, enforces Jumia's rules *before* upload, and lets a human eyeball images and category matches.

## 2. Goals

- Zero-touch mapping for products whose brand + category are already resolved
- Block non-conforming rows locally rather than discovering them via Jumia rejection
- Visual review of every image before it ships
- Category/brand/rule config editable without code changes
- Margin visibility per SKU using the commission & shipping sheet

**Non-goals (v1):** direct Jumia API push, AR/FR translation generation, automated attribute inference for category-specific fields.

## 3. Users

Single operator (you). Dashboard is local-first, single-tenant, no auth in v1.

---

## 4. Architecture

```
┌─────────────┐
│ Google Feed │ (XML, https://gizmojunction.com/api/google-feed)
└──────┬──────┘
       │ httpx + lxml
┌──────▼───────────────────────────────────────────┐
│ INGEST         normalize → staging table         │
└──────┬───────────────────────────────────────────┘
       │
┌──────▼───────────────────────────────────────────┐
│ RESOLVE        brand map · category map · fuzzy  │
│                unresolved → review queue         │
└──────┬───────────────────────────────────────────┘
       │
┌──────▼───────────────────────────────────────────┐
│ VALIDATE       rule engine (YAML-driven)         │
│                image probe · pydantic model      │
└──────┬───────────────────────────────────────────┘
       │
┌──────▼─────────┐         ┌────────────────────┐
│ DASHBOARD      │◄────────┤ REVIEW QUEUE       │
│ (FastAPI+HTMX) │         │ image grid, edits  │
└──────┬─────────┘         └────────────────────┘
       │ approve
┌──────▼───────────────────────────────────────────┐
│ EXPORT         openpyxl → Upload_Template.xlsx   │
│                + rejects.csv + margin report     │
└──────────────────────────────────────────────────┘
```

### Why a dashboard, not just a CLI

You need image preview and category confirmation — both are visual judgment calls. CLI stays as the automation entry point; dashboard is the review layer over the same pipeline.

### Run execution model

INGEST→RESOLVE→VALIDATE takes long enough (feed fetch, image probing, fuzzy matching against 30k categories) that it can't run inline in a request handler. The dashboard's Run screen kicks it off as a FastAPI `BackgroundTasks` job writing progress into a `runs` row; the page polls via HTMX (`hx-trigger="every 1s"`) until the run completes. No separate queue/worker process needed at this scale — a second concurrent run is simply disallowed while one is in flight.

---

## 5. Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Excel + feed + validation ecosystem |
| Feed fetch | `httpx` (async) | Concurrent image probing |
| XML parse | `lxml` | Namespace-aware, fast |
| Validation | `pydantic v2` | Typed models, structured per-field errors |
| Rules | `PyYAML` + custom engine + `simpleeval` | Non-developer-editable rule config; `simpleeval` sandboxes the `expr` checks instead of raw `eval()` |
| Fuzzy match | `rapidfuzz` | Category path matching, C-speed |
| Excel write | `openpyxl` | Template has no validation sheets — simple append |
| Fee lookup | `pandas` | 30k-row commission sheet, one-time load |
| Image checks | `httpx` HEAD + `Pillow` | Reachability + dimensions |
| Store | `SQLite` | Staging, resolution cache, run history |
| Dashboard | `FastAPI` + `Jinja2` + `HTMX` + Tailwind | No SPA build step; server-rendered, fast to ship |
| Config | YAML on disk, editable in-dashboard | Version-controllable |

**Rejected:** Go (weaker xlsx tooling), Next.js dashboard (build overhead for a local tool), Postgres (SQLite is sufficient at this scale).

---

## 6. Data Model

```sql
products        -- current staging from feed, upserted on each ingest
  sku PK, title, description, image_link, price_kes,
  brand_raw, product_type_raw, availability, condition,
  fetched_at, feed_hash

resolutions     -- learned lookups, permanent
  kind ('brand'|'category'), raw_value,
  jumia_id, jumia_label, confidence, confirmed_by_human, updated_at
  UNIQUE(kind, raw_value)

resolutions_history  -- append-only, one row per change to `resolutions`
  id PK, kind, raw_value, jumia_id, jumia_label, confirmed_by_human, changed_at

validation_runs
  id PK, started_at, finished_at, status ('running'|'completed'|'failed'),
  error_message, feed_item_count, passed, failed, exported_path

run_products    -- immutable snapshot of each product as validated in this run
  run_id FK, sku, title, price_kes, brand_resolved, category_resolved,
  stage ('ingested'|'resolved'|'probed'|'validated'),
  status ('passed'|'warned'|'blocked'), feed_hash
  PRIMARY KEY (run_id, sku)

row_issues
  run_id FK, sku, field, severity ('block'|'warn'), rule_id, message

image_cache
  url PK, status_code, width, height, bytes, checked_at
```

`resolutions` is the core asset — every manual category confirmation is permanent institutional knowledge. Uniqueness is `(kind, raw_value)`, not `raw_value` alone — a brand string and a category string can coincide. Every write to `resolutions` also appends to `resolutions_history`, so a bad manual pick is recoverable and auditable, not silently overwritten.

`products` is mutable staging (today's feed state); `run_products` is the immutable record of what a given run actually validated, which is what the History screen's "diff vs previous run" and per-run `rejects.csv` are built from — reading `row_issues` against live `products` would be reading someone else's run once the next ingest overwrites the staging table.

`run_products.stage` makes a run resumable: if the process dies mid-run (most likely during image probing, the network-bound stage), the next invocation picks up rows still at an earlier stage instead of re-running the whole pipeline from `ingested`. `validation_runs.status`/`error_message` give the run itself a terminal failure state — see §15.

`image_cache.checked_at` has a config-driven max age (`image_cache.max_age_hours`, default 24h); reads older than that are treated as a miss and re-probed. Without this, a Cloudinary URL that later expires would stay cached as "pass" forever, which is the opposite of what §14's expiry mitigation claims.

---

## 7. Category Resolution

This is the hardest part and deserves its own treatment.

**The problem:** Feed says `Components & Accessories`. Jumia wants `1002708 - Computing / Computer Accessories / Printer Ink & Toner / Inkjet Printer Ink`. There are ~30k Jumia categories. No public API for the ID mapping.

**Approach — three tiers:**

1. **Exact cache hit.** `resolutions` table lookup on `raw_value`. Instant, no review.
2. **Fuzzy suggestion.** `rapidfuzz` scores the feed `product_type` + `title` tokens against the commission sheet's `PATH` column. Top 5 candidates surfaced in the dashboard with scores. Human picks; choice is cached permanently.
3. **Manual entry.** Nothing scores above threshold → operator pastes the `ID - Path` string from Seller Center. Cached.

**Critical constraint:** the commission sheet uses UUID `CATEGORY SID`s, which are *not* the numeric IDs the template needs. So the fee sheet gives you the path text and commission data, but the numeric ID must come from the template's existing rows or manual Seller Center lookup. Bootstrap harvests all `ID - Path` pairs from any filled template you feed it.

**Category-driven attributes.** Once category is known, the rule config declares which of the template's category-specific attribute columns are required for it (exact column letters TBD against the real template — see §13 Open Decision 5):

```yaml
categories:
  "1002708":                          # Inkjet Printer Ink
    label: "Computing / ... / Inkjet Printer Ink"
    required_attrs: [color, color_family, product_weight]
    optional_attrs: [package_content, model]
  "1002000":                          # example: Laptops
    required_attrs: [processor_type, system_memory, hdd_size, display_size]
```

Rows missing a required attribute for their category are blocked with a precise message.

---

## 8. Rule Engine

Rules live in YAML so you tune them as Jumia rejections teach you new constraints.

```yaml
rules:
  - id: sku_required
    field: SellerSKU
    severity: block
    check: {not_empty: true, max_length: 50}

  - id: sku_unique
    field: SellerSKU
    severity: block
    check: {unique_in_batch: true}

  - id: name_length
    field: Name
    severity: block
    check: {min_length: 20, max_length: 255}

  - id: name_no_promo
    field: Name
    severity: block
    check:
      not_matches: '(?i)\b(best|cheap|sale|free shipping|!!!)\b'
    message: "Jumia rejects promotional language in product names"

  - id: price_positive
    field: Price_KES
    severity: block
    check: {gt: 0}

  - id: sale_price_lower
    severity: block
    check: {expr: "Sale_Price_KES is null or Sale_Price_KES < Price_KES"}

  - id: brand_format
    field: Brand
    severity: block
    check: {matches: '^\d+ - .+$'}
    message: "Brand must be 'ID - Name' e.g. '1045133 - Generic'"

  - id: category_format
    field: PrimaryCategory
    severity: block
    check: {matches: '^\d+ - .+$'}

  - id: image_reachable
    field: MainImage
    severity: block
    check: {http_status: 200}

  - id: image_min_dims
    field: MainImage
    severity: block
    check: {min_width: 500, min_height: 500}

  - id: image_white_bg
    field: MainImage
    severity: warn
    check: {corner_luminance_gt: 240}
    message: "Jumia prefers white background on main image"

  - id: desc_not_title
    severity: warn
    check: {expr: "Description != Name"}
    message: "Description duplicates title — poor listing quality"

  - id: short_desc_html
    field: short_description
    severity: warn
    check: {allowed_tags: [ul, li, p, br, strong]}

  - id: stock_int
    field: Stock
    severity: block
    check: {integer: true, gte: 0}
```

**Severity semantics:** `block` → row excluded from export, lands in `rejects.csv`. `warn` → exported but flagged amber in dashboard.

---

## 9. Image Pipeline

Runs async, concurrency-capped, results cached in SQLite so re-runs are instant. Cache entries expire after `image_cache.max_age_hours` (default 24h) and are re-probed on the next run — see §6 data model note on `image_cache`.

Per image: HEAD for status → GET bytes if unknown → Pillow for dimensions, format, and corner-luminance sample (small corner patch average, not a single pixel — less noisy for the white-background heuristic) → cache.

**Dashboard image grid** is the primary review surface:

- Thumbnail wall, ~8 per row, lazy-loaded
- Badge overlay per tile: dimensions, format, pass/warn/block
- Click → full-size lightbox with the full row's field values beside it
- Filter chips: `all` · `blocked` · `warnings` · `unresolved category` · `missing image`
- Bulk actions: approve selected, exclude selected, re-probe

Cloudinary note — your URLs already support transforms (`w_800,h_800,c_pad,b_white`). The system can auto-append that transform to any image failing the dimension or background rule, turning a block into a pass without re-hosting. Worth making a config toggle: `cloudinary.auto_pad: true`.

---

## 10. Dashboard Screens

| Screen | Purpose |
|---|---|
| **Run** | Trigger fetch, live progress, summary counts (parsed / passed / warned / blocked) |
| **Review Grid** | Image wall with filters and inline field edits |
| **Unresolved** | Category & brand queue — fuzzy candidates with scores, one-click confirm |
| **Rules** | YAML editor with live re-validate against last run, no restart |
| **Mapping** | Feed field → template column editor |
| **Margin** | Per-SKU: price, commission %, DS fee, net, flagged negatives |
| **History** | Past runs, downloadable exports, diff vs previous run |

**Configurability is the design constraint.** Every threshold, regex, required-attribute set, and field mapping is YAML-backed and editable in-app. Editing a rule re-validates the cached last run immediately — no re-fetch, no restart. This is what makes the system survive Jumia changing their mind.

---

## 11. Export

`openpyxl` loads `Upload_Template.xlsx`, clears rows 2+, appends approved rows in the template's exact column order (full width, header-driven — see §13 Open Decision 5 on confirming the real column count/letters before hardcoding any range). Header row untouched. Output: `out/jumia_upload_YYYYMMDD_HHMM.xlsx`.

Companion artifacts: `rejects.csv` (sku, rule_id, field, message) and `margin_report.csv` are built from that run's `run_products` + `row_issues` rows (§6), not live `products` — so they stay accurate even after the next ingest overwrites staging. Plus `run_summary.json`.

Idempotency via `feed_hash` — unchanged products are skipped unless `--force`.

---

## 12. Milestones

| M | Scope | Output |
|---|---|---|
| **M0** | Feed parse + SQLite staging (incl. `run_products.stage`) + bootstrap harvester from filled template | CLI dumps normalized products, seeds `resolutions` |
| **M1** | Rule engine (config schema-validated, collect-all evaluation) + pydantic model + export writer + unit tests + golden-file export test | Working CLI end-to-end, no UI |
| **M2** | Image probe pipeline + cache + resumable stage tracking | Dimension/reachability enforcement, resumes after mid-run failure |
| **M3** | Dashboard: run + review grid + image wall + run failure state surfaced | Visual approval loop |
| **M4** | Unresolved queue with fuzzy candidates + `resolutions_history` audit trail | Category resolution UX |
| **M5** | Rules/mapping editors in-app + margin report | Full configurability |

M0–M2 is a usable system. M3+ is the review layer.

---

## 13. Open Decisions

1. **Brand strategy.** Every existing row uses `1045133 - Generic`. Is that deliberate policy, or do UGREEN/Epson/Brother have their own Jumia brand IDs you should be claiming? Own-brand listings rank better.
2. **Description quality.** In the feed sample, `g:description` is byte-identical to `g:title`. Jumia listings with a one-line description convert badly. Either fix the feed at source in GizmoJunction, or the system generates `short_description` bullets — which means an LLM step in the pipeline, worth scoping separately.
3. **Name length floor.** Your feed titles run ~60 chars; template examples run ~75. Confirm Jumia's actual minimum before setting `min_length: 20`.
4. **ParentSKU derivation.** Template rows group variants (`T6641`, `T6642` → parent `T664`). The feed has no parent concept. Rule-based prefix stripping, or manual?
5. **Template column range.** §7 and §11 referred to different column ranges for attributes vs. full export width in earlier drafts. Confirm the real `Upload_Template.xlsx` header (letters, count, which are category-conditional) before M1 hardcodes anything — the export writer should derive column order from the parsed header (per §14's own mitigation), not a literal range.

---

## 14. Risks

| Risk | Mitigation |
|---|---|
| Jumia changes template columns | Header parsed at runtime, not hardcoded; mismatch fails loudly |
| Category ID map incomplete | Unresolved queue blocks rather than guesses; every confirmation is permanent |
| Rejection reasons stay opaque | Log every Jumia rejection back into the rule config as a new rule |
| Cloudinary URL expiry | Image cache stores status with a max-age; entries older than `image_cache.max_age_hours` are re-probed rather than trusted indefinitely (§6, §9) |
| Rule `expr` field allows arbitrary code if implemented naively | Evaluated via `simpleeval` (sandboxed), not Python `eval()` — relevant even single-tenant, since rules are dashboard-editable (§5, §8) |
| SQLite locked under concurrent dashboard read + background run write | WAL mode (`PRAGMA journal_mode=WAL`) enabled at startup |
| Config/`resolutions` loss (disk corruption, bad edit) | Rule/mapping YAML lives in the git-tracked `config/` dir; `resolutions` table dumped to CSV on each run as a cheap backup of the core asset (§6) |

---

## 15. Architecture Principles & Reliability

Non-negotiable design rules that keep the system trustworthy as it grows past a weekend project — violating any of these should be treated as a bug, not a style choice.

1. **Runs are resumable, not all-or-nothing.** A run steps each SKU through `ingested → resolved → probed → validated` (`run_products.stage`, §6). If the process dies mid-run — most likely during image probing, the only network-bound, slow stage — the next invocation resumes rows from their last completed stage instead of redoing the whole pipeline. This is what makes a flaky Cloudinary response cost seconds instead of the whole run.
2. **Config is schema-validated before it's used.** Rule YAML and category-attribute YAML are parsed into a pydantic model on save (Rules screen) and on load (CLI), not just exercised lazily at validation time. A malformed rule fails loudly in the editor, not silently mid-run three weeks later.
3. **`resolutions` is append-only in spirit.** Every write also lands in `resolutions_history` (§6). "Permanent institutional knowledge" needs to survive a fat-fingered correction, not just a first-time entry.
4. **Rule evaluation never short-circuits.** Every rule runs against every row regardless of earlier failures; `row_issues` collects the complete list. The dashboard's whole value proposition is showing a SKU's full problem list in one pass — an early `return` on first failure silently breaks that.
5. **A run has an explicit terminal failure state.** The background task wraps the pipeline in try/except; an unhandled exception sets `validation_runs.status = 'failed'` with `error_message` populated, not an indefinitely-spinning progress bar (§4 Run execution model).
6. **The export path is protected by a golden-file test.** One fixture feed + fixture template, checked into `tests/fixtures/`, with the expected output XLSX diffed cell-by-cell in CI/pre-commit. Column-order drift is the single failure mode that causes mass Jumia rejection, so it's the one thing that gets an end-to-end test rather than relying on unit tests of the pieces (§12 M1).

### Scale assumptions (confirm before committing to SQLite/pandas choices)

Every tech choice in §5 (SQLite, `pandas` one-time load, `rapidfuzz` per unresolved item) assumes roughly: **low thousands of SKUs per feed**, **one run at a time**, **runs triggered a few times a week, not continuously**. If GizmoJunction's real catalog is an order of magnitude larger, or runs need to happen concurrently/on a tight schedule, revisit SQLite (§16) and consider caching fuzzy-match results per `raw_value` rather than rescoring identical unresolved strings every run. Confirm the real feed item count before M0 locks these assumptions in.

---

## 16. Do we need Postgres?

No — SQLite stays, and the additions in this revision (resumable-run state, `resolutions_history`, config validation) don't change that. Postgres earns its operational cost (a service to run, credentials to manage, a backup story beyond "copy a file") when you have concurrent writers across a network, multiple tenants, or need transactional guarantees SQLite's file-level locking can't give you. This system has none of those: single operator, single machine, local-first, one run in flight at a time (§4). The one real concurrency case — the dashboard reading while a background run writes — is handled by WAL mode (§14), which is exactly what WAL is for.

Revisit this only if the project's shape changes: multiple people using the dashboard, the pipeline running on a server other people query concurrently, or genuinely large data volumes. None of those are in scope per §3 (single-tenant, no auth) or the non-goals in §2. Reintroducing Postgres later is a schema-port, not a rewrite — nothing here uses SQLite-only features — so there's no lock-in cost to deferring it.

---

Item 1 in Open Decisions is worth settling before M0 — if you should be using real brand IDs, the bootstrap harvester's seeded data is wrong from the start and every product inherits it.