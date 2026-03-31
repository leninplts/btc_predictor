"""
validate/check_websockets.py
-----------------------------
Nivel 3 — Validacion WebSocket en vivo (30 segundos).

Lanza los dos workers (RTDS + Market Channel) con una DB temporal,
espera 30 segundos de datos reales, luego analiza lo que se acumulo.

Checks:
  FAIL  -> el pipeline no puede funcionar correctamente
  WARN  -> puede ser normal dependiendo del estado del mercado

Uso:
  python validate/check_websockets.py
"""

import sys
import os
import time
import sqlite3
import tempfile
import asyncio
import json
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# Utilidades de reporte
# ---------------------------------------------------------------------------

RESULTS: list[tuple[str, str, str]] = []   # (status, name, msg)

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def _ok(name: str, msg: str = "") -> None:
    RESULTS.append(("OK", name, msg))
    print(f"  {GREEN}[OK]  {RESET} {name}" + (f" — {msg}" if msg else ""))

def _warn(name: str, msg: str) -> None:
    RESULTS.append(("WARN", name, msg))
    print(f"  {YELLOW}[WARN]{RESET} {name} — {msg}")

def _fail(name: str, msg: str) -> None:
    RESULTS.append(("FAIL", name, msg))
    print(f"  {RED}[FAIL]{RESET} {name} — {msg}")


# ---------------------------------------------------------------------------
# Captura de eventos de conexion desde el pipeline
# ---------------------------------------------------------------------------

class ConnectionProbe:
    """
    Inyectado en BotState para detectar si los WebSockets llegaron a conectarse.
    Escucha los logs de loguru capturando los mensajes de exito de conexion.
    """
    def __init__(self):
        self.rtds_connected   = False
        self.market_connected = False
        self._handler_id: Optional[int] = None

    def install(self):
        """Instala un sink en loguru que detecta mensajes de conexion exitosa."""
        from loguru import logger

        def _sink(message):
            text = str(message)
            if "RTDS: conectado" in text:
                self.rtds_connected = True
            if "Market WS: suscrito" in text:
                self.market_connected = True

        self._handler_id = logger.add(_sink, level="SUCCESS", format="{message}")

    def uninstall(self):
        if self._handler_id is not None:
            from loguru import logger
            try:
                logger.remove(self._handler_id)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Silenciar loguru durante la captura (no queremos flood en consola)
# ---------------------------------------------------------------------------

def _silence_loguru():
    """Elimina todos los handlers de loguru y agrega uno que descarta todo."""
    from loguru import logger
    logger.remove()
    # Solo mostrar ERROR o superior durante la captura
    logger.add(sys.stderr, level="ERROR", format="{time:HH:mm:ss} | {level} | {message}")


def _restore_loguru():
    """Restaura loguru a INFO en stdout."""
    from loguru import logger
    logger.remove()
    logger.add(
        sys.stdout,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        colorize=True
    )


# ---------------------------------------------------------------------------
# Pipeline runner con timeout
# ---------------------------------------------------------------------------

CAPTURE_SECONDS = 30

async def _run_with_timeout(state, probe: ConnectionProbe) -> None:
    """Lanza run_pipeline con timeout de CAPTURE_SECONDS segundos."""
    from data.websocket_client import run_pipeline

    probe.install()

    pipeline_task = asyncio.create_task(run_pipeline(state))

    try:
        await asyncio.wait_for(asyncio.shield(pipeline_task), timeout=CAPTURE_SECONDS)
    except asyncio.TimeoutError:
        pass   # normal — el timeout es el flujo esperado
    except Exception:
        pass
    finally:
        state.stop()
        pipeline_task.cancel()
        try:
            await pipeline_task
        except (asyncio.CancelledError, Exception):
            pass
        probe.uninstall()


# ---------------------------------------------------------------------------
# Evaluacion de la DB despues de la captura
# ---------------------------------------------------------------------------

def _evaluate(tmp_db: str, market: dict, state, probe: ConnectionProbe,
               capture_elapsed: float) -> None:
    """Lee la DB temporal y emite OK / WARN / FAIL por cada check."""

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row

    now_ms = int(time.time() * 1000)

    # ------------------------------------------------------------------
    # CHECK 1: RTDS Binance conectado
    # ------------------------------------------------------------------
    if probe.rtds_connected:
        _ok("rtds_binance_conectado", "mensaje de exito recibido en logs")
    else:
        # Puede conectar pero no loguear si silenciamos demasiado pronto
        # Usamos la DB como respaldo
        count_binance = conn.execute(
            "SELECT COUNT(*) as c FROM btc_prices WHERE source='binance'"
        ).fetchone()["c"]
        if count_binance > 0:
            _ok("rtds_binance_conectado", f"no log pero hay {count_binance} registros en DB")
        else:
            _fail("rtds_binance_conectado",
                  "no hay registros de Binance — RTDS no conecto o no emitio")

    # ------------------------------------------------------------------
    # CHECK 2: Market WS conectado
    # ------------------------------------------------------------------
    if probe.market_connected:
        _ok("market_ws_conectado", "mensaje de exito recibido en logs")
    else:
        count_ob = conn.execute(
            "SELECT COUNT(*) as c FROM orderbook_snapshots"
        ).fetchone()["c"]
        if count_ob > 0:
            _ok("market_ws_conectado", f"no log pero hay {count_ob} snapshots en DB")
        else:
            _fail("market_ws_conectado",
                  "no hay snapshots — Market WS no conecto o no emitio")

    # ------------------------------------------------------------------
    # CHECK 3: btc_prices Binance — minimo 1 registro
    # ------------------------------------------------------------------
    rows_binance = conn.execute(
        "SELECT COUNT(*) as c, MIN(price) as mn, MAX(price) as mx, MAX(ts) as last_ts "
        "FROM btc_prices WHERE source='binance'"
    ).fetchone()

    count_b = rows_binance["c"]
    if count_b >= 1:
        rate = count_b / capture_elapsed
        _ok("btc_prices_binance",
            f"{count_b} registros en {capture_elapsed:.0f}s (~{rate:.1f}/s)")
    else:
        _fail("btc_prices_binance",
              f"0 registros de precio BTC (Binance) en {capture_elapsed:.0f}s")

    # ------------------------------------------------------------------
    # CHECK 4: btc_prices Chainlink — WARN si 0 (puede no emitir siempre)
    # ------------------------------------------------------------------
    count_cl = conn.execute(
        "SELECT COUNT(*) as c FROM btc_prices WHERE source='chainlink'"
    ).fetchone()["c"]

    if count_cl >= 1:
        _ok("btc_prices_chainlink", f"{count_cl} registros")
    else:
        _warn("btc_prices_chainlink",
              f"0 registros en {capture_elapsed:.0f}s — Chainlink puede no emitir continuamente")

    # ------------------------------------------------------------------
    # CHECK 5: orderbook_snapshots — minimo 2 (YES + NO)
    # ------------------------------------------------------------------
    count_ob = conn.execute(
        "SELECT COUNT(*) as c FROM orderbook_snapshots"
    ).fetchone()["c"]

    distinct_assets = conn.execute(
        "SELECT COUNT(DISTINCT asset_id) as c FROM orderbook_snapshots"
    ).fetchone()["c"]

    if count_ob >= 2:
        _ok("orderbook_snapshots",
            f"{count_ob} snapshots para {distinct_assets} asset(s)")
    elif count_ob == 1:
        _warn("orderbook_snapshots",
              "solo 1 snapshot — esperaba 2 (YES + NO). Puede ser timing")
    else:
        _fail("orderbook_snapshots",
              f"0 snapshots — Market WS no recibio ningun book event")

    # ------------------------------------------------------------------
    # CHECK 6: price_changes — WARN si 0 (depende de actividad del book)
    # ------------------------------------------------------------------
    count_pc = conn.execute(
        "SELECT COUNT(*) as c FROM price_changes"
    ).fetchone()["c"]

    if count_pc >= 1:
        _ok("price_changes", f"{count_pc} cambios de nivel registrados")
    else:
        _warn("price_changes",
              "0 price_changes — book puede estar inactivo en este intervalo")

    # ------------------------------------------------------------------
    # CHECK 7: last_trades — WARN si 0 (depende de actividad)
    # ------------------------------------------------------------------
    count_lt = conn.execute(
        "SELECT COUNT(*) as c FROM last_trades"
    ).fetchone()["c"]

    if count_lt >= 1:
        _ok("last_trades", f"{count_lt} trades capturados")
    else:
        _warn("last_trades",
              "0 trades — puede que no haya habido ejecuciones en este intervalo")

    # ------------------------------------------------------------------
    # CHECK 8: Sanity check del precio BTC
    # ------------------------------------------------------------------
    BTC_MIN = 10_000.0
    BTC_MAX = 500_000.0

    latest_price_row = conn.execute(
        "SELECT price FROM btc_prices ORDER BY ts DESC LIMIT 1"
    ).fetchone()

    if latest_price_row:
        btc_price = latest_price_row["price"]
        if BTC_MIN <= btc_price <= BTC_MAX:
            _ok("btc_price_sanity",
                f"${btc_price:,.2f} (rango valido ${BTC_MIN:,.0f}–${BTC_MAX:,.0f})")
        else:
            _fail("btc_price_sanity",
                  f"precio BTC fuera de rango: ${btc_price:,.2f} "
                  f"(esperado ${BTC_MIN:,.0f}–${BTC_MAX:,.0f})")
    else:
        _fail("btc_price_sanity", "no hay registros de precio BTC")

    # ------------------------------------------------------------------
    # CHECK 9: asset_id en snapshots coincide con mercado activo
    # ------------------------------------------------------------------
    expected_ids = {market.get("asset_id_yes", ""), market.get("asset_id_no", "")}
    expected_ids.discard("")

    if expected_ids:
        rows_ids = conn.execute(
            "SELECT DISTINCT asset_id FROM orderbook_snapshots"
        ).fetchall()
        actual_ids = {row["asset_id"] for row in rows_ids}

        if actual_ids and actual_ids.issubset(expected_ids):
            _ok("asset_id_coincide",
                f"todos los asset_ids en DB son del mercado activo")
        elif actual_ids and not actual_ids.issubset(expected_ids):
            _warn("asset_id_coincide",
                  f"asset_ids en DB: {len(actual_ids)} | del mercado: {len(expected_ids)} — "
                  "puede haber data de mercado anterior si la DB ya tenia datos")
        else:
            _warn("asset_id_coincide", "no hay snapshots para verificar asset_ids")
    else:
        _warn("asset_id_coincide", "mercado sin asset_ids conocidos")

    # ------------------------------------------------------------------
    # CHECK 10: Timestamps recientes
    # ------------------------------------------------------------------
    latest_ts_row = conn.execute(
        "SELECT MAX(ts_recv) as t FROM btc_prices"
    ).fetchone()

    if latest_ts_row and latest_ts_row["t"]:
        delta_ms  = now_ms - latest_ts_row["t"]
        delta_s   = delta_ms / 1000
        threshold = 60.0   # segundos
        if delta_s <= threshold:
            _ok("timestamps_recientes",
                f"ultimo ts_recv hace {delta_s:.1f}s (threshold: {threshold}s)")
        else:
            _fail("timestamps_recientes",
                  f"ultimo registro hace {delta_s:.1f}s — demasiado antiguo")
    else:
        _fail("timestamps_recientes", "no hay registros con ts_recv")

    conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print()
    print(f"{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  VALIDACION FASE 1 — Nivel 3: WebSocket ({CAPTURE_SECONDS}s en vivo){RESET}")
    print(f"{BOLD}{'='*60}{RESET}")
    print()

    # 1. Descubrir mercado activo
    print(f"  Descubriendo mercado BTC 5-min activo...")
    from data.rest_client import get_active_btc_5m_market
    market = get_active_btc_5m_market()

    if not market:
        _fail("pre_requisito_mercado",
              "No hay mercado BTC 5-min activo ahora mismo — "
              "intenta en unos minutos cuando abra el siguiente intervalo")
        print()
        print(f"{BOLD}{'-'*60}{RESET}")
        print(f"  {RED}{BOLD}Nivel 3: FAIL — prerequisito fallido{RESET}")
        print(f"{BOLD}{'='*60}{RESET}")
        sys.exit(1)

    print(f"  Mercado: {market['question']}")
    print(f"  Slug   : {market['slug']}")
    print()

    # 2. DB temporal
    tmp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_file.close()
    tmp_db = tmp_file.name

    import data.storage as storage
    original_db_path = storage.DB_PATH
    storage.DB_PATH = tmp_db
    storage.init_db()

    # 3. Configurar BotState
    from data.websocket_client import BotState
    state = BotState()
    state.update_market(
        market["market_id"],
        market["asset_id_yes"],
        market["asset_id_no"]
    )

    # 4. Silenciar loguru + instalar probe
    probe = ConnectionProbe()
    _silence_loguru()

    # 5. Lanzar pipeline con countdown visible
    print(f"  Iniciando captura de {CAPTURE_SECONDS}s (conectando WebSockets)...")
    t_start = time.time()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Ticker en consola mientras corre
    async def _runner():
        pipeline = asyncio.create_task(_run_with_timeout(state, probe))
        elapsed = 0
        while elapsed < CAPTURE_SECONDS:
            await asyncio.sleep(5)
            elapsed += 5
            remaining = CAPTURE_SECONDS - elapsed
            # Contar registros actuales
            try:
                conn_tmp = sqlite3.connect(tmp_db)
                btc_c = conn_tmp.execute("SELECT COUNT(*) FROM btc_prices").fetchone()[0]
                ob_c  = conn_tmp.execute("SELECT COUNT(*) FROM orderbook_snapshots").fetchone()[0]
                conn_tmp.close()
            except Exception:
                btc_c = ob_c = "?"
            print(f"  [{elapsed:2d}s/{CAPTURE_SECONDS}s] "
                  f"btc_prices={btc_c} | orderbook_snapshots={ob_c} "
                  f"| {remaining}s restantes...")
        await pipeline

    try:
        loop.run_until_complete(_runner())
    except KeyboardInterrupt:
        print("\n  Captura interrumpida por el usuario")
        state.stop()
    finally:
        loop.close()

    capture_elapsed = time.time() - t_start

    # 6. Restaurar loguru y evaluar
    _restore_loguru()
    storage.DB_PATH = original_db_path

    print()
    print(f"  Captura completada en {capture_elapsed:.1f}s. Evaluando resultados...")
    print()

    _evaluate(tmp_db, market, state, probe, capture_elapsed)

    # 7. Limpiar
    try:
        os.unlink(tmp_db)
    except Exception:
        pass

    # 8. Reporte final
    ok_count   = sum(1 for s, _, _ in RESULTS if s == "OK")
    warn_count = sum(1 for s, _, _ in RESULTS if s == "WARN")
    fail_count = sum(1 for s, _, _ in RESULTS if s == "FAIL")

    print()
    print(f"{BOLD}{'-'*60}{RESET}")
    print(f"  RESULTADO: "
          f"{GREEN}{ok_count} OK{RESET} | "
          f"{YELLOW}{warn_count} WARN{RESET} | "
          f"{RED}{fail_count} FAIL{RESET}")

    if fail_count == 0:
        print(f"  {GREEN}{BOLD}Nivel 3: PASS [OK]{RESET}")
    else:
        print(f"  {RED}{BOLD}Nivel 3: FAIL [FAIL]{RESET}")
        print()
        print("  Checks fallidos:")
        for status, name, msg in RESULTS:
            if status == "FAIL":
                print(f"    - {name}: {msg}")

    print(f"{BOLD}{'='*60}{RESET}")
    print()

    sys.exit(0 if fail_count == 0 else 1)


if __name__ == "__main__":
    main()
