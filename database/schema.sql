-- database/schema.sql
-- 무한매수법 V4.0 - SQLite 초기화

CREATE TABLE IF NOT EXISTS state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT DEFAULT 'normal',
    t_value REAL DEFAULT 0.0,
    principal REAL DEFAULT 20000.0,
    balance REAL DEFAULT 20000.0,
    total_quantity INTEGER DEFAULT 0,
    avg_price REAL DEFAULT 0.0,
    reverse_start_t REAL DEFAULT 0.0,
    completed_cycles INTEGER DEFAULT 0,
    stock_code TEXT DEFAULT 'TQQQ',
    division INTEGER DEFAULT 40,
    updated_at TEXT
);

INSERT OR IGNORE INTO state (id, mode, principal, balance, stock_code, division)
VALUES (1, 'normal', 20000.0, 20000.0, 'TQQQ', 40);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT,
    order_type TEXT,
    side TEXT,
    stock_code TEXT,
    quantity INTEGER,
    price REAL,
    t_before REAL,
    t_after REAL,
    mode TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS completed_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_number INTEGER UNIQUE,
    stock_code TEXT,
    start_date TEXT,
    end_date TEXT,
    principal REAL,
    final_balance REAL,
    pnl REAL,
    return_pct REAL
);

CREATE TABLE IF NOT EXISTS backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backup_time TEXT DEFAULT (datetime('now')),
    state_json TEXT,
    reason TEXT
);
