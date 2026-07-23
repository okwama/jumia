# PRD / Architecture: Jumia Feed Sync

**Owner:** Benjamin · **Status:** Draft v0.8 · **Date:** 23 Jul 2026

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

INGEST→RESOLVE→VALIDATE takes long enough (feed fetch, image probing, fuzzy matching against 1,913 categories and 175,460 brands — §7) that it can't run inline in a request handler. **Implemented M3, matches this as designed:** the dashboard's Run screen creates the `validation_runs` row synchronously (so a same-instant double-click sees it immediately, no race), then hands the actual work to a FastAPI `BackgroundTasks` job; the page polls `/run/status` via HTMX (`hx-trigger="every 2s"`) until the run completes. No separate queue/worker process needed at this scale — a second concurrent run is disallowed while one is in flight (checked against `validation_runs.status = 'running'`).

Launch it with `jumia-feed-sync serve`, or `start.ps1` (syncs deps, creates `.env` from the example on first run, opens the browser, then runs the server in the foreground).

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
  sku PK, title, description, image_link, price_kes, sale_price_kes,
  brand_raw, product_type_raw, availability, condition,
  fetched_at, feed_hash

id_label_catalog  -- known-valid (id, label) pairs harvested from filled
                     -- templates or the commission sheet. NOT keyed by feed
                     -- text -- this is the candidate universe fuzzy-match
                     -- scores against, not a resolution.
  kind ('brand'|'category'|'parent_sku'), jumia_id, jumia_label,
  source ('template'|'commission_sheet'|'manual'), first_seen_at
  UNIQUE(kind, jumia_id)

resolutions     -- learned lookups, permanent: raw feed text -> a confirmed
                   -- entry from id_label_catalog (or a manual one)
  kind ('brand'|'category'|'parent_sku'), raw_value,
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
  status ('passed'|'warned'|'blocked'),
  human_override ('approved'|'excluded'|NULL), feed_hash
  PRIMARY KEY (run_id, sku)

row_issues
  run_id FK, sku, field, severity ('block'|'warn'), rule_id, message

image_cache
  url PK, status_code, width, height, bytes, format, corner_luminance, checked_at

field_overrides  -- human edits to Name/Description/MainImage, survive re-ingest
  sku, field ('Name'|'Description'|'MainImage'|'Price_KES'), value, updated_at
  PRIMARY KEY (sku, field)
```

`field_overrides` exists because `products` is overwritten on every feed ingest (§4) -- editing `title` directly there would just get silently wiped the next time the feed syncs. `mapping.map_product` applies any override on top of the ingested value every time a product is mapped, so an edit persists across ingests until explicitly changed or cleared.

`run_products.human_override` (M3) is the Review Grid's approve/exclude action: a human can flip an automated decision -- exclude an otherwise passed/warned row (bad image content the rules can't see), or approve an otherwise blocked one (a false positive, or a rule you're choosing to override this once). `run_export` treats `excluded` as "never export this, regardless of status" and `approved` as "always export this, regardless of status" (§11).

`resolutions` is the core asset — every manual category confirmation is permanent institutional knowledge. Uniqueness is `(kind, raw_value)`, not `raw_value` alone — a brand string and a category string can coincide. Every write to `resolutions` also appends to `resolutions_history`, so a bad manual pick is recoverable and auditable, not silently overwritten.

`id_label_catalog` vs. `resolutions` — these solve different problems. A filled `Upload_Template.xlsx` only contains the *final* Jumia value (e.g. `1002708 - Computing/.../Inkjet Printer Ink`) — it has no record of what raw feed text produced it, so the bootstrap harvester (§7) cannot write directly into `resolutions`. It writes into `id_label_catalog` instead: "these IDs are known to be valid and in use." That catalog is what the RESOLVE stage's fuzzy tier scores candidate feed text against; a human confirming one of those candidates for a specific `raw_value` is what actually creates the `resolutions` row. `kind='parent_sku'` reuses the same two tables for ParentSKU lookups — real data shows ParentSKU isn't always derivable from the SKU string (§13 Open Decision 4), so it needs the same catalog-then-confirm flow as brand and category, not a regex.

`products` is mutable staging (today's feed state); `run_products` is the immutable record of what a given run actually validated, which is what the History screen's "diff vs previous run" and per-run `rejects.csv` are built from — reading `row_issues` against live `products` would be reading someone else's run once the next ingest overwrites the staging table.

`run_products.stage` makes a run resumable: if the process dies mid-run (most likely during image probing, the network-bound stage), the next invocation picks up rows still at an earlier stage instead of re-running the whole pipeline from `ingested`. `validation_runs.status`/`error_message` give the run itself a terminal failure state — see §15.

`image_cache.checked_at` has a config-driven max age (`image_cache.max_age_hours`, default 24h); reads older than that are treated as a miss and re-probed. Without this, a Cloudinary URL that later expires would stay cached as "pass" forever, which is the opposite of what §14's expiry mitigation claims.

---

## 7. Category Resolution

This is the hardest part and deserves its own treatment.

**The problem:** Feed says `Components & Accessories`. Jumia wants `1002708 - Computing / Computer Accessories / Printer Ink & Toner / Inkjet Printer Ink`. **Real count, confirmed 2026-07-23 against Jumia's own guidelines workbook: 1,913 categories** (the earlier "~30k" guess was wrong — corrected everywhere it appeared, §15 included). No public API for the ID mapping.

**Approach — three tiers, all implemented as of 2026-07-23 (pulled forward from M4 — see §10):**

1. **Exact cache hit.** `resolutions` table lookup on `raw_value`. Instant, no review. (`mapping.map_product`)
2. **Fuzzy suggestion.** `rapidfuzz` scores the raw feed value against `id_label_catalog` (§6). Top 5 candidates surfaced in the dashboard's Unresolved screen with scores, lazily per row. Human picks; choice is cached permanently in `resolutions`. (`resolve.suggest`, `/unresolved`)
3. **Manual entry.** Same screen, a free-text `jumia_id`/`jumia_label` pair, for when nothing scores well — confirmed necessary in practice, not just a theoretical fallback: the seller's own store name (`GizmoJunction`, used as `brand_raw` on generic items) has no real match in Jumia's brand catalog at all (§10). Cached the same way. (`resolve.confirm`)

**The UUID problem is resolved, not just worked around.** The original plan was the commission sheet's `PATH` column, whose `CATEGORY SID`s are UUIDs — not the numeric IDs the template needs. That's moot now: **Jumia's own seller guidelines workbook** (`Brands` and `Categories` sheets, harvested via `bootstrap-guidelines`, §12) is a direct, authoritative `ID - Label` list — 175,460 real brand codes, 1,913 real category codes, straight from Jumia, already in the correct numeric-ID format. This is now the primary source for `id_label_catalog`; the filled-template harvest (`bootstrap`) is a secondary source that only confirms categories/brands already in active use. The commission sheet, if you still get hold of it, now matters only for its fee/commission percentages (§13 Open Decision 6), not for category resolution at all.

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
    check: {expr: "Sale_Price_KES is None or Sale_Price_KES < Price_KES"}

  - id: brand_format
    field: Brand
    severity: block
    check: {not_empty: true, matches: '^\d+ - .+$'}
    message: "Brand must be 'ID - Name' e.g. '1045133 - Generic' -- also fails on an unresolved brand"

  - id: category_format
    field: PrimaryCategory
    severity: block
    check: {not_empty: true, matches: '^\d+ - .+$'}
    message: "PrimaryCategory must be 'ID - Path' -- also fails on an unresolved category"

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

**`expr` syntax is Python, not SQL** — evaluated via `simpleeval` (§5, §15) with row field names bound as variables. Use `is None` / `is not None`, not SQL's `is null`.

---

## 9. Image Pipeline

Runs async, concurrency-capped, results cached in SQLite so re-runs are instant. Cache entries expire after `image_cache.max_age_hours` (default 24h) and are re-probed on the next run — see §6 data model note on `image_cache`.

Per image: HEAD for status (skip the GET entirely if non-200 — don't pay for bytes you'll reject anyway) → GET bytes if HEAD is 200 → Pillow for dimensions and a corner-luminance sample (10×10px corner patch average, not a single pixel — less noisy for the white-background heuristic) → cache.

**Real-data finding (2026-07-23, M2):** running the probe against actual GizmoJunction/Cloudinary images surfaced two things worth knowing before trusting this heuristic blindly. First, it works as intended — one real image (`UG-55551B`, 466×524) genuinely fails `image_min_dims` (needs 500×500), exactly the kind of thing this system exists to catch before Jumia does. Second, two other real images returned corner luminance near 0 (near-black) despite plausibly having intended white/transparent backgrounds — likely PNG alpha compositing to black under `.convert("L")` rather than an actual dark background. This is exactly why `image_white_bg` is `warn`, not `block` (§8): the heuristic is approximate by design, and a false positive here degrades to a dashboard flag, not a silent export block.

**Dashboard image grid** is the primary review surface:

- Thumbnail wall, ~8 per row, lazy-loaded
- Badge overlay per tile: dimensions, format, pass/warn/block
- Click → full-size lightbox with the full row's field values beside it
- Filter chips: `all` · `blocked` · `warnings` · `unresolved category` · `missing image`
- Bulk actions: approve selected, exclude selected, re-probe

Cloudinary note — your URLs already support transforms (`w_800,h_800,c_pad,b_white`). The system can auto-append that transform to any image failing the dimension or background rule, turning a block into a pass without re-hosting. Worth making a config toggle: `cloudinary.auto_pad: true`.

---

## 10. Dashboard Screens

| Screen | Purpose | Status |
|---|---|---|
| **Run** | Trigger fetch, live progress, summary counts (parsed / passed / warned / blocked) | Done (M3) |
| **Review Grid** | Image wall with filters, bulk approve/exclude, per-tile edit | Done (M3 + this pass) -- filters are `all/blocked/warned/passed/unresolved_category/missing_image` |
| **Unresolved** | Category & brand queue — fuzzy candidates with scores, one-click confirm | Done (pulled forward from M4, 2026-07-23 — see below) |
| **Rules** | YAML editor with live re-validate against last run, no restart | M5 |
| **Mapping** | Feed field → template column editor | M5 |
| **Margin** | Per-SKU: price, commission %, DS fee, net, flagged negatives | M5 (also blocked on Open Decision 6 -- no commission sheet yet) |
| **History** | Past runs, downloadable exports, diff vs previous run | Not milestoned yet |

**M3 scope notes.** Two things called for in §9's original dashboard bullet list didn't make the M3 cut: inline field edits on a tile, and the **re-probe** bulk action. Re-probe turned out to matter more than expected: real verification (2026-07-23) hit a transient network failure on a HEAD request that got cached as `status_code=None` ("unreachable") for the full `image_cache.max_age_hours` (24h) -- a one-off blip now blocks that row for a day with no way to force a recheck except waiting out the TTL or manually clearing the cache row. Still not built (worth prioritizing whenever image pipeline work picks back up); field editing was, see below.

**Unresolved queue, pulled forward from M4 (2026-07-23).** Turned out to be blocking, not a nice-to-have: without it, resolving a brand or category required hand-writing SQL `INSERT`s into `resolutions`, which isn't something an actual operator can do. Grouped by raw feed value, not by product (58 real UGREEN products share one raw `brand_raw` — resolve it once, not 58 times). Fuzzy suggestions score the raw value against `id_label_catalog` (§7) via `rapidfuzz`, lazily per row (not precomputed for the whole list — see the performance note below). Confirming a suggestion, or typing a manual `jumia_id`/`jumia_label` pair, calls `resolve.confirm()`, which writes `resolutions` *and* appends to `resolutions_history` (§15 principle 3) — every change is what powers the Review Grid, resolves both DB tiers cleanly per §7.

**Real-data finding: not every raw value has a good match.** `GizmoJunction` — the seller's own store name, used as `brand_raw` for generic/unbranded items in the feed — scored its top fuzzy "matches" at 90% against unrelated brands (`Gizmo`, `Giz`, `Ct+`). There's no real Jumia brand called GizmoJunction; a human has to recognize this and type the actual answer (`1045133 - Generic`) manually rather than trust the suggestion. This is exactly why manual entry (tier 3, §7) has to stay a first-class path, not a fallback buried behind fuzzy suggestions.

**Performance finding: the naive approach doesn't scale, caching does (confirmed 2026-07-23).** A single fuzzy lookup against the real 175,460-row brand catalog took **3.5-4.9s** end-to-end the first time: ~1.7s was SQLite re-fetching all 175K rows (identical on every request, since the catalog barely changes), ~1-1.3s was the `WRatio` scoring itself, plus overhead. Caching the catalog in-process per `kind` (invalidated only by restarting the dashboard — acceptable, since a bootstrap harvest doesn't happen while someone's mid-resolution-session) cut it to **~2s** on a warm cache. Tried a cheaper scorer (`QRatio`) as the other lever: same speed win, but it measurably degraded category-match quality on real data (buried the correct "Computing / Computer Accessories" match under irrelevant categories for a "Components & Accessories" query) — not worth the trade. 2s per lookup is fine for occasional manual resolution; a smarter candidate-narrowing approach (e.g. bucket by first letter before scoring) would be the next lever if this becomes a bottleneck at higher usage.

**Field editing (this pass, not originally milestoned).** A per-tile "edit" action on the Review Grid lets you correct `Name`, `Description`, or `MainImage` without touching `products` — which is overwritten on every feed ingest, so an edit made there would be silently lost on the next fetch. Edits live in `field_overrides` (§6) and are applied by `mapping.map_product` on top of the ingested value, every time a product is mapped. Deliberately narrower than M5's full Mapping screen (that's feed-field → template-column configuration; this is per-SKU data correction) — different problem, smaller surface, built now because it was asked for directly.

**Configurability is the design constraint.** Every threshold, regex, required-attribute set, and field mapping is YAML-backed and editable in-app. Editing a rule re-validates the cached last run immediately — no re-fetch, no restart. This is what makes the system survive Jumia changing their mind.

---

## 11. Export

**Jumia's Seller Center upload flow is category-scoped (confirmed 2026-07-23, not in earlier drafts of this PRD): you select one category before uploading, so a template file can only contain rows for that category.** This changes the shape of export from "one file per run" to **one `.xlsx` per resolved category ID among the approved rows** — mixing categories into a single file isn't something you could actually upload. Rows with no resolved category (only reachable via a human `approved` override on a row that was blocked for missing category; normal validation never lets this through) land in a clearly-named `uncategorized` file rather than being silently dropped.

For each category file: `openpyxl` loads `Upload_Template.xlsx`, clears rows 2+, appends that category's approved rows in the template's exact column order (full width, header-driven — see §13 Open Decision 5 on confirming the real column count/letters before hardcoding any range). Header row untouched. Output: `out/jumia_upload_{category_id}_YYYYMMDD_HHMMSS.xlsx`, one per category.

Companion artifacts, shared across the whole run (not per-category — a blocked row isn't going into any Jumia upload, so splitting rejects by category doesn't apply the same way): `rejects.csv` (sku, rule_id, field, message), built from that run's `row_issues` (§6), not live `products` — so it stays accurate even after the next ingest overwrites staging. `margin_report.csv` and `run_summary.json` are still not built (blocked on Open Decision 6 for the former).

Idempotency via `feed_hash` — unchanged products are skipped unless `--force`.

---

## 12. Milestones

| M | Scope | Output |
|---|---|---|
| **M0** | Feed parse + SQLite staging (incl. `run_products.stage`) + bootstrap harvesters (filled template + Jumia guidelines workbook) | CLI dumps normalized products, seeds `id_label_catalog` — done: 745 products ingested, 177,373 id/label pairs harvested |
| **M1** | Rule engine (config schema-validated, collect-all evaluation) + pydantic model + export writer + unit tests + golden-file export test | Done: `validate`/`export` CLI, 49 tests passing, verified against real staged data (58/745 real products exported cleanly once brand+category resolved) |
| **M2** | Image probe pipeline + cache | Done: `image.py` (async, concurrency-capped, TTL-cached), wired into `validate`'s rule evaluation; verified against real Cloudinary images (correctly caught one real undersized image) |
| **M3** | Dashboard: run + review grid + image wall + run failure state surfaced | Done: FastAPI + Jinja2 + HTMX, `jumia-feed-sync serve` (or `start.ps1`); verified in-browser against real data including a live human-approve override changing what `export` actually wrote |
| **M4** | Unresolved queue with fuzzy candidates + `resolutions_history` audit trail | Done (pulled forward 2026-07-23, ahead of M3's original sequence): `resolve.py`, `/unresolved`; verified against real data — resolved a real brand (UGREEN → 1118344) and a real category through the actual UI, including one manual-entry case (fuzzy scoring had no good match) |
| **M5** | Rules/mapping editors in-app + margin report | Per-SKU field editing (`field_overrides`, narrower than the full Mapping screen) done this pass; Rules editor, full Mapping screen, and Margin report still open |

M0–M2 is a usable system. M3+ is the review layer.

---

## 13. Open Decisions

1. **Brand strategy — now a fully informed decision, not a guess.** All 6 sample template rows use `1045133 - Generic`. Checked against Jumia's real 175,460-brand reference list (§7): **every brand in your feed sample already has its own real Jumia code** — `1118344 - Ugreen`, `1036890 - Epson`, `1069068 - Lenovo`, `1017163 - Brother`, `1105916 - Sandisk`, `1071398 - Logitech`. Generic wasn't a fallback for missing brand codes; the real codes were available the whole time. Still your call whether to switch, but it's no longer "maybe UGREEN has an ID" — it does, confirmed. If you switch, `id_label_catalog` (source `jumia_reference`) already has every code you'd need; the RESOLVE stage just needs `resolutions` entries pointing feed `brand_raw` values at them instead of at Generic.
2. **Description quality.** In the feed sample, `g:description` is byte-identical to `g:title`. Jumia listings with a one-line description convert badly. Either fix the feed at source in GizmoJunction, or the system generates `short_description` bullets — which means an LLM step in the pipeline, worth scoping separately.
3. **Name length floor.** Your feed titles run ~60 chars; template examples run ~75. Confirm Jumia's actual minimum before setting `min_length: 20`.
4. **ParentSKU derivation — resolved as "not pure prefix-stripping."** Real evidence from the template: `T6641/T6642/T6643/T6644` all correctly parent to `T664` (a simple "strip last character" rule would work here). But `BT5000M` and `BT5000Y` both parent to **`BT5000N`** — a string that isn't a substring of either SKU. A prefix-stripping rule cannot produce that; it looks like a manually assigned parent identifier tied to a "neutral" placeholder variant. **Conclusion: ParentSKU can't be purely rule-derived.** Treat it like brand/category — a `resolutions`-style lookup (`kind='parent_sku'`) that falls back to manual entry, not a regex.
5. **Template column range — resolved.** Confirmed against the real header: `Name...Stock` = A–Q, category-conditional attributes (`battery_capacity...youtube_id`) = R–BH, images (`MainImage, Image2–8`) = BI–BP. Full width A–BP (68 columns), matching §1's "68-column XLSX template." §7 and §11 were both right, about different column ranges — no longer ambiguous, but the export writer should still derive column order from the parsed header at runtime (§14) rather than hardcoding these letters, since Jumia can change the template without notice.
6. **Commission & shipping sheet — still not provided, but the scope shrank.** Three real files have now been dropped: `Upload_Template.xlsx`, an internal GizmoJunction sales workbook (`Dashboard`/`Quotation`/`Invoice`/`Receipt`/`Products`/`Sales Log`/`Raw Import` — WooCommerce export, real customer/invoice data, correctly never committed), and Jumia's own guidelines workbook (`Introduction`/`Upload Template`/`Brands`/`Categories`/`Options` — see §7). None of the three is the commission/fee sheet. Since §7's UUID problem is now solved by the guidelines workbook, the commission sheet's *only* remaining job is fee/commission percentages for margin math — category resolution no longer depends on it at all. The sales workbook's `Products` sheet does have a per-SKU `Cost (KES)` column, the other half of margin math, but §6 has no `cost_kes` column to receive it yet. Needs a decision: import `cost_kes` from that sheet now (keyed by SKU) and locate the actual commission/fee sheet separately, or defer the Margin screen (M5) until both exist. Not urgent — M5 is the last milestone.
7. **`Options` sheet (guidelines workbook) is an unused asset worth scoping.** 270 rows × 12 columns of Jumia's actual valid values per attribute (`color_family`, `display_size`, `hdd_size`, `system_memory`, `warranty_type`, etc. — §8's rule engine has no `allowed_values` check type yet, and the category-attribute config in §7 has no way to constrain a field to a fixed enum). Worth a `bootstrap-options` harvester and an `allowed_values` rule check in M1/M5 — not done in this pass, flagged here so it isn't lost.

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

1. **Runs are resumable, not all-or-nothing — implemented at the image-cache layer, not via per-row stage persistence.** `run_products.stage` (§6) documents the conceptual pipeline (`ingested → resolved → probed → validated`), but the actual resumability mechanism (implemented M2) is `image_cache`: every probe result is committed durably, keyed by URL with a TTL, independent of which validation run asked for it. If the process dies mid-run — most likely during image probing, the only network-bound, slow stage — re-running `validate` recomputes mapping and rules instantly (in-memory, no I/O) and only re-fetches images that were never cached or have expired. This was a deliberate scope cut from the original idea of resuming a specific `run_id` row-by-row: the image cache alone delivers the actual reliability goal (a flaky Cloudinary response costs seconds, not the whole run) without the added complexity of tracking partial progress within one run.
2. **Config is schema-validated before it's used.** Rule YAML and category-attribute YAML are parsed into a pydantic model on save (Rules screen) and on load (CLI), not just exercised lazily at validation time. A malformed rule fails loudly in the editor, not silently mid-run three weeks later.
3. **`resolutions` is append-only in spirit.** Every write also lands in `resolutions_history` (§6). "Permanent institutional knowledge" needs to survive a fat-fingered correction, not just a first-time entry.
4. **Rule evaluation never short-circuits.** Every rule runs against every row regardless of earlier failures; `row_issues` collects the complete list. The dashboard's whole value proposition is showing a SKU's full problem list in one pass — an early `return` on first failure silently breaks that.
5. **A run has an explicit terminal failure state.** The background task wraps the pipeline in try/except; an unhandled exception sets `validation_runs.status = 'failed'` with `error_message` populated, not an indefinitely-spinning progress bar (§4 Run execution model).
6. **The export path is protected by a golden-file test.** One fixture feed + fixture template, checked into `tests/fixtures/`, with the expected output XLSX diffed cell-by-cell in CI/pre-commit. Column-order drift is the single failure mode that causes mass Jumia rejection, so it's the one thing that gets an end-to-end test rather than relying on unit tests of the pieces (§12 M1).

### Scale assumptions (confirmed 2026-07-23 against real data)

Every tech choice in §5 (SQLite, `pandas` one-time load, `rapidfuzz` per unresolved item) assumes roughly: **low thousands of SKUs per feed**, **one run at a time**, **runs triggered a few times a week, not continuously**. Confirmed: the live feed has **745 items** (M0 ingest, run against the real endpoint) — comfortably within the assumption. The fuzzy-match candidate universe is **1,913 categories** and **175,460 brands** (§7, harvested from Jumia's guidelines workbook, both far from the earlier "~30k" guess) — `rapidfuzz` scoring 745 unresolved items against 1,913 category candidates per run is trivial at C-speed; scoring against 175K brand candidates is the one number worth watching if brand fuzzy-matching ever gets slow, since it's two orders of magnitude larger than the category set. If GizmoJunction's real catalog grows an order of magnitude, or runs need to happen concurrently/on a tight schedule, revisit SQLite (§16) and consider caching fuzzy-match results per `raw_value` rather than rescoring identical unresolved strings every run.

---

## 16. Do we need Postgres?

No — SQLite stays, and the additions in this revision (resumable-run state, `resolutions_history`, config validation) don't change that. Postgres earns its operational cost (a service to run, credentials to manage, a backup story beyond "copy a file") when you have concurrent writers across a network, multiple tenants, or need transactional guarantees SQLite's file-level locking can't give you. This system has none of those: single operator, single machine, local-first, one run in flight at a time (§4). The one real concurrency case — the dashboard reading while a background run writes — is handled by WAL mode (§14), which is exactly what WAL is for.

Revisit this only if the project's shape changes: multiple people using the dashboard, the pipeline running on a server other people query concurrently, or genuinely large data volumes. None of those are in scope per §3 (single-tenant, no auth) or the non-goals in §2. Reintroducing Postgres later is a schema-port, not a rewrite — nothing here uses SQLite-only features — so there's no lock-in cost to deferring it.

---

Item 1 in Open Decisions is worth settling before M0 — if you should be using real brand IDs, the bootstrap harvester's seeded data is wrong from the start and every product inherits it.