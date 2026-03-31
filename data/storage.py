"""
storage.py
----------
Capa de persistencia SQLite para todos los datos del pipeline.

Tablas:
  - btc_prices          : precios BTC en tiempo real (Binance + Chainlink)
  - orderbook_snapshots : snapshots completos del order book de Polymarket
  - price_changes       : cambios nivel a nivel del order book (tick data)
  - last_trades         : ultimo precio de cada trade ejecutado en Polymarket
  - resolved_markets    : mercados BTC 5-min resueltos con su resultado
  - active_markets      : registro de mercados activos descubiertos
"""

import sqlite3
import json
import os
from datetime import datetime, timezone
from typing import Optional
from loguru import logger


DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "pipeline.db")


def get_connection() -> sqlite3.Connection:
    """Abre (o crea) la base de datos y devuelve la conexion."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # escrituras concurrentes sin lock
    conn.execute("PRAGMA synchronous=NORMAL") # balance velocidad/durabilidad
    return conn


def init_db() -> None:
    """Crea todas las tablas si no existen. Idempotente."""
    conn = get_connection()
    cur = conn.cursor()

    # --- Precios BTC en tiempo real ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS btc_prices (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          INTEGER NOT NULL,          -- unix ms (del proveedor)
            ts_recv     INTEGER NOT NULL,          -- unix ms (cuando lo recibimos)
            source      TEXT    NOT NULL,          -- 'binance' | 'chainlink'
            symbol      TEXT    NOT NULL,          -- 'btcusdt' | 'btc/usd'
            price       REAL    NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_btc_prices_ts ON btc_prices(ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_btc_prices_source ON btc_prices(source, ts)")

    # --- Snapshots completos del order book ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orderbook_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          INTEGER NOT NULL,          -- unix ms del evento
            ts_recv     INTEGER NOT NULL,
            asset_id    TEXT    NOT NULL,          -- token ID (YES o NO)
            market_id   TEXT    NOT NULL,          -- condition ID
            bids        TEXT    NOT NULL,          -- JSON [{price, size}, ...]
            asks        TEXT    NOT NULL,          -- JSON [{price, size}, ...]
            hash        TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ob_ts ON orderbook_snapshots(ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ob_asset ON orderbook_snapshots(asset_id, ts)")

    # --- Cambios nivel a nivel del order book (stream continuo) ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_changes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          INTEGER NOT NULL,          -- unix ms del evento
            ts_recv     INTEGER NOT NULL,
            asset_id    TEXT    NOT NULL,
            market_id   TEXT    NOT NULL,
            price       REAL    NOT NULL,
            size        REAL    NOT NULL,          -- 0 = nivel eliminado
            side        TEXT    NOT NULL,          -- 'BUY' | 'SELL'
            best_bid    REAL,
            best_ask    REAL,
            hash        TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pc_ts ON price_changes(ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pc_asset ON price_changes(asset_id, ts)")

    # --- Ultimo precio de cada trade ejecutado ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS last_trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          INTEGER NOT NULL,          -- unix ms del trade
            ts_recv     INTEGER NOT NULL,
            asset_id    TEXT    NOT NULL,
            market_id   TEXT    NOT NULL,
            price       REAL    NOT NULL,
            size        REAL    NOT NULL,
            side        TEXT    NOT NULL,          -- 'BUY' | 'SELL'
            fee_rate_bps TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_lt_ts ON last_trades(ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_lt_asset ON last_trades(asset_id, ts)")

    # --- Mercados resueltos (ground truth para entrenar el modelo) ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS resolved_markets (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id       TEXT    NOT NULL UNIQUE, -- condition ID
            asset_id_yes    TEXT    NOT NULL,
            asset_id_no     TEXT    NOT NULL,
            question        TEXT,
            slug            TEXT,
            winning_outcome TEXT,                   -- 'Yes' | 'No'
            winning_asset   TEXT,
            btc_price_open  REAL,                   -- precio BTC al abrir
            btc_price_close REAL,                   -- precio BTC al resolver
            direction       TEXT,                   -- 'UP' | 'DOWN' | NULL
            ts_open         INTEGER,
            ts_resolved     INTEGER NOT NULL,
            ts_recv         INTEGER NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rm_ts ON resolved_markets(ts_resolved)")

    # --- Mercados activos conocidos ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS active_markets (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id       TEXT    NOT NULL UNIQUE,
            asset_id_yes    TEXT    NOT NULL,
            asset_id_no     TEXT    NOT NULL,
            question        TEXT,
            slug            TEXT,
            description     TEXT,
            status          TEXT    DEFAULT 'active', -- 'active' | 'resolved' | 'cancelled'
            ts_discovered   INTEGER NOT NULL,
            ts_updated      INTEGER NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_am_status ON active_markets(status)")

    conn.commit()
    conn.close()
    logger.info(f"Base de datos inicializada: {os.path.abspath(DB_PATH)}")


# ---------------------------------------------------------------------------
# Funciones de insercion
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    """Timestamp actual en milisegundos UTC."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def insert_btc_price(source: str, symbol: str, price: float, ts: int) -> None:
    """Guarda un tick de precio BTC."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO btc_prices (ts, ts_recv, source, symbol, price) VALUES (?,?,?,?,?)",
            (ts, _now_ms(), source, symbol, price)
        )
        conn.commit()
    finally:
        conn.close()


def insert_orderbook_snapshot(
    ts: int,
    asset_id: str,
    market_id: str,
    bids: list,
    asks: list,
    hash_val: Optional[str] = None
) -> None:
    """Guarda un snapshot completo del order book."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO orderbook_snapshots
               (ts, ts_recv, asset_id, market_id, bids, asks, hash)
               VALUES (?,?,?,?,?,?,?)""",
            (ts, _now_ms(), asset_id, market_id,
             json.dumps(bids), json.dumps(asks), hash_val)
        )
        conn.commit()
    finally:
        conn.close()


def insert_price_change(
    ts: int,
    asset_id: str,
    market_id: str,
    price: float,
    size: float,
    side: str,
    best_bid: Optional[float] = None,
    best_ask: Optional[float] = None,
    hash_val: Optional[str] = None
) -> None:
    """Guarda un cambio de nivel en el order book."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO price_changes
               (ts, ts_recv, asset_id, market_id, price, size, side, best_bid, best_ask, hash)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (ts, _now_ms(), asset_id, market_id,
             price, size, side, best_bid, best_ask, hash_val)
        )
        conn.commit()
    finally:
        conn.close()


def insert_last_trade(
    ts: int,
    asset_id: str,
    market_id: str,
    price: float,
    size: float,
    side: str,
    fee_rate_bps: Optional[str] = None
) -> None:
    """Guarda el precio del ultimo trade ejecutado."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO last_trades
               (ts, ts_recv, asset_id, market_id, price, size, side, fee_rate_bps)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ts, _now_ms(), asset_id, market_id,
             price, size, side, fee_rate_bps)
        )
        conn.commit()
    finally:
        conn.close()


def upsert_active_market(
    market_id: str,
    asset_id_yes: str,
    asset_id_no: str,
    question: str = "",
    slug: str = "",
    description: str = ""
) -> None:
    """Registra o actualiza un mercado activo."""
    conn = get_connection()
    now = _now_ms()
    try:
        conn.execute(
            """INSERT INTO active_markets
               (market_id, asset_id_yes, asset_id_no, question, slug, description,
                status, ts_discovered, ts_updated)
               VALUES (?,?,?,?,?,?,'active',?,?)
               ON CONFLICT(market_id) DO UPDATE SET
                 question=excluded.question,
                 slug=excluded.slug,
                 description=excluded.description,
                 status='active',
                 ts_updated=excluded.ts_updated""",
            (market_id, asset_id_yes, asset_id_no,
             question, slug, description, now, now)
        )
        conn.commit()
    finally:
        conn.close()


def insert_resolved_market(
    market_id: str,
    asset_id_yes: str,
    asset_id_no: str,
    winning_outcome: str,
    winning_asset: str,
    question: str = "",
    slug: str = "",
    btc_price_open: Optional[float] = None,
    btc_price_close: Optional[float] = None,
    ts_open: Optional[int] = None,
    ts_resolved: Optional[int] = None
) -> None:
    """Guarda un mercado resuelto. Calcula direccion UP/DOWN si hay precios."""
    direction = None
    if btc_price_open is not None and btc_price_close is not None:
        direction = "UP" if btc_price_close > btc_price_open else "DOWN"

    conn = get_connection()
    now = _now_ms()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO resolved_markets
               (market_id, asset_id_yes, asset_id_no, question, slug,
                winning_outcome, winning_asset, btc_price_open, btc_price_close,
                direction, ts_open, ts_resolved, ts_recv)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (market_id, asset_id_yes, asset_id_no, question, slug,
             winning_outcome, winning_asset, btc_price_open, btc_price_close,
             direction, ts_open, ts_resolved or now, now)
        )
        # Marcar como resuelto en active_markets
        conn.execute(
            "UPDATE active_markets SET status='resolved', ts_updated=? WHERE market_id=?",
            (now, market_id)
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Funciones de consulta
# ---------------------------------------------------------------------------

def get_latest_btc_price(source: str = "binance") -> Optional[dict]:
    """Devuelve el precio BTC mas reciente de la fuente indicada."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM btc_prices WHERE source=? ORDER BY ts DESC LIMIT 1",
            (source,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_active_markets() -> list[dict]:
    """Devuelve todos los mercados activos registrados."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM active_markets WHERE status='active'"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_unresolved_markets() -> list[dict]:
    """
    Devuelve mercados en active_markets con status='active'
    cuyo intervalo de 5-min ya deberia haber terminado.
    El slug tiene el formato btc-updown-5m-<unix_ts>, y el mercado
    cierra 300 segundos despues de ese timestamp.
    """
    conn = get_connection()
    try:
        now_ms = _now_ms()
        rows = conn.execute(
            "SELECT * FROM active_markets WHERE status='active'"
        ).fetchall()
        results = []
        for r in rows:
            slug = r["slug"] or ""
            parts = slug.split("-")
            if len(parts) >= 4 and parts[-1].isdigit():
                ts_start = int(parts[-1])
                ts_end_ms = (ts_start + 300) * 1000
                # Solo incluir mercados cuyo intervalo termino hace >60s
                if now_ms > ts_end_ms + 60_000:
                    results.append(dict(r))
        return results
    finally:
        conn.close()


def get_btc_price_at(ts_ms: int, source: str = "chainlink", tolerance_ms: int = 30_000) -> Optional[float]:
    """
    Devuelve el precio BTC mas cercano al timestamp dado.
    Busca en un rango de +-tolerance_ms milisegundos.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT price FROM btc_prices
               WHERE source=? AND ts BETWEEN ? AND ?
               ORDER BY ABS(ts - ?) ASC
               LIMIT 1""",
            (source, ts_ms - tolerance_ms, ts_ms + tolerance_ms, ts_ms)
        ).fetchone()
        return row["price"] if row else None
    finally:
        conn.close()


def get_db_stats() -> dict:
    """Estadisticas rapidas de cuantos registros hay en cada tabla."""
    conn = get_connection()
    stats = {}
    tables = [
        "btc_prices", "orderbook_snapshots", "price_changes",
        "last_trades", "resolved_markets", "active_markets"
    ]
    try:
        for t in tables:
            row = conn.execute(f"SELECT COUNT(*) as c FROM {t}").fetchone()
            stats[t] = row["c"]
    finally:
        conn.close()
    return stats
