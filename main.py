
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime, timezone

# =====================================================
# BINANCE FUTURES BOT V3.1
# RAILWAY - READ ONLY - NO ORDERS
# =====================================================

BASE_URL = "https://fapi.binance.com"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT"]
TIMEFRAMES = ["1m", "3m", "5m", "15m", "1h"]

SCAN_INTERVAL = 30
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
RSI_LEN = 14
ATR_LEN = 14
VOL_LEN = 20
VOL_MULT = 1.2
PIVOT_LEN = 5

session = requests.Session()
session.headers.update({"User-Agent": "FuturesBot-V3.1-ReadOnly"})

retry = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=frozenset(["GET"])
)
session.mount("https://", HTTPAdapter(max_retries=retry))


class RestrictedLocationError(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def api_get(endpoint, params=None):
    try:
        response = session.get(
            BASE_URL + endpoint,
            params=params or {},
            timeout=20
        )

        body = response.text[:1000]

        if "restricted location" in body.lower() or "eligibility" in body.lower():
            raise RestrictedLocationError(
                "Binance restricted this API request by location. "
                "Stop and contact Binance Support."
            )

        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict) and "code" in data and data.get("code", 0) < 0:
            raise RuntimeError(f"Binance API error: {data}")

        return data

    except RestrictedLocationError:
        raise
    except requests.RequestException as exc:
        raise RuntimeError(f"Network/API connection error: {exc}") from exc


def get_candles(symbol, interval):
    data = api_get(
        "/fapi/v1/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": 250
        }
    )

    current_ms = int(time.time() * 1000)

    candles = []
    for x in data:
        if int(x[6]) >= current_ms:
            continue

        candles.append({
            "time": int(x[0]),
            "open": float(x[1]),
            "high": float(x[2]),
            "low": float(x[3]),
            "close": float(x[4]),
            "volume": float(x[5])
        })

    return candles


def ema(values, period):
    if len(values) < period:
        return None

    result = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

    for value in values[period:]:
        result = value * multiplier + result * (1 - multiplier)

    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def ema_series(values, period):
    result = [None] * len(values)

    if len(values) < period:
        return result

    current = sum(values[:period]) / period
    result[period - 1] = current
    multiplier = 2 / (period + 1)

    for i in range(period, len(values)):
        current = values[i] * multiplier + current * (1 - multiplier)
        result[i] = current

    return result


def macd(values):
    fast = ema_series(values, 12)
    slow = ema_series(values, 26)

    line = [
        a - b if a is not None and b is not None else None
        for a, b in zip(fast, slow)
    ]

    valid = [x for x in line if x is not None]

    if len(valid) < 35:
        return None, None

    signal_series = ema_series(valid, 9)
    return valid[-1], signal_series[-1]


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    tr = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        tr.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        ))

    value = sum(tr[:period]) / period

    for x in tr[period:]:
        value = (value * (period - 1) + x) / period

    return value


def average_volume(candles, period=20):
    if len(candles) < period:
        return None

    return sum(x["volume"] for x in candles[-period:]) / period


def get_pivots(candles, strength=5):
    support = None
    resistance = None

    for i in range(strength, len(candles) - strength):
        left = candles[i-strength:i]
        right = candles[i+1:i+strength+1]

        high = candles[i]["high"]
        low = candles[i]["low"]

        if all(high > x["high"] for x in left + right):
            resistance = high

        if all(low < x["low"] for x in left + right):
            support = low

    return support, resistance


def calculate_signal(candles):
    if len(candles) < 220:
        raise ValueError("Not enough closed candles")

    closes = [x["close"] for x in candles]

    price = closes[-1]
    current = candles[-1]
    previous = candles[-2]

    e20 = ema(closes, EMA_FAST)
    e50 = ema(closes, EMA_MID)
    e200 = ema(closes, EMA_SLOW)

    rv = rsi(closes, RSI_LEN)
    av = atr(candles, ATR_LEN)
    ml, ms = macd(closes)
    va = average_volume(candles, VOL_LEN)

    values = [e20, e50, e200, rv, av, ml, ms, va]
    if any(x is None for x in values):
        raise ValueError("Indicator calculation incomplete")

    volume_ok = current["volume"] >= va * VOL_MULT

    bull_trend = price > e200 and e20 > e50
    bear_trend = price < e200 and e20 < e50

    bull_momentum = rv > 50 and ml > ms
    bear_momentum = rv < 50 and ml < ms

    support, resistance = get_pivots(candles, PIVOT_LEN)

    near_support = (
        support is not None and abs(price - support) <= av * 0.5
    )

    near_resistance = (
        resistance is not None and abs(price - resistance) <= av * 0.5
    )

    support_bounce = (
        support is not None
        and current["low"] <= support + av * 0.25
        and price > support
        and price > current["open"]
    )

    resistance_reject = (
        resistance is not None
        and current["high"] >= resistance - av * 0.25
        and price < resistance
        and price < current["open"]
    )

    resistance_break = (
        resistance is not None
        and previous["close"] <= resistance
        and price > resistance
        and price > current["open"]
    )

    support_break = (
        support is not None
        and previous["close"] >= support
        and price < support
        and price < current["open"]
    )

    buy = (
        bull_trend and bull_momentum and volume_ok
        and ((support_bounce and near_support) or resistance_break)
    )

    sell = (
        bear_trend and bear_momentum and volume_ok
        and ((resistance_reject and near_resistance) or support_break)
    )

    signal = "BUY" if buy else "SELL" if sell else "WAIT"

    return {
        "price": price,
        "ema20": e20,
        "ema50": e50,
        "ema200": e200,
        "rsi": rv,
        "macd": ml,
        "macd_signal": ms,
        "atr": av,
        "volume_ok": volume_ok,
        "support": support,
        "resistance": resistance,
        "trend": "BULLISH" if bull_trend else (
            "BEARISH" if bear_trend else "NEUTRAL"
        ),
        "signal": signal
    }


def scan_symbol(symbol):
    print("\n" + "-" * 55)
    print("SYMBOL:", symbol)

    results = {}

    for timeframe in TIMEFRAMES:
        candles = get_candles(symbol, timeframe)
        results[timeframe] = calculate_signal(candles)

    main = results["5m"]

    print("Price:", main["price"])
    print("Trend:", main["trend"])
    print("EMA 20:", round(main["ema20"], 5))
    print("EMA 50:", round(main["ema50"], 5))
    print("EMA 200:", round(main["ema200"], 5))
    print("RSI:", round(main["rsi"], 2))
    print("MACD:", round(main["macd"], 6))
    print("MACD Signal:", round(main["macd_signal"], 6))
    print("ATR:", round(main["atr"], 6))
    print("Volume OK:", main["volume_ok"])
    print("Support:", main["support"])
    print("Resistance:", main["resistance"])
    print("5m SIGNAL:", main["signal"])

    print("MULTI-TIMEFRAME:")
    for timeframe, result in results.items():
        print(timeframe, "=", result["signal"])

    print("READ-ONLY: NO ORDER SENT")


def main():
    print("=" * 60)
    print("BINANCE FUTURES BOT V3.1")
    print("RAILWAY READ-ONLY EDITION")
    print("NO API KEYS REQUIRED")
    print("NO ORDERS / NO LIVE TRADING")
    print("Endpoint:", BASE_URL)
    print("=" * 60)

    # Initial connectivity check
    api_get("/fapi/v1/ping")
    print("Public API connection successful.")

    scan_number = 0

    while True:
        scan_number += 1
        started = time.time()

        print("\n" + "=" * 60)
        print("SCAN:", scan_number)
        print("UTC:", utc_now())
        print("=" * 60)

        for symbol in SYMBOLS:
            try:
                scan_symbol(symbol)

            except RestrictedLocationError as exc:
                print("STOP:", exc)
                raise SystemExit(1)

            except Exception as exc:
                print(symbol, "ERROR:", str(exc))

        elapsed = time.time() - started
        sleep_time = max(1, SCAN_INTERVAL - elapsed)

        print("\nSCAN COMPLETE")
        print("Elapsed:", round(elapsed, 1), "seconds")
        print("Next scan in:", round(sleep_time, 1), "seconds")

        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
