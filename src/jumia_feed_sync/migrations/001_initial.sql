-- Initial schema. See Readme.md #6 (Data Model) for the narrative version.

CREATE TABLE products (
    sku TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    image_link TEXT,
    price_kes REAL,
    sale_price_kes REAL,
    brand_raw TEXT,
    product_type_raw TEXT,
    availability TEXT,
    condition TEXT,
    fetched_at TEXT NOT NULL,
    feed_hash TEXT NOT NULL
);

CREATE TABLE id_label_catalog (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('brand', 'category', 'parent_sku')),
    jumia_id TEXT NOT NULL,
    jumia_label TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('template', 'jumia_reference', 'commission_sheet', 'manual')),
    first_seen_at TEXT NOT NULL,
    UNIQUE (kind, jumia_id)
);

CREATE TABLE resolutions (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('brand', 'category', 'parent_sku')),
    raw_value TEXT NOT NULL,
    jumia_id TEXT NOT NULL,
    jumia_label TEXT NOT NULL,
    confidence REAL,
    confirmed_by_human INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE (kind, raw_value)
);

CREATE TABLE resolutions_history (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    raw_value TEXT NOT NULL,
    jumia_id TEXT NOT NULL,
    jumia_label TEXT NOT NULL,
    confirmed_by_human INTEGER NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE validation_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')) DEFAULT 'running',
    error_message TEXT,
    feed_item_count INTEGER,
    passed INTEGER,
    failed INTEGER,
    exported_path TEXT
);

CREATE TABLE run_products (
    run_id INTEGER NOT NULL REFERENCES validation_runs(id),
    sku TEXT NOT NULL,
    title TEXT,
    price_kes REAL,
    brand_resolved TEXT,
    category_resolved TEXT,
    stage TEXT NOT NULL CHECK (stage IN ('ingested', 'resolved', 'probed', 'validated')) DEFAULT 'ingested',
    status TEXT CHECK (status IN ('passed', 'warned', 'blocked')),
    feed_hash TEXT NOT NULL,
    PRIMARY KEY (run_id, sku)
);

CREATE TABLE row_issues (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES validation_runs(id),
    sku TEXT NOT NULL,
    field TEXT,
    severity TEXT NOT NULL CHECK (severity IN ('block', 'warn')),
    rule_id TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE image_cache (
    url TEXT PRIMARY KEY,
    status_code INTEGER,
    width INTEGER,
    height INTEGER,
    bytes INTEGER,
    corner_luminance REAL,
    checked_at TEXT NOT NULL
);

CREATE INDEX idx_row_issues_run ON row_issues(run_id);
CREATE INDEX idx_run_products_run ON run_products(run_id);
