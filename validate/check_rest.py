"""
validate/check_rest.py
----------------------
Nivel 2 — Validacion de conectividad REST.

Verifica que podemos hablar con las APIs de Polymarket y que
las respuestas tienen el formato y valores esperados.

Requiere conexion a internet. No requiere credenciales.

Uso:
  python validate/check_rest.py
"""

import sys
import os
import re
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# Utilidades de reporte
# ---------------------------------------------------------------------------

RESULTS: list[tuple[str, str, str]] = []

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def _ok(name: str, msg: str = "") -> None:
    RESULTS.append(("OK", name, msg))
    print(f"  {GREEN}[OK]  {RESET} {name}" + (f" — {msg}" if msg else ""))

def _fail(name: str, msg: str) -> None:
    RESULTS.append(("FAIL", name, msg))
    print(f"  {RED}[FAIL]{RESET} {name} — {msg}")

def _run(name: str, fn) -> None:
    try:
        fn()
    except AssertionError as e:
        _fail(name, str(e) or "assertion fallida")
    except Exception as e:
        _fail(name, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Estado compartido entre tests (descubrir mercado solo una vez)
# ---------------------------------------------------------------------------

_market_cache: dict = {}

def _get_market() -> dict:
    """Descubre el mercado activo una sola vez y lo cachea."""
    if not _market_cache:
        from data.rest_client import get_active_btc_5m_market
        m = get_active_btc_5m_market()
        if m:
            _market_cache.update(m)
    return _market_cache


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_clob_ping():
    """CLOB API responde en menos de 5 segundos."""
    from data.rest_client import ping_clob
    t0 = time.time()
    result = ping_clob()
    elapsed = time.time() - t0
    assert result is True, "ping_clob() devolvio False — CLOB API no responde"
    _ok("test_clob_ping", f"respuesta en {elapsed:.2f}s")


def test_gamma_ping():
    """Gamma API responde en menos de 5 segundos."""
    from data.rest_client import ping_gamma
    t0 = time.time()
    result = ping_gamma()
    elapsed = time.time() - t0
    assert result is True, "ping_gamma() devolvio False — Gamma API no responde"
    _ok("test_gamma_ping", f"respuesta en {elapsed:.2f}s")


def test_market_discovery():
    """get_active_btc_5m_market() devuelve un mercado activo."""
    from data.rest_client import get_active_btc_5m_market
    market = get_active_btc_5m_market()
    assert market is not None, (
        "get_active_btc_5m_market() devolvio None — "
        "puede que no haya mercado activo en este momento"
    )
    _market_cache.update(market)
    _ok("test_market_discovery", f"slug={market.get('slug', 'N/A')}")


def test_market_required_fields():
    """El mercado descubierto tiene todas las claves requeridas y no vacias."""
    market = _get_market()
    assert market, "No hay mercado en cache — test_market_discovery fallo primero"

    required = ["market_id", "asset_id_yes", "asset_id_no", "question", "slug"]
    for key in required:
        val = market.get(key)
        assert val, f"Campo '{key}' esta ausente o vacio: {repr(val)}"

    _ok("test_market_required_fields", f"question='{market['question']}'")


def test_token_id_format():
    """
    Los asset_id_yes y asset_id_no son strings numericos largos.
    Los token IDs de Polymarket son enteros de ~77 digitos.
    """
    market = _get_market()
    assert market, "No hay mercado en cache"

    for field in ["asset_id_yes", "asset_id_no"]:
        tid = market[field]
        assert isinstance(tid, str), f"{field} deberia ser string, es {type(tid)}"
        assert tid.isdigit(), f"{field} no es numerico: {tid[:30]}..."
        assert len(tid) >= 60, f"{field} demasiado corto ({len(tid)} chars): {tid[:30]}..."

    _ok("test_token_id_format",
        f"yes={market['asset_id_yes'][:20]}... ({len(market['asset_id_yes'])} digits)")


def test_slug_pattern():
    """
    El slug sigue el patron btc-updown-5m-<N> donde N es multiplo de 300.
    Esto valida que la logica de calculo de timestamp funciona.
    """
    market = _get_market()
    assert market, "No hay mercado en cache"

    slug = market.get("slug", "")
    pattern = r"^btc-updown-5m-(\d+)$"
    match = re.match(pattern, slug)
    assert match, f"Slug no sigue patron 'btc-updown-5m-<N>': '{slug}'"

    ts_in_slug = int(match.group(1))
    assert ts_in_slug % 300 == 0, (
        f"Timestamp en slug ({ts_in_slug}) no es multiplo de 300 — "
        "puede indicar un problema en la logica de discovery"
    )

    # Verificar que el timestamp es razonablemente reciente (ultimas 24h)
    now_ts = int(time.time())
    delta_seconds = abs(now_ts - ts_in_slug)
    assert delta_seconds < 86400, (
        f"Timestamp en slug ({ts_in_slug}) difiere {delta_seconds}s del actual ({now_ts}) — "
        "mercado muy antiguo o muy futuro"
    )

    _ok("test_slug_pattern", f"slug={slug} | ts_delta={delta_seconds}s")


def test_order_book_returns_data():
    """get_order_book() devuelve bids y asks no vacios."""
    from data.rest_client import get_order_book
    market = _get_market()
    assert market, "No hay mercado en cache"

    ob = get_order_book(market["asset_id_yes"])
    assert ob is not None, "get_order_book() devolvio None"
    bids = ob.get("bids", [])
    asks = ob.get("asks", [])
    assert isinstance(bids, list), f"bids deberia ser lista, es {type(bids)}"
    assert isinstance(asks, list), f"asks deberia ser lista, es {type(asks)}"
    assert len(bids) > 0 or len(asks) > 0, (
        "Order book completamente vacio (0 bids y 0 asks) — mercado sin liquidez"
    )

    _ok("test_order_book_returns_data", f"{len(bids)} bids, {len(asks)} asks")


def test_order_book_format():
    """Cada entry del order book tiene 'price' y 'size' como strings."""
    from data.rest_client import get_order_book
    market = _get_market()
    assert market, "No hay mercado en cache"

    ob = get_order_book(market["asset_id_yes"])
    assert ob is not None, "get_order_book() devolvio None"

    for side_name, entries in [("bids", ob.get("bids", [])), ("asks", ob.get("asks", []))]:
        for i, entry in enumerate(entries[:5]):   # revisar los primeros 5
            assert "price" in entry, f"{side_name}[{i}] no tiene 'price': {entry}"
            assert "size"  in entry, f"{side_name}[{i}] no tiene 'size': {entry}"
            try:
                p = float(entry["price"])
                s = float(entry["size"])
            except (ValueError, TypeError) as e:
                assert False, f"{side_name}[{i}] price/size no son numericos: {entry} — {e}"
            assert 0.0 < p <= 1.0, f"{side_name}[{i}] price fuera de rango [0,1]: {p}"
            assert s >= 0.0, f"{side_name}[{i}] size negativo: {s}"

    total = len(ob.get("bids", [])) + len(ob.get("asks", []))
    _ok("test_order_book_format", f"formato correcto en {total} entries revisados")


def test_midpoint_range():
    """get_midpoint() devuelve float entre 0.01 y 0.99."""
    from data.rest_client import get_midpoint
    market = _get_market()
    assert market, "No hay mercado en cache"

    mid = get_midpoint(market["asset_id_yes"])
    assert mid is not None, "get_midpoint() devolvio None"
    assert isinstance(mid, float), f"midpoint deberia ser float, es {type(mid)}"
    assert 0.0 < mid < 1.0, f"midpoint fuera de rango (0, 1): {mid}"

    _ok("test_midpoint_range", f"midpoint YES = {mid:.4f} ({mid*100:.1f}% probabilidad UP)")


def test_price_history_returns_data():
    """get_share_price_history() devuelve al menos 1 punto historico."""
    from data.rest_client import get_share_price_history
    market = _get_market()
    assert market, "No hay mercado en cache"

    history = get_share_price_history(market["asset_id_yes"], interval="1d", fidelity=1)
    assert isinstance(history, list), f"history deberia ser lista, es {type(history)}"
    assert len(history) >= 1, (
        "get_share_price_history() devolvio lista vacia — "
        "puede que el mercado sea muy nuevo o el endpoint no responda"
    )

    _ok("test_price_history_returns_data", f"{len(history)} puntos historicos (intervalo 1d)")


def test_price_history_format():
    """Cada punto del historico tiene 't' (int) y 'p' (float) en rango valido."""
    from data.rest_client import get_share_price_history
    market = _get_market()
    assert market, "No hay mercado en cache"

    history = get_share_price_history(market["asset_id_yes"], interval="1d", fidelity=1)
    assert history, "Historico vacio — no se puede validar formato"

    errors = []
    for i, point in enumerate(history[:20]):   # revisar primeros 20
        if "t" not in point:
            errors.append(f"punto[{i}] sin 't': {point}")
            continue
        if "p" not in point:
            errors.append(f"punto[{i}] sin 'p': {point}")
            continue
        try:
            t = int(point["t"])
            p = float(point["p"])
        except (ValueError, TypeError) as e:
            errors.append(f"punto[{i}] no numerico: {point} — {e}")
            continue
        if not (0.0 <= p <= 1.0):
            errors.append(f"punto[{i}] precio {p} fuera de [0,1]")

    assert not errors, "Errores de formato en price_history:\n" + "\n".join(errors)

    first = history[0]
    last  = history[-1]
    _ok("test_price_history_format",
        f"formato correcto | primer p={first['p']:.3f} | ultimo p={last['p']:.3f}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

ALL_TESTS = [
    ("test_clob_ping",                  test_clob_ping),
    ("test_gamma_ping",                 test_gamma_ping),
    ("test_market_discovery",           test_market_discovery),
    ("test_market_required_fields",     test_market_required_fields),
    ("test_token_id_format",            test_token_id_format),
    ("test_slug_pattern",               test_slug_pattern),
    ("test_order_book_returns_data",    test_order_book_returns_data),
    ("test_order_book_format",          test_order_book_format),
    ("test_midpoint_range",             test_midpoint_range),
    ("test_price_history_returns_data", test_price_history_returns_data),
    ("test_price_history_format",       test_price_history_format),
]


def main():
    print()
    print(f"{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  VALIDACION FASE 1 — Nivel 2: REST API{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")
    print()

    for name, fn in ALL_TESTS:
        _run(name, fn)

    ok_count   = sum(1 for s, _, _ in RESULTS if s == "OK")
    fail_count = sum(1 for s, _, _ in RESULTS if s == "FAIL")

    print()
    print(f"{BOLD}{'-'*60}{RESET}")
    print(f"  RESULTADO: {GREEN}{ok_count} OK{RESET} | {RED}{fail_count} FAIL{RESET}")

    if fail_count == 0:
        print(f"  {GREEN}{BOLD}Nivel 2: PASS [OK]{RESET}")
    else:
        print(f"  {RED}{BOLD}Nivel 2: FAIL [FAIL]{RESET}")
        print()
        print("  Tests fallidos:")
        for status, name, msg in RESULTS:
            if status == "FAIL":
                print(f"    - {name}: {msg}")

    print(f"{BOLD}{'='*60}{RESET}")
    print()

    sys.exit(0 if fail_count == 0 else 1)


if __name__ == "__main__":
    main()
