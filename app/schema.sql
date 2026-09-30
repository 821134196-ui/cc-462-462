-- 博物馆语音导览器借还周转系统
CREATE TABLE IF NOT EXISTS sites (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    branch TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id),
    current_site_id TEXT NOT NULL REFERENCES sites(id),
    battery INTEGER NOT NULL DEFAULT 100,
    -- available | on_loan | return_check | pending_disinfection
    -- | pending_charge | charging | repair | in_transit
    status TEXT NOT NULL DEFAULT 'available',
    lang_code TEXT NOT NULL,
    lang_version TEXT NOT NULL,
    lang_ok INTEGER NOT NULL DEFAULT 1,
    earphone INTEGER NOT NULL DEFAULT 1,
    strap INTEGER NOT NULL DEFAULT 1,
    shell_ok INTEGER NOT NULL DEFAULT 1,
    button_ok INTEGER NOT NULL DEFAULT 1,
    pending_transfer INTEGER NOT NULL DEFAULT 0,
    slot_no TEXT,
    last_release_batch INTEGER,
    note TEXT
);

CREATE TABLE IF NOT EXISTS visitors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id),
    visitor_id TEXT NOT NULL REFERENCES visitors(id),
    site_id TEXT NOT NULL REFERENCES sites(id),
    lang_code TEXT NOT NULL,
    issued_at REAL NOT NULL,
    due_at REAL NOT NULL,
    returned_at REAL,
    status TEXT NOT NULL DEFAULT 'active', -- active | overdue | returned
    deposit_amount INTEGER NOT NULL,
    deposit_status TEXT NOT NULL DEFAULT 'held', -- held | refunded | partially_refunded
    refunded_amount INTEGER NOT NULL DEFAULT 0,
    refunded_at REAL,
    refund_receipt TEXT,
    earnings INTEGER NOT NULL DEFAULT 0,
    fee_overdue INTEGER NOT NULL DEFAULT 0,
    fee_earphone INTEGER NOT NULL DEFAULT 0,
    fee_strap INTEGER NOT NULL DEFAULT 0,
    fee_damage INTEGER NOT NULL DEFAULT 0,
    dispute INTEGER NOT NULL DEFAULT 0,
    return_site_id TEXT REFERENCES sites(id),
    cross_site INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_device ON orders(device_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS charging_slots (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id),
    label TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 1,
    device_id TEXT REFERENCES devices(id),
    note TEXT
);

CREATE TABLE IF NOT EXISTS charging_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id TEXT NOT NULL REFERENCES charging_slots(id),
    device_id TEXT NOT NULL REFERENCES devices(id),
    started_at REAL NOT NULL,
    ended_at REAL,
    result TEXT
);

CREATE TABLE IF NOT EXISTS disinfect_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id TEXT NOT NULL REFERENCES sites(id),
    created_at REAL NOT NULL,
    sterilized_at REAL,
    released_at REAL,
    released_by TEXT,
    status TEXT NOT NULL DEFAULT 'sterilizing', -- sterilizing | released
    note TEXT
);

CREATE TABLE IF NOT EXISTS disinfect_items (
    batch_id INTEGER NOT NULL REFERENCES disinfect_batches(id),
    device_id TEXT NOT NULL REFERENCES devices(id),
    PRIMARY KEY (batch_id, device_id)
);

CREATE TABLE IF NOT EXISTS repair_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL REFERENCES devices(id),
    site_id TEXT NOT NULL REFERENCES sites(id),
    reported_by TEXT NOT NULL,
    issue TEXT NOT NULL,
    category TEXT NOT NULL, -- lang | accessory | shell | button | slot | other
    status TEXT NOT NULL DEFAULT 'open', -- open | repairing | resolved
    created_at REAL NOT NULL,
    resolved_at REAL,
    resolution TEXT
);

CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT
);
