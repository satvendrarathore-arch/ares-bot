import os
import time
import hmac
import hashlib
import base64
import json
import requests
import logging
from datetime import datetime

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("ARES")

# Config from Environment Variables
API_KEY       = os.environ.get("BITGET_API_KEY", "")
SECRET_KEY    = os.environ.get("BITGET_SECRET_KEY", "")
PASSPHRASE    = os.environ.get("BITGET_PASSPHRASE", "")
TRADE_AMOUNT  = float(os.environ.get("TRADE_AMOUNT_USDT", "20"))
MAX_LEVERAGE  = int(os.environ.get("MAX_LEVERAGE", "3"))
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SECONDS", "300"))

BASE_URL      = "https://api.bitget.com"
PRODUCT_TYPE  = "USDT-FUTURES"
SYMBOLS       = ["BTCUSDT", "ETHUSDT"]

STOP_LOSS_PCT   = 0.015
TAKE_PROFIT_PCT = 0.030
MIN_CONFIDENCE  = 75
MAX_OPEN_TRADES = 2


# BITGET API FUNCTIONS

def sign(secret, message):
    mac = hmac.new(secret.encode(), message.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def get_timestamp():
    return str(int(time.time() * 1000))

def bitget_request(method, path, params=None, body=None):
    ts = get_timestamp()
    if method == "GET" and params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        sign_str = ts + "GET" + path + "?" + qs
        url = BASE_URL + path + "?" + qs
    elif method == "POST":
        body_str = json.dumps(body) if body else ""
        sign_str = ts + "POST" + path + body_str
        url = BASE_URL + path
    else:
        sign_str = ts + method + path
        url = BASE_URL + path

    signature = sign(SECRET_KEY, sign_str)
    headers = {
        "Content-Type": "application/json",
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": PASSPHRASE,
        "locale": "en-US",
    }

    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=10)
        else:
            resp = requests.post(url, headers=headers,
                                 data=json.dumps(body) if body else None, timeout=10)
        return resp.json()
    except Exception as e:
        log.error(f"API request error: {e}")
        return None


def get_ticker(symbol):
    resp = requests.get(
        f"{BASE_URL}/api/v2/mix/market/ticker",
        params={"symbol": symbol, "productType": PRODUCT_TYPE},
        timeout=10
    )
    data = resp.json()
    return data.get("data", [{}])[0] if data.get("data") else None

def get_candles(symbol, granularity="60", limit="60"):
    resp = requests.get(
        f"{BASE_URL}/api/v2/mix/market/candles",
        params={"symbol": symbol, "productType": PRODUCT_TYPE,
                "granularity": granularity, "limit": limit},
        timeout=10
    )
    data = resp.json()
    return data.get("data", [])

def get_funding_rate(symbol):
    resp = requests.get(
        f"{BASE_URL}/api/v2/mix/market/current-fund-rate",
        params={"symbol": symbol, "productType": PRODUCT_TYPE},
        timeout=10
    )
    data = resp.json()
    items = data.get("data", [])
    return float(items[0].get("fundingRate", 0)) if items else 0.0

def get_balance():
    res = bitget_request("GET", "/api/v2/mix/account/account", params={
        "symbol": "BTCUSDT",
        "productType": PRODUCT_TYPE,
        "marginCoin": "USDT"
    })
    if res and res.get("data"):
        return float(res["data"].get("available", 0))
    return 0.0

def get_open_positions():
    res = bitget_request("GET", "/api/v2/mix/position/all-position", params={
        "productType": PRODUCT_TYPE,
        "marginCoin": "USDT"
    })
    if res and res.get("data"):
        return [p for p in res["data"] if float(p.get("total", 0)) > 0]
    return []

def set_leverage(symbol, leverage):
    for side in ["long", "short"]:
        bitget_request("POST", "/api/v2/mix/account/set-leverage", body={
            "symbol": symbol,
            "productType": PRODUCT_TYPE,
            "marginCoin": "USDT",
            "leverage": str(leverage),
            "holdSide": side
        })

def place_order(symbol, side, trade_side, size, leverage):
    set_leverage(symbol, leverage)
    res = bitget_request("POST", "/api/v2/mix/order/place-order", body={
        "symbol": symbol,
        "productType": PRODUCT_TYPE,
        "marginMode": "isolated",
        "marginCoin": "USDT",
        "size": str(size),
        "side": side,
        "tradeSide": trade_side,
        "orderType": "market",
        "force": "gtc",
    })
    return res

def close_position(symbol, hold_side, size):
    side = "sell" if hold_side == "long" else "buy"
    return place_order(symbol, side, "close", size, 1)

def set_sl_tp(symbol, hold_side, sl_price, tp_price):
    for plan, price in [("loss_plan", sl_price), ("profit_plan", tp_price)]:
        bitget_request("POST", "/api/v2/mix/order/place-tpsl-order", body={
            "symbol": symbol,
            "productType": PRODUCT_TYPE,
            "marginCoin": "USDT",
            "planType": plan,
            "triggerPrice": str(round(price, 2)),
            "triggerType": "mark_price",
            "executePrice": "0",
            "holdSide": hold_side,
            "size": "0",
        })


# TECHNICAL ANALYSIS

def calculate_indicators(closes):
    if len(closes) < 26:
        return None

    def ema(data, period):
        k = 2 / (period + 1)
        result = [sum(data[:period]) / period]
        for price in data[period:]:
            result.append(price * k + result[-1] * (1 - k))
        return result

    ema9  = ema(closes, 9)[-1]
    ema21 = ema(closes, 21)[-1]
    ema50 = ema(closes, min(50, len(closes)))[-1]

    diffs = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in diffs[-14:]]
    losses = [abs(min(d, 0)) for d in diffs[-14:]]
    avg_gain = sum(gains) / 14
    avg_loss = sum(losses) / 14
    rsi = 100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss != 0 else 100

    atr = sum(abs(closes[i] - closes[i-1]) for i in range(-14, 0)) / 14

    ema12 = ema(closes, 12)[-1]
    ema26 = ema(closes, min(26, len(closes)))[-1]
    macd = ema12 - ema26

    return {
        "ema9": ema9, "ema21": ema21, "ema50": ema50,
        "rsi": rsi, "atr": atr, "macd": macd,
    }

def generate_signal(symbol, ticker, candles, funding_rate):
    if not candles or len(candles) < 26:
        return None

    closes  = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    price   = float(ticker.get("lastPr", closes[-1]))
    high24  = float(ticker.get("high24h", price))
    low24   = float(ticker.get("low24h", price))
    change  = float(ticker.get("change24h", 0)) * 100

    ind = calculate_indicators(closes)
    if not ind:
        return None

    rsi   = ind["rsi"]
    ema9  = ind["ema9"]
    ema21 = ind["ema21"]
    ema50 = ind["ema50"]
    macd  = ind["macd"]
    atr   = ind["atr"]

    vol_avg  = sum(volumes[-10:]) / 10
    vol_surge = volumes[-1] > vol_avg * 1.5

    bull = 0
    bear = 0
    reasons = []

    if ema9 > ema21 > ema50:
        bull += 3
        reasons.append("EMA uptrend confirmed")
    elif ema9 < ema21 < ema50:
        bear += 3
        reasons.append("EMA downtrend confirmed")

    if price > ema21:
        bull += 1
    else:
        bear += 1

    if rsi < 35:
        bull += 2
        reasons.append(f"RSI oversold ({rsi:.1f})")
    elif rsi > 65:
        bear += 2
        reasons.append(f"RSI overbought ({rsi:.1f})")
    elif 45 < rsi < 60:
        bull += 1

    if macd > 0:
        bull += 1
        reasons.append("MACD bullish")
    else:
        bear += 1
        reasons.append("MACD bearish")

    if change > 1.5:
        bull += 1
    elif change < -1.5:
        bear += 1

    if vol_surge and change > 0:
        bull += 1
        reasons.append("Volume surge bullish")
    elif vol_surge and change < 0:
        bear += 1
        reasons.append("Volume surge bearish")

    if funding_rate > 0.001:
        bear += 1
        reasons.append("High funding rate")
    elif funding_rate < -0.001:
        bull += 1
        reasons.append("Negative funding rate")

    range24 = high24 - low24
    if range24 > 0:
        pos = (price - low24) / range24
        if pos < 0.2:
            bull += 1
            reasons.append("Near 24h low support")
        elif pos > 0.8:
            bear += 1
            reasons.append("Near 24h high resistance")

    total = bull + bear
    if total == 0:
        return None

    if bull >= 7 and bull > bear * 1.5:
        action = "LONG"
        confidence = min(95, int((bull / total) * 100))
    elif bear >= 7 and bear > bull * 1.5:
        action = "SHORT"
        confidence = min(95, int((bear / total) * 100))
    else:
        action = "HOLD"
        confidence = 50

    vol_pct = (atr / price) * 100
    leverage = 1 if vol_pct > 3 else 2 if vol_pct > 1.5 else min(MAX_LEVERAGE, 3)

    if action == "LONG":
        sl = price * (1 - STOP_LOSS_PCT)
        tp = price * (1 + TAKE_PROFIT_PCT)
        side = "buy"
        hold_side = "long"
    elif action == "SHORT":
        sl = price * (1 + STOP_LOSS_PCT)
        tp = price * (1 - TAKE_PROFIT_PCT)
        side = "sell"
        hold_side = "short"
    else:
        sl = tp = price
        side = hold_side = "none"

    return {
        "symbol": symbol,
        "asset": symbol.replace("USDT", ""),
        "action": action,
        "side": side,
        "hold_side": hold_side,
        "confidence": confidence,
        "price": price,
        "sl": sl,
        "tp": tp,
        "leverage": leverage,
        "rr_ratio": f"1:{round(TAKE_PROFIT_PCT/STOP_LOSS_PCT,1)}",
        "reasons": reasons,
        "rsi": rsi,
        "ema_trend": "UP" if ema9 > ema21 else "DOWN",
    }


# POSITION MONITOR
active_trades = {}

def monitor_positions(positions):
    for pos in positions:
        sym = pos.get("symbol")
        if sym not in active_trades:
            continue
        trade = active_trades[sym]
        mark_price = float(pos.get("markPrice", trade["entry"]))
        hold_side  = pos.get("holdSide", trade["side"])
        size       = float(pos.get("total", 0))
        should_close = False
        reason = ""

        if hold_side == "long":
            if mark_price <= trade["sl"]:
                should_close = True
                reason = f"STOP LOSS @ ${mark_price:.2f}"
            elif mark_price >= trade["tp"]:
                should_close = True
                reason = f"TAKE PROFIT @ ${mark_price:.2f}"
        elif hold_side == "short":
            if mark_price >= trade["sl"]:
                should_close = True
                reason = f"STOP LOSS @ ${mark_price:.2f}"
            elif mark_price <= trade["tp"]:
                should_close = True
                reason = f"TAKE PROFIT @ ${mark_price:.2f}"

        if should_close and size > 0:
            log.info(f"[CLOSE] {sym} — {reason}")
            res = close_position(sym, hold_side, size)
            if res and res.get("code") == "00000":
                pnl = (mark_price - trade["entry"]) * size if hold_side == "long" else (trade["entry"] - mark_price) * size
                log.info(f"[CLOSED] ✅ {sym} | PnL: ${pnl:.4f} USDT")
            del active_trades[sym]


# MAIN BOT LOOP

def run_bot():
    log.info("=" * 55)
    log.info("  ARES FUTURES BOT — Bitget USDT-M Perpetuals")
    log.info(f"  Symbols : {', '.join(SYMBOLS)}")
    log.info(f"  Margin  : ${TRADE_AMOUNT} USDT | Max Leverage: {MAX_LEVERAGE}x")
    log.info(f"  SL: {STOP_LOSS_PCT*100}% | TP: {TAKE_PROFIT_PCT*100}%")
    log.info(f"  Scan    : every {SCAN_INTERVAL//60} minutes")
    log.info("=" * 55)

    if not API_KEY or not SECRET_KEY or not PASSPHRASE:
        log.error("API keys not set! Add them in Railway Variables.")
        return

    cycle = 0
    while True:
        cycle += 1
        log.info(f"\n--- CYCLE {cycle} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")

        balance = get_balance()
        log.info(f"[BALANCE] ${balance:.2f} USDT available")

        if balance < 10:
            log.warning("[SKIP] Balance too low. Waiting...")
            time.sleep(SCAN_INTERVAL)
            continue

        open_positions = get_open_positions()
        log.info(f"[POSITIONS] {len(open_positions)} open")

        if open_positions:
            monitor_positions(open_positions)

        open_syms = [p.get("symbol") for p in open_positions]

        if len(open_positions) >= MAX_OPEN_TRADES:
            log.info("[SKIP] Max trades open. Monitoring only.")
            time.sleep(SCAN_INTERVAL)
            continue

        for symbol in SYMBOLS:
            if symbol in open_syms:
                log.info(f"[{symbol}] Position already open. Skipping.")
                continue

            log.info(f"[SCAN] {symbol}...")

            try:
                ticker  = get_ticker(symbol)
                candles = get_candles(symbol, "60", "60")
                funding = get_funding_rate(symbol)
            except Exception as e:
                log.error(f"[{symbol}] Data error: {e}")
                continue

            if not ticker or not candles:
                log.warning(f"[{symbol}] No data")
                continue

            price  = float(ticker.get("lastPr", 0))
            change = float(ticker.get("change24h", 0)) * 100
            log.info(f"[{symbol}] ${price:,.2f} | 24h: {change:+.2f}% | Funding: {funding*100:.4f}%")

            signal = generate_signal(symbol, ticker, candles, funding)

            if not signal:
                log.info(f"[{symbol}] Not enough data")
                continue

            log.info(f"[SIGNAL] {signal['action']} | Conf: {signal['confidence']}% | "
                     f"RSI: {signal['rsi']:.1f} | EMA: {signal['ema_trend']} | "
                     f"Lev: {signal['leverage']}x | R:R {signal['rr_ratio']}")
            log.info(f"[REASON] {' | '.join(signal['reasons'][:3])}")

            if signal["action"] in ("LONG", "SHORT") and signal["confidence"] >= MIN_CONFIDENCE:
                size = round((TRADE_AMOUNT * signal["leverage"]) / price, 4)
                if size <= 0:
                    continue

                log.info(f"[ORDER] {signal['action']} {size} {signal['asset']} "
                         f"@ {signal['leverage']}x | SL:${signal['sl']:,.2f} TP:${signal['tp']:,.2f}")

                res = place_order(symbol, signal["side"], "open", size, signal["leverage"])

                if res and res.get("code") == "00000":
                    oid = res.get("data", {}).get("orderId", "N/A")
                    log.info(f"[ORDER] ✅ {signal['action']} opened! ID: {oid}")
                    time.sleep(1)
                    set_sl_tp(symbol, signal["hold_side"], signal["sl"], signal["tp"])
                    log.info(f"[RISK] SL & TP set on exchange ✅")
                    active_trades[symbol] = {
                        "side": signal["hold_side"],
                        "entry": price,
                        "sl": signal["sl"],
                        "tp": signal["tp"],
                        "size": size,
                    }
                else:
                    log.error(f"[ORDER] Failed: {res.get('msg') if res else 'No response'}")
            else:
                log.info(f"[{symbol}] HOLD — confidence {signal['confidence']}% or market unclear")

            time.sleep(2)

        log.info(f"[SLEEP] Next scan in {SCAN_INTERVAL//60} min...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    run_bot()
