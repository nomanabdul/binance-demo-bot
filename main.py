import csv
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==========================================================
# BINANCE USDT-M FUTURES DEMO BOT V3.0 - RAILWAY EDITION
# DEMO ONLY: fixed demo-fapi URL; no live endpoint/fallback.
# ==========================================================

BASE_URL = "https://demo-fapi.binance.com"
API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()

if not API_KEY or not API_SECRET:
    raise SystemExit(
        "Missing BINANCE_API_KEY / BINANCE_API_SECRET Railway variables."
    )

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT"]

MARGIN = 20.0
LEVERAGE = 5
POSITION_SIZE = MARGIN * LEVERAGE
MAX_POSITIONS = 2

# Continuous service: one full scan every 30 seconds.
SCAN_INTERVAL = 30
ERROR_SLEEP = 10

EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
RSI_LEN = 14
ATR_LEN = 14
SL_MULT = 1.5

RR1, RR2, RR3 = 1.0, 2.0, 3.0
PIVOT_LEN = 5
NEAR_ATR = 0.5
VOL_LEN = 20
VOL_MULT = 1.2

TP1_CLOSE_PERCENT = 0.25
TP2_CLOSE_PERCENT = 0.25
TIMEFRAMES = ["1m", "3m", "5m", "15m", "1h"]

# Railway Volume: mount at /data and set DATA_DIR=/data.
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "futures_demo_state_v3.json"
TRADES_FILE = DATA_DIR / "futures_demo_trades_v3.csv"

session = requests.Session()
session.headers.update({
    "X-MBX-APIKEY": API_KEY,
    "User-Agent": "FuturesDemoBot/3.0-Railway"
})
retry = Retry(
    total=3, connect=3, read=3, backoff_factor=0.7,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=frozenset(["GET"])
)
adapter = HTTPAdapter(max_retries=retry)
session.mount("https://", adapter)

api_errors = 0
symbol_rules = {}


def now():
    return datetime.now(timezone.utc).isoformat()


def save_state():
    temp = STATE_FILE.with_suffix(".tmp")
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, STATE_FILE)


def load_state():
    if not STATE_FILE.exists():
        return {"version": "3.0", "positions": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("version") != "3.0" or not isinstance(data.get("positions"), dict):
        raise SystemExit("State file version/format mismatch. Safety stop.")
    return data


state = load_state()


def ensure_csv():
    if not TRADES_FILE.exists():
        with open(TRADES_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                "time", "symbol", "side", "entry", "quantity",
                "sl", "tp1", "tp2", "tp3", "exit_reason"
            ])


ensure_csv()


# ==========================================================
# API HELPERS
# ==========================================================

def public_request(method, endpoint, params=None):
    global api_errors
    response = session.request(
        method, BASE_URL + endpoint, params=params or {}, timeout=20
    )
    if response.status_code >= 400:
        api_errors += 1
        raise RuntimeError(
            f"API ERROR {response.status_code}: {response.text[:500]}"
        )
    return response.json()


def signed_request(method, endpoint, params=None):
    global api_errors
    p = dict(params or {})
    p["recvWindow"] = 10000
    p["timestamp"] = int(time.time() * 1000)
    query = urlencode(p)
    signature = hmac.new(
        API_SECRET.encode(), query.encode(), hashlib.sha256
    ).hexdigest()
    url = f"{BASE_URL}{endpoint}?{query}&signature={signature}"
    response = session.request(method, url, timeout=20)
    if response.status_code >= 400:
        api_errors += 1
        raise RuntimeError(
            f"SIGNED API ERROR {response.status_code}: {response.text[:700]}"
        )
    return response.json()


def get_candles(symbol, interval):
    data = public_request(
        "GET", "/fapi/v1/klines",
        {"symbol": symbol, "interval": interval, "limit": 250}
    )
    current_ms = int(time.time() * 1000)
    return [
        {
            "time": int(x[0]), "open": float(x[1]),
            "high": float(x[2]), "low": float(x[3]),
            "close": float(x[4]), "volume": float(x[5])
        }
        for x in data if int(x[6]) < current_ms
    ]


def load_exchange_rules():
    info = public_request("GET", "/fapi/v1/exchangeInfo")
    for item in info["symbols"]:
        symbol = item["symbol"]
        if symbol not in SYMBOLS:
            continue
        filters = {x["filterType"]: x for x in item["filters"]}
        market_filter = filters.get("MARKET_LOT_SIZE", filters["LOT_SIZE"])
        symbol_rules[symbol] = {
            "tick": filters["PRICE_FILTER"]["tickSize"],
            "step": filters["LOT_SIZE"]["stepSize"],
            "min_qty": filters["LOT_SIZE"]["minQty"],
            "market_step": market_filter["stepSize"],
            "market_min": market_filter["minQty"],
        }
    missing = [s for s in SYMBOLS if s not in symbol_rules]
    if missing:
        raise RuntimeError(f"Missing exchange rules for: {missing}")


def floor_step(value, step):
    step_d = Decimal(str(step))
    value_d = Decimal(str(value))
    return float(
        (value_d / step_d).to_integral_value(rounding=ROUND_DOWN) * step_d
    )


def format_step(value, step):
    decimals = max(0, -Decimal(str(step)).as_tuple().exponent)
    return f"{value:.{decimals}f}"


def quantity_for_order(symbol, qty):
    rule = symbol_rules[symbol]
    step = rule["market_step"]
    qty = floor_step(qty, step)
    if qty < float(rule["market_min"]):
        raise ValueError(f"{symbol}: quantity below market minimum")
    return format_step(qty, step)


def price_for_order(symbol, price):
    tick = symbol_rules[symbol]["tick"]
    return format_step(floor_step(price, tick), tick)


# ==========================================================
# ACCOUNT / POSITION HELPERS
# ==========================================================

def account_info():
    return signed_request("GET", "/fapi/v2/account")


def get_exchange_positions():
    data = signed_request("GET", "/fapi/v2/positionRisk")
    return {
        x["symbol"]: x for x in data
        if abs(float(x["positionAmt"])) > 0
    }


def get_position_amount(symbol):
    pos = get_exchange_positions().get(symbol)
    return float(pos["positionAmt"]) if pos else 0.0


def setup_account():
    # Request one-way mode. Existing hedge-mode positions must be resolved manually.
    try:
        signed_request(
            "POST", "/fapi/v1/positionSide/dual",
            {"dualSidePosition": "false"}
        )
    except Exception as exc:
        if "-4059" not in str(exc):
            print("Position mode notice:", exc)

    for symbol in SYMBOLS:
        try:
            signed_request(
                "POST", "/fapi/v1/marginType",
                {"symbol": symbol, "marginType": "ISOLATED"}
            )
        except Exception as exc:
            if "-4046" not in str(exc):
                print(symbol, "margin setup warning:", exc)

        signed_request(
            "POST", "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": LEVERAGE}
        )


# ==========================================================
# INDICATORS
# ==========================================================

def ema(values, period):
    if len(values) < period:
        return None
    result = sum(values[:period]) / period
    multiplier = 2 / (period + 1)
    for value in values[period:]:
        result = value * multiplier + result * (1 - multiplier)
    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100 if avg_gain > 0 else 50
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def ema_series(data, period):
    result = []
    current = None
    multiplier = 2 / (period + 1)
    for i, value in enumerate(data):
        if i == period - 1:
            current = sum(data[:period]) / period
        elif i >= period:
            current = value * multiplier + current * (1 - multiplier)
        result.append(current)
    return result


def macd(values):
    if len(values) < 40:
        return None, None
    e12 = ema_series(values, 12)
    e26 = ema_series(values, 26)
    line = [
        a - b for a, b in zip(e12, e26)
        if a is not None and b is not None
    ]
    signal = ema_series(line, 9)
    return line[-1], signal[-1]


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    tr = []
    for i in range(1, len(candles)):
        current, previous = candles[i], candles[i - 1]
        tr.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        ))
    result = sum(tr[:period]) / period
    for value in tr[period:]:
        result = (result * (period - 1) + value) / period
    return result


def volume_average(candles, period=20):
    if len(candles) < period:
        return None
    return sum(x["volume"] for x in candles[-period:]) / period


def get_pivots(candles, strength=5):
    support = resistance = None
    for i in range(strength, len(candles) - strength):
        left = candles[i-strength:i]
        right = candles[i+1:i+strength+1]
        high, low = candles[i]["high"], candles[i]["low"]
        if all(high > x["high"] for x in left) and all(
            high > x["high"] for x in right
        ):
            resistance = high
        if all(low < x["low"] for x in left) and all(
            low < x["low"] for x in right
        ):
            support = low
    return support, resistance


# ==========================================================
# STRATEGY ENGINE - ORIGINAL V3.0 CONDITIONS
# ==========================================================

def calculate_signal(candles):
    if len(candles) < 220:
        raise ValueError("Not enough closed candles.")
    closes = [x["close"] for x in candles]
    current, previous = candles[-1], candles[-2]
    price = current["close"]

    e20 = ema(closes, EMA_FAST)
    e50 = ema(closes, EMA_MID)
    e200 = ema(closes, EMA_SLOW)
    rv = rsi(closes, RSI_LEN)
    av = atr(candles, ATR_LEN)
    ml, ms = macd(closes)
    va = volume_average(candles, VOL_LEN)

    if any(x is None for x in (e20, e50, e200, rv, av, ml, ms, va)):
        raise ValueError("Indicator calculation failed.")

    volume_ok = current["volume"] >= va * VOL_MULT
    bull_trend = price > e200 and e20 > e50
    bear_trend = price < e200 and e20 < e50
    bull_momentum = rv > 50 and ml > ms
    bear_momentum = rv < 50 and ml < ms

    support, resistance = get_pivots(candles, PIVOT_LEN)
    near_support = support is not None and abs(price - support) <= av * NEAR_ATR
    near_resistance = (
        resistance is not None and abs(price - resistance) <= av * NEAR_ATR
    )

    support_bounce = (
        support is not None and current["low"] <= support + av * 0.25
        and price > support and price > current["open"]
    )
    resistance_reject = (
        resistance is not None and current["high"] >= resistance - av * 0.25
        and price < resistance and price < current["open"]
    )
    resistance_break = (
        resistance is not None and previous["close"] <= resistance
        and price > resistance and price > current["open"]
    )
    support_break = (
        support is not None and previous["close"] >= support
        and price < support and price < current["open"]
    )

    buy = (
        bull_trend and bull_momentum
        and ((support_bounce and near_support) or resistance_break)
        and volume_ok
    )
    sell = (
        bear_trend and bear_momentum
        and ((resistance_reject and near_resistance) or support_break)
        and volume_ok
    )
    signal = "BUY" if buy else "SELL" if sell else "WAIT"

    return {
        "price": price, "ema20": e20, "ema50": e50, "ema200": e200,
        "rsi": rv, "macd": ml, "macd_signal": ms, "atr": av,
        "volume_ok": volume_ok, "support": support,
        "resistance": resistance, "signal": signal,
        "trend": "BULLISH" if bull_trend else (
            "BEARISH" if bear_trend else "NEUTRAL"
        )
    }


def get_mtf(symbol):
    return {
        tf: calculate_signal(get_candles(symbol, tf))["signal"]
        for tf in TIMEFRAMES
    }


# ==========================================================
# ORDERS
# ==========================================================

def market_order(symbol, side, qty, reduce_only=False):
    params = {
        "symbol": symbol, "side": side, "type": "MARKET",
        "quantity": quantity_for_order(symbol, qty),
        "newOrderRespType": "RESULT"
    }
    if reduce_only:
        params["reduceOnly"] = "true"
    return signed_request("POST", "/fapi/v1/order", params)


def conditional_order(symbol, side, order_type, trigger_price):
    params = {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "triggerPrice": price_for_order(symbol, trigger_price),
        "closePosition": "true",
        "workingType": "MARK_PRICE",
        "clientAlgoId": (
            f"BOT3_{symbol}_{order_type}_{int(time.time())}"
        )
    }
    return signed_request("POST", "/fapi/v1/algoOrder", params)


def cancel_algo(symbol, algo_id):
    if not algo_id:
        return
    try:
        signed_request(
            "DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id}
        )
    except Exception as exc:
        print("Cancel algo warning:", symbol, exc)


def close_all_emergency(symbol, close_side):
    try:
        amount = abs(get_position_amount(symbol))
        if amount > 0:
            result = market_order(symbol, close_side, amount, True)
            print("EMERGENCY CLOSE RESULT:", symbol, result)
    except Exception as exc:
        print("CRITICAL: EMERGENCY CLOSE FAILED:", symbol, exc)


# ==========================================================
# TRADE MANAGEMENT
# ==========================================================

def open_position(symbol, side, signal_price, atr_value):
    if symbol in state["positions"]:
        return

    remote_positions = get_exchange_positions()
    if symbol in remote_positions:
        print("EXCHANGE POSITION ALREADY EXISTS:", symbol, "- no new order")
        return
    if len(remote_positions) >= MAX_POSITIONS:
        print("MAXIMUM EXCHANGE POSITIONS REACHED.")
        return

    account = account_info()
    available = float(account["availableBalance"])
    if available < MARGIN:
        print("INSUFFICIENT DEMO AVAILABLE BALANCE.")
        return

    qty = float(quantity_for_order(symbol, POSITION_SIZE / signal_price))
    order_side = "BUY" if side == "BUY" else "SELL"
    close_side = "SELL" if side == "BUY" else "BUY"

    print(f"\nSENDING DEMO MARKET ENTRY: {symbol} {side}")
    result = market_order(symbol, order_side, qty)
    if result.get("status") != "FILLED":
        raise RuntimeError("Entry not confirmed FILLED: " + str(result))

    executed_qty = float(result.get("executedQty", 0))
    entry = float(result.get("avgPrice", 0))
    if executed_qty <= 0 or entry <= 0:
        raise RuntimeError("Invalid entry fill response.")

    if side == "BUY":
        sl = entry - atr_value * SL_MULT
        risk = entry - sl
        tp1, tp2, tp3 = (
            entry + risk * RR1, entry + risk * RR2, entry + risk * RR3
        )
    else:
        sl = entry + atr_value * SL_MULT
        risk = sl - entry
        tp1, tp2, tp3 = (
            entry - risk * RR1, entry - risk * RR2, entry - risk * RR3
        )

    pos = {
        "side": side, "entry": entry, "quantity": executed_qty,
        "remaining_quantity": executed_qty, "sl": sl,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "tp1_hit": False, "tp2_hit": False,
        "sl_algo_id": None, "tp3_algo_id": None, "entry_time": now()
    }
    state["positions"][symbol] = pos
    save_state()

    try:
        sl_result = conditional_order(symbol, close_side, "STOP_MARKET", sl)
        pos["sl_algo_id"] = sl_result.get("algoId")
        tp_result = conditional_order(
            symbol, close_side, "TAKE_PROFIT_MARKET", tp3
        )
        pos["tp3_algo_id"] = tp_result.get("algoId")
        save_state()
    except Exception as exc:
        print("PROTECTIVE ORDER FAILED:", exc)
        close_all_emergency(symbol, close_side)
        raise RuntimeError(
            "Protection placement failed; emergency close attempted. "
            "Check Demo account positions immediately."
        )

    print("\nDEMO POSITION OPENED")
    print("Symbol:", symbol, "| Side:", side, "| Entry:", entry)
    print("Quantity:", executed_qty, "| Stop Loss:", sl)
    print("TP1:", tp1, "(25% close)")
    print("TP2:", tp2, "(25% close)")
    print("TP3:", tp3, "(exchange close-all order)")


def partial_close(symbol, percent, label):
    pos = state["positions"][symbol]
    amount_signed = get_position_amount(symbol)
    amount = abs(amount_signed)
    if amount <= 0:
        return False

    qty = min(amount, pos["quantity"] * percent)
    close_side = "SELL" if pos["side"] == "BUY" else "BUY"
    qty = float(quantity_for_order(symbol, qty))
    if qty <= 0:
        print(symbol, label, "quantity too small; skipped.")
        return False

    result = market_order(symbol, close_side, qty, True)
    if result.get("status") != "FILLED":
        raise RuntimeError(label + " partial close not confirmed.")
    pos["remaining_quantity"] = max(0.0, amount - qty)
    save_state()
    print(symbol, label, "PARTIAL CLOSE FILLED:", qty)
    return True


def record_closed_trade(symbol, pos, reason):
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            now(), symbol, pos["side"], pos["entry"], pos["quantity"],
            pos["sl"], pos["tp1"], pos["tp2"], pos["tp3"], reason
        ])


def manage_position(symbol, candle):
    if symbol not in state["positions"]:
        return
    pos = state["positions"][symbol]
    amount = abs(get_position_amount(symbol))

    # Exchange-side SL or TP3 may have closed the position.
    if amount <= 0:
        cancel_algo(symbol, pos.get("sl_algo_id"))
        cancel_algo(symbol, pos.get("tp3_algo_id"))
        record_closed_trade(
            symbol, pos, "Closed on exchange; exact exit reason not verified"
        )
        del state["positions"][symbol]
        save_state()
        print(symbol, "POSITION CLOSED ON EXCHANGE.")
        return

    if pos["side"] == "BUY":
        hit1, hit2 = candle["high"] >= pos["tp1"], candle["high"] >= pos["tp2"]
    else:
        hit1, hit2 = candle["low"] <= pos["tp1"], candle["low"] <= pos["tp2"]

    if hit1 and not pos["tp1_hit"]:
        if partial_close(symbol, TP1_CLOSE_PERCENT, "TP1"):
            pos["tp1_hit"] = True
            save_state()

    if hit2 and not pos["tp2_hit"]:
        if partial_close(symbol, TP2_CLOSE_PERCENT, "TP2"):
            pos["tp2_hit"] = True
            save_state()


# ==========================================================
# REPORT / STARTUP SAFETY
# ==========================================================

def report():
    account = account_info()
    remote_positions = get_exchange_positions()
    print("\n========== DEMO ACCOUNT REPORT ==========")
    print("Available Balance:", account["availableBalance"])
    print("Total Wallet Balance:", account["totalWalletBalance"])
    print("Unrealized PNL:", account["totalUnrealizedProfit"])
    print("Exchange Open Positions:", len(remote_positions))
    print("Bot Tracked Positions:", len(state["positions"]))
    print("API Errors:", api_errors)
    for symbol, p in remote_positions.items():
        print(
            symbol, "Position Amount:", p["positionAmt"],
            "Entry:", p["entryPrice"],
            "Unrealized:", p["unRealizedProfit"]
        )
    print("=========================================")


print("=" * 60)
print("BINANCE USDT-M FUTURES DEMO BOT V3.0 - RAILWAY")
print("FIXED DEMO URL:", BASE_URL)
print("LIVE TRADING URL: NOT PRESENT")
print("=" * 60)
print("Margin:", MARGIN, "| Leverage:", LEVERAGE)
print("Position Notional:", POSITION_SIZE, "| Max Positions:", MAX_POSITIONS)
print("Symbols:", ", ".join(SYMBOLS))
print("Data directory:", DATA_DIR.resolve())

try:
    account_info()
    load_exchange_rules()
except Exception as exc:
    raise SystemExit("DEMO API / EXCHANGE INFO CHECK FAILED: " + str(exc))

remote = get_exchange_positions()
unknown = [
    s for s in remote if s in SYMBOLS and s not in state["positions"]
]
if unknown:
    raise SystemExit(
        "SAFETY STOP: Demo exchange positions missing from local state: "
        + str(unknown)
        + ". Restore the Railway Volume/state file or reconcile manually."
    )

setup_account()
print("DEMO API CONNECTED.")
print("One-way mode requested; isolated margin and 5x leverage configured.")
print("Continuous scanning started. Stop service to stop the bot.")

scan_number = 0
while True:
    scan_number += 1
    scan_started = time.time()
    print("\n" + "=" * 58)
    print("DEMO SCAN:", scan_number, "| UTC:", now())
    print("=" * 58)

    for symbol in SYMBOLS:
        try:
            candles = get_candles(symbol, "5m")
            data = calculate_signal(candles)

            if symbol in state["positions"]:
                manage_position(symbol, candles[-1])

            mtf = get_mtf(symbol)

            print("\n", symbol)
            print("Price:", data["price"], "| Trend:", data["trend"])
            print("EMA20/50/200:", data["ema20"], data["ema50"], data["ema200"])
            print("RSI:", data["rsi"], "| MACD:", data["macd"])
            print("MACD Signal:", data["macd_signal"], "| ATR:", data["atr"])
            print("Volume OK:", data["volume_ok"])
            print("Support:", data["support"], "| Resistance:", data["resistance"])
            print("MAIN SIGNAL:", data["signal"], "| MTF:", mtf)

            # Do not reopen a position closed during this same scan.
            if symbol in state["positions"]:
                continue
            if data["signal"] in ("BUY", "SELL"):
                open_position(symbol, data["signal"], data["price"], data["atr"])

        except Exception as exc:
            print(symbol, "ERROR:", str(exc))

    save_state()
    try:
        report()
    except Exception as exc:
        print("REPORT ERROR:", exc)

    elapsed = time.time() - scan_started
    sleep_for = max(1, SCAN_INTERVAL - elapsed)
    print(
        f"\nSCAN {scan_number} COMPLETE. "
        f"Elapsed: {elapsed:.1f}s. Sleeping: {sleep_for:.1f}s"
    )
    time.sleep(sleep_for)
