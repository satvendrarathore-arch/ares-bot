import os, time, hmac, hashlib, base64, json, requests, logging
from datetime import datetime
from collections import deque

# ═══════════════════════════════════════════════════════════════
#   ARES ULTRA v4.1 — World #1 Futures Bot — BALANCE FIXED
#   Bitget USDT-M Perpetuals | BTC + ETH
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("ARES")

# ── Config ────────────────────────────────────────────────────
API_KEY       = os.environ.get("BITGET_API_KEY", "")
SECRET_KEY    = os.environ.get("BITGET_SECRET_KEY", "")
PASSPHRASE    = os.environ.get("BITGET_PASSPHRASE", "")
MAX_LEVERAGE  = int(os.environ.get("MAX_LEVERAGE", "10"))
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SECONDS", "120"))
COMPOUND_PCT  = float(os.environ.get("COMPOUND_PCT", "20")) / 100
MAX_TRADES    = int(os.environ.get("MAX_TRADES", "2"))

BASE_URL      = "https://api.bitget.com"
PRODUCT_TYPE  = "USDT-FUTURES"
SYMBOLS       = ["BTCUSDT", "ETHUSDT"]

# ── Risk ──────────────────────────────────────────────────────
SL_MULT       = 1.5
TP1_MULT      = 2.0
TP2_MULT      = 3.5
TP3_MULT      = 6.0
TRAIL_MULT    = 1.0
MIN_CONF      = 70
MAX_DAILY_LOSS= 0.06
MAX_DRAWDOWN  = 0.12

# ── Session ───────────────────────────────────────────────────
S = {
    "start_bal": 0,
    "peak_bal": 0,
    "daily_pnl": 0,
    "total_pnl": 0,
    "wins": 0,
    "losses": 0,
    "loss_streak": 0,
    "win_streak": 0,
    "trades": {},
    "daily_start": datetime.now().date(),
    "history": deque(maxlen=50),
}


# ══════════════════════════════════════════════════════════════
#  API LAYER
# ══════════════════════════════════════════════════════════════

def _sign(secret, msg):
    return base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()

def call(method, path, params=None, body=None):
    t = str(int(time.time() * 1000))
    headers = {
        "Content-Type": "application/json",
        "ACCESS-KEY": API_KEY,
        "ACCESS-TIMESTAMP": t,
        "ACCESS-PASSPHRASE": PASSPHRASE,
        "locale": "en-US",
    }
    try:
        if method == "GET" and params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            headers["ACCESS-SIGN"] = _sign(SECRET_KEY, t + "GET" + path + "?" + qs)
            r = requests.get(BASE_URL + path + "?" + qs, headers=headers, timeout=12)
        elif method == "POST":
            bs = json.dumps(body) if body else ""
            headers["ACCESS-SIGN"] = _sign(SECRET_KEY, t + "POST" + path + bs)
            r = requests.post(BASE_URL + path, headers=headers, data=bs, timeout=12)
        else:
            headers["ACCESS-SIGN"] = _sign(SECRET_KEY, t + "GET" + path)
            r = requests.get(BASE_URL + path, headers=headers, timeout=12)
        return r.json()
    except Exception as e:
        log.error(f"API error [{method}] {path}: {e}")
        return None


# ── Market Data (Public) ──────────────────────────────────────

def pub_ticker(sym):
    try:
        r = requests.get(f"{BASE_URL}/api/v2/mix/market/ticker",
            params={"symbol": sym, "productType": PRODUCT_TYPE}, timeout=10)
        d = r.json().get("data", [])
        return d[0] if d else None
    except Exception as e:
        log.error(f"Ticker error: {e}")
        return None

def pub_candles(sym, gran="15m", limit=150):
    """
    FIXED: Bitget v2 requires granularity with unit suffix
    5 → 5m, 15 → 15m, 60 → 1H, 240 → 4H
    """
    # Convert old format to new format
    gran_map = {"5": "5m", "15": "15m", "60": "1H", "240": "4H",
                "1": "1m", "30": "30m", "120": "2H", "360": "6H"}
    gran = gran_map.get(str(gran), gran)
    
    try:
        r = requests.get(f"{BASE_URL}/api/v2/mix/market/candles",
            params={"symbol": sym, "productType": PRODUCT_TYPE,
                    "granularity": gran, "limit": str(limit)}, timeout=10)
        data = r.json()
        if data.get("code") == "00000":
            return data.get("data", [])
        else:
            log.warning(f"[CANDLES] {sym} {gran}: {data.get('msg', 'Unknown error')}")
            return []
    except Exception as e:
        log.error(f"Candles error {sym}: {e}")
        return []

def pub_funding(sym):
    try:
        r = requests.get(f"{BASE_URL}/api/v2/mix/market/current-fund-rate",
            params={"symbol": sym, "productType": PRODUCT_TYPE}, timeout=10)
        d = r.json().get("data", [])
        return float(d[0].get("fundingRate", 0)) if d else 0.0
    except:
        return 0.0


# ── Account (Authenticated) ───────────────────────────────────

def fetch_balance():
    """
    FIXED: Uses correct Bitget v2 futures balance endpoint
    Tries multiple endpoints for reliability
    """
    # Method 1: accounts list endpoint
    try:
        res = call("GET", "/api/v2/mix/account/accounts",
                   params={"productType": PRODUCT_TYPE})
        if res and res.get("code") == "00000" and res.get("data"):
            for item in res["data"]:
                if item.get("marginCoin", "").upper() == "USDT":
                    bal = float(item.get("available", 0))
                    log.info(f"[API] Balance via accounts endpoint: ${bal:.2f}")
                    return bal
    except Exception as e:
        log.warning(f"Method 1 failed: {e}")

    # Method 2: single account endpoint
    try:
        res = call("GET", "/api/v2/mix/account/account",
                   params={"symbol": "BTCUSDT",
                           "productType": PRODUCT_TYPE,
                           "marginCoin": "USDT"})
        if res and res.get("code") == "00000" and res.get("data"):
            bal = float(res["data"].get("available", 0))
            log.info(f"[API] Balance via account endpoint: ${bal:.2f}")
            return bal
    except Exception as e:
        log.warning(f"Method 2 failed: {e}")

    # Method 3: wallet assets
    try:
        res = call("GET", "/api/v2/mix/account/account",
                   params={"symbol": "ETHUSDT",
                           "productType": PRODUCT_TYPE,
                           "marginCoin": "USDT"})
        if res and res.get("code") == "00000" and res.get("data"):
            bal = float(res["data"].get("available", 0))
            log.info(f"[API] Balance via ETH endpoint: ${bal:.2f}")
            return bal
    except Exception as e:
        log.warning(f"Method 3 failed: {e}")

    log.error("[API] All balance methods failed — check API keys!")
    return 0.0

def fetch_positions():
    try:
        res = call("GET", "/api/v2/mix/position/all-position",
                   params={"productType": PRODUCT_TYPE, "marginCoin": "USDT"})
        if res and res.get("data"):
            return [p for p in res["data"] if float(p.get("total", 0)) > 0]
    except Exception as e:
        log.error(f"Positions error: {e}")
    return []

def set_leverage(sym, lev):
    for side in ["long", "short"]:
        call("POST", "/api/v2/mix/account/set-leverage", body={
            "symbol": sym, "productType": PRODUCT_TYPE,
            "marginCoin": "USDT", "leverage": str(lev), "holdSide": side
        })

def open_order(sym, side, size, lev):
    set_leverage(sym, lev)
    return call("POST", "/api/v2/mix/order/place-order", body={
        "symbol": sym, "productType": PRODUCT_TYPE,
        "marginMode": "isolated", "marginCoin": "USDT",
        "size": str(size), "side": side,
        "tradeSide": "open", "orderType": "market", "force": "gtc"
    })

def close_order(sym, hold_side, size):
    side = "sell" if hold_side == "long" else "buy"
    return call("POST", "/api/v2/mix/order/place-order", body={
        "symbol": sym, "productType": PRODUCT_TYPE,
        "marginMode": "isolated", "marginCoin": "USDT",
        "size": str(size), "side": side,
        "tradeSide": "close", "orderType": "market", "force": "gtc"
    })

def set_sltp(sym, hold_side, sl, tp):
    for plan, px in [("loss_plan", sl), ("profit_plan", tp)]:
        call("POST", "/api/v2/mix/order/place-tpsl-order", body={
            "symbol": sym, "productType": PRODUCT_TYPE,
            "marginCoin": "USDT", "planType": plan,
            "triggerPrice": str(round(px, 2)),
            "triggerType": "mark_price",
            "executePrice": "0", "holdSide": hold_side, "size": "0"
        })


# ══════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════

def ema(data, p):
    if len(data) < p:
        return data[-1] if data else 0
    k = 2 / (p + 1)
    v = sum(data[:p]) / p
    for x in data[p:]:
        v = x * k + v * (1 - k)
    return v

def ema_series(data, p):
    if len(data) < p:
        return [data[-1]] * len(data) if data else [0]
    k = 2 / (p + 1)
    out = [sum(data[:p]) / p]
    for x in data[p:]:
        out.append(x * k + out[-1] * (1 - k))
    return out

def calc_rsi(closes, p=14):
    if len(closes) < p + 1:
        return 50
    diffs = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    g = sum(max(d, 0) for d in diffs[-p:]) / p
    l = sum(abs(min(d, 0)) for d in diffs[-p:]) / p
    return 100 - (100 / (1 + g / l)) if l else 100

def calc_macd(closes):
    if len(closes) < 35:
        return 0, 0, 0
    e12 = ema_series(closes, 12)
    e26 = ema_series(closes, 26)
    ml = [e12[i] - e26[i] for i in range(min(len(e12), len(e26)))]
    if len(ml) < 9:
        return ml[-1], ml[-1], 0
    sl = ema_series(ml, 9)[-1]
    return ml[-1], sl, ml[-1] - sl

def calc_bb(closes, p=20):
    if len(closes) < p:
        c = closes[-1]
        return c * 1.02, c, c * 0.98
    r = closes[-p:]
    mid = sum(r) / p
    std = (sum((x - mid)**2 for x in r) / p) ** 0.5
    return mid + 2*std, mid, mid - 2*std

def calc_atr(highs, lows, closes, p=14):
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, len(closes))]
    return sum(trs[-p:]) / min(len(trs), p) if trs else closes[-1] * 0.01

def calc_stoch(highs, lows, closes, k=14):
    h, l = max(highs[-k:]), min(lows[-k:])
    return ((closes[-1] - l) / (h - l)) * 100 if h != l else 50

def calc_vwap(highs, lows, closes, vols):
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(closes))]
    tv = sum(t * v for t, v in zip(tp, vols))
    sv = sum(vols)
    return tv / sv if sv else closes[-1]

def mtf_analysis(symbol):
    bull, bear = 0, 0
    results = {}
    for gran, label, w in [("5","5m",1),("15","15m",2),("60","1H",3),("240","4H",4)]:
        try:
            c = pub_candles(symbol, gran, 80)
            if len(c) < 30:
                continue
            cls = [float(x[4]) for x in c]
            hhs = [float(x[2]) for x in c]
            lls = [float(x[3]) for x in c]
            e9, e21 = ema(cls, 9), ema(cls, 21)
            r = calc_rsi(cls)
            _, _, mh = calc_macd(cls)
            bbu, _, bbl = calc_bb(cls)
            sk = calc_stoch(hhs, lls, cls)
            tb, tr = 0, 0
            if e9 > e21: tb += 2
            else: tr += 2
            if r < 35: tb += 2
            elif r > 65: tr += 2
            elif r > 50: tb += 1
            else: tr += 1
            if mh > 0: tb += 1
            else: tr += 1
            if cls[-1] <= bbl: tb += 1
            elif cls[-1] >= bbu: tr += 1
            if sk < 25: tb += 1
            elif sk > 75: tr += 1
            bull += tb * w
            bear += tr * w
            results[label] = "🟢" if tb > tr else "🔴" if tr > tb else "⚪"
        except:
            results[label] = "⚪"
    return bull, bear, results


# ══════════════════════════════════════════════════════════════
#  COMPOUNDING ENGINE
# ══════════════════════════════════════════════════════════════

def compound_size(bal, price, lev, conf):
    margin = bal * COMPOUND_PCT

    # Loss streak protection
    if S["loss_streak"] >= 3:
        margin *= 0.5
        log.info(f"[COMPOUND] ⚠️ Loss streak {S['loss_streak']} — size 50%")
    elif S["loss_streak"] == 2:
        margin *= 0.7

    # Win streak boost
    if S["win_streak"] >= 3:
        margin *= 1.15
    elif S["win_streak"] >= 2:
        margin *= 1.08

    # Confidence boost
    if conf >= 90: margin *= 1.2
    elif conf >= 85: margin *= 1.1
    elif conf >= 80: margin *= 1.05

    # Drawdown protection
    if S["peak_bal"] > 0:
        dd = (S["peak_bal"] - bal) / S["peak_bal"]
        if dd > MAX_DRAWDOWN:
            margin *= 0.4
            log.warning(f"[COMPOUND] ⚠️ Drawdown {dd*100:.1f}% — size 40%")
        elif dd > 0.06:
            margin *= 0.7

    margin = min(margin, bal * 0.40)
    margin = max(margin, 5.0)
    size = round((margin * lev) / price, 4)
    return size, round(margin, 2)


# ══════════════════════════════════════════════════════════════
#  SIGNAL ENGINE
# ══════════════════════════════════════════════════════════════

def generate_signal(sym, tick, cdata, fund):
    if not cdata or len(cdata) < 60:
        return None

    highs  = [float(x[2]) for x in cdata]
    lows   = [float(x[3]) for x in cdata]
    closes = [float(x[4]) for x in cdata]
    vols   = [float(x[5]) for x in cdata]

    price  = float(tick.get("lastPr", closes[-1]))
    h24    = float(tick.get("high24h", price))
    l24    = float(tick.get("low24h", price))
    chg    = float(tick.get("change24h", 0)) * 100

    e9   = ema(closes, 9)
    e21  = ema(closes, 21)
    e50  = ema(closes, 50)
    e100 = ema(closes, 100)
    e200 = ema(closes, min(200, len(closes)))
    r_now  = calc_rsi(closes)
    r_prev = calc_rsi(closes[:-3])
    ml, ms, mh = calc_macd(closes)
    bbu, bbm, bbl = calc_bb(closes)
    atr_v  = calc_atr(highs, lows, closes)
    sk     = calc_stoch(highs, lows, closes)
    vwap_v = calc_vwap(highs, lows, closes, vols)
    vol_avg = sum(vols[-20:]) / 20 if len(vols) >= 20 else vols[-1]
    vol_r   = vols[-1] / vol_avg if vol_avg > 0 else 1
    volat   = (atr_v / price) * 100
    mtf_b, mtf_br, tf_map = mtf_analysis(sym)

    # Skip extreme volatility
    if volat > 8:
        log.warning(f"[{sym}] Volatility {volat:.1f}% extreme — skip")
        return None

    bull, bear = 0, 0
    reasons = []

    # 1. EMA Stack
    if e9 > e21 > e50 > e100:
        bull += 5; reasons.append("✅ Full EMA stack bullish")
    elif e9 > e21 > e50:
        bull += 4; reasons.append("✅ EMA uptrend 9>21>50")
    elif e9 > e21:
        bull += 2; reasons.append("✅ EMA bullish 9>21")
    elif e9 < e21 < e50 < e100:
        bear += 5; reasons.append("🔴 Full EMA stack bearish")
    elif e9 < e21 < e50:
        bear += 4; reasons.append("🔴 EMA downtrend 9<21<50")
    elif e9 < e21:
        bear += 2; reasons.append("🔴 EMA bearish 9<21")

    # 2. EMA200
    if price > e200: bull += 3; reasons.append("✅ Above EMA200")
    else: bear += 3; reasons.append("🔴 Below EMA200")

    # 3. RSI
    rsi_up = r_now > r_prev
    if r_now < 25: bull += 4; reasons.append(f"✅ RSI extreme oversold {r_now:.0f}")
    elif r_now < 35: bull += 3; reasons.append(f"✅ RSI oversold {r_now:.0f}")
    elif r_now < 45 and rsi_up: bull += 2; reasons.append(f"✅ RSI recovering {r_now:.0f}")
    elif r_now > 75: bear += 4; reasons.append(f"🔴 RSI extreme overbought {r_now:.0f}")
    elif r_now > 65: bear += 3; reasons.append(f"🔴 RSI overbought {r_now:.0f}")
    elif r_now > 55 and not rsi_up: bear += 2; reasons.append(f"🔴 RSI weakening {r_now:.0f}")
    elif 48 < r_now < 58: bull += 1

    # 4. MACD
    if mh > 0 and ml > ms and ml > 0: bull += 3; reasons.append("✅ MACD bullish above zero")
    elif mh > 0 and ml > ms: bull += 2; reasons.append("✅ MACD bullish cross")
    elif mh > 0: bull += 1
    elif mh < 0 and ml < ms and ml < 0: bear += 3; reasons.append("🔴 MACD bearish below zero")
    elif mh < 0 and ml < ms: bear += 2; reasons.append("🔴 MACD bearish cross")
    elif mh < 0: bear += 1

    # 5. Bollinger Bands
    bb_range = bbu - bbl
    bb_pct = (price - bbl) / bb_range if bb_range > 0 else 0.5
    if price < bbl: bull += 3; reasons.append("✅ Below BB lower")
    elif bb_pct < 0.2: bull += 2; reasons.append("✅ Near BB lower support")
    elif price > bbu: bear += 3; reasons.append("🔴 Above BB upper")
    elif bb_pct > 0.8: bear += 2; reasons.append("🔴 Near BB upper resistance")

    # 6. Stochastic
    if sk < 15: bull += 3; reasons.append(f"✅ Stoch extreme oversold {sk:.0f}")
    elif sk < 25: bull += 2; reasons.append(f"✅ Stoch oversold {sk:.0f}")
    elif sk > 85: bear += 3; reasons.append(f"🔴 Stoch extreme overbought {sk:.0f}")
    elif sk > 75: bear += 2; reasons.append(f"🔴 Stoch overbought {sk:.0f}")

    # 7. VWAP
    if price > vwap_v * 1.002: bull += 2; reasons.append("✅ Above VWAP")
    elif price < vwap_v * 0.998: bear += 2; reasons.append("🔴 Below VWAP")

    # 8. Volume
    if vol_r > 2.5 and chg > 0: bull += 3; reasons.append(f"✅ Huge vol surge bullish {vol_r:.1f}x")
    elif vol_r > 1.8 and chg > 0: bull += 2; reasons.append(f"✅ Vol surge bullish {vol_r:.1f}x")
    elif vol_r > 2.5 and chg < 0: bear += 3; reasons.append(f"🔴 Huge vol surge bearish {vol_r:.1f}x")
    elif vol_r > 1.8 and chg < 0: bear += 2; reasons.append(f"🔴 Vol surge bearish {vol_r:.1f}x")

    # 9. Funding Rate
    if fund > 0.003: bear += 3; reasons.append(f"🔴 Extreme funding {fund*100:.3f}%")
    elif fund > 0.001: bear += 2
    elif fund < -0.003: bull += 3; reasons.append(f"✅ Extreme negative funding {fund*100:.3f}%")
    elif fund < -0.001: bull += 2

    # 10. 24h Range
    rng = h24 - l24
    if rng > 0:
        pos = (price - l24) / rng
        if pos < 0.10: bull += 3; reasons.append("✅ At 24h low support")
        elif pos < 0.25: bull += 2
        elif pos > 0.90: bear += 3; reasons.append("🔴 At 24h high resistance")
        elif pos > 0.75: bear += 2

    # 11. MTF Confluence
    tf_str = " ".join(f"{k}:{v}" for k, v in tf_map.items())
    log.info(f"[MTF] {tf_str}")
    if mtf_b > mtf_br * 1.8: bull += 5; reasons.append("✅ ALL timeframes bullish!")
    elif mtf_b > mtf_br * 1.3: bull += 3; reasons.append("✅ Most TF bullish")
    elif mtf_br > mtf_b * 1.8: bear += 5; reasons.append("🔴 ALL timeframes bearish!")
    elif mtf_br > mtf_b * 1.3: bear += 3; reasons.append("🔴 Most TF bearish")

    # 12. Momentum
    if chg > 4: bull += 2
    elif chg > 2: bull += 1
    elif chg < -4: bear += 2
    elif chg < -2: bear += 1

    total = bull + bear
    if total == 0:
        return None

    bull_pct = (bull / total) * 100
    bear_pct = 100 - bull_pct

    if bull >= 20 and bull_pct >= 63:
        action, side, hold = "LONG", "buy", "long"
        conf = min(97, int(bull_pct))
    elif bear >= 20 and bear_pct >= 63:
        action, side, hold = "SHORT", "sell", "short"
        conf = min(97, int(bear_pct))
    else:
        action, side, hold = "HOLD", "none", "none"
        conf = 50

    # Smart Leverage
    if volat > 5: lev = 1
    elif volat > 3.5: lev = 2
    elif volat > 2.5: lev = 3
    elif volat > 1.5: lev = 4 if conf >= 80 else 3
    elif conf >= 92: lev = min(MAX_LEVERAGE, 10)
    elif conf >= 87: lev = min(MAX_LEVERAGE, 8)
    elif conf >= 82: lev = min(MAX_LEVERAGE, 6)
    elif conf >= 75: lev = min(MAX_LEVERAGE, 5)
    else: lev = min(MAX_LEVERAGE, 3)

    sl_d  = atr_v * SL_MULT
    tp1_d = atr_v * TP1_MULT
    tp2_d = atr_v * TP2_MULT
    tp3_d = atr_v * TP3_MULT
    trail = atr_v * TRAIL_MULT

    if action == "LONG":
        sl, tp1, tp2, tp3 = price-sl_d, price+tp1_d, price+tp2_d, price+tp3_d
    elif action == "SHORT":
        sl, tp1, tp2, tp3 = price+sl_d, price-tp1_d, price-tp2_d, price-tp3_d
    else:
        sl = tp1 = tp2 = tp3 = price

    return {
        "sym": sym, "asset": sym.replace("USDT",""),
        "action": action, "side": side, "hold": hold,
        "conf": conf, "price": price,
        "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "lev": lev, "rr": f"1:{round(tp2_d/sl_d,1)}",
        "atr": atr_v, "trail": trail,
        "rsi": r_now, "macd_h": mh, "stoch": sk,
        "vol_r": vol_r, "volat": volat,
        "bull": bull, "bear": bear,
        "reasons": reasons[:5],
    }


# ══════════════════════════════════════════════════════════════
#  POSITION MANAGER
# ══════════════════════════════════════════════════════════════

def manage_positions(open_pos):
    for pos in open_pos:
        sym  = pos.get("symbol")
        hold = pos.get("holdSide", "long")
        mark = float(pos.get("markPrice", 0))
        size = float(pos.get("total", 0))
        entry= float(pos.get("openPriceAvg", mark))
        pnl  = float(pos.get("unrealizedPL", 0))

        if sym not in S["trades"]:
            continue

        t = S["trades"][sym]
        close_it = False
        reason = ""
        trail = t.get("trail", mark * 0.008)

        if hold == "long":
            new_sl = mark - trail
            if new_sl > t["sl"] and mark > entry * 1.005:
                t["sl"] = new_sl
                S["trades"][sym] = t

            if mark >= t["tp1"] and entry > t["sl"]:
                be = entry * 1.003
                if be > t["sl"]:
                    t["sl"] = be
                    S["trades"][sym] = t
                    log.info(f"[TRAIL] {sym} TP1! SL→BE ${be:,.2f} ✅")

            if mark >= t["tp2"]:
                tight = mark * 0.985
                if tight > t["sl"]:
                    t["sl"] = tight
                    S["trades"][sym] = t
                    log.info(f"[TRAIL] {sym} TP2! Tight trail ${tight:,.2f} 🎯")

            if mark <= t["sl"]: close_it = True; reason = f"SL @ ${mark:,.2f}"
            elif mark >= t["tp3"]: close_it = True; reason = f"🎯 TP3 @ ${mark:,.2f}"

        elif hold == "short":
            new_sl = mark + trail
            if new_sl < t["sl"] and mark < entry * 0.995:
                t["sl"] = new_sl
                S["trades"][sym] = t

            if mark <= t["tp1"] and entry < t["sl"]:
                be = entry * 0.997
                if be < t["sl"]:
                    t["sl"] = be
                    S["trades"][sym] = t
                    log.info(f"[TRAIL] {sym} TP1! SL→BE ${be:,.2f} ✅")

            if mark <= t["tp2"]:
                tight = mark * 1.015
                if tight < t["sl"]:
                    t["sl"] = tight
                    S["trades"][sym] = t
                    log.info(f"[TRAIL] {sym} TP2! Tight trail ${tight:,.2f} 🎯")

            if mark >= t["sl"]: close_it = True; reason = f"SL @ ${mark:,.2f}"
            elif mark <= t["tp3"]: close_it = True; reason = f"🎯 TP3 @ ${mark:,.2f}"

        pnl_pct = (pnl / t.get("margin", 1)) * 100 if t.get("margin") else 0
        log.info(f"[MON] {sym} {hold.upper()} Mark:${mark:,.2f} PnL:{pnl:+.4f}({pnl_pct:+.1f}%) SL:${t['sl']:,.2f}")

        if close_it and size > 0:
            log.info(f"[EXIT] {sym} — {reason}")
            res = close_order(sym, hold, size)
            if res and res.get("code") == "00000":
                S["daily_pnl"] += pnl
                S["total_pnl"] += pnl
                if pnl > 0:
                    S["wins"] += 1; S["win_streak"] += 1; S["loss_streak"] = 0
                    log.info(f"[WIN] ✅ +${pnl:.4f} | Streak:{S['win_streak']}W 🏆")
                else:
                    S["losses"] += 1; S["loss_streak"] += 1; S["win_streak"] = 0
                    log.info(f"[LOSS] ❌ ${pnl:.4f} | Streak:{S['loss_streak']}L")
                del S["trades"][sym]
            else:
                log.error(f"[EXIT FAIL] {res}")


# ══════════════════════════════════════════════════════════════
#  STATS REPORT
# ══════════════════════════════════════════════════════════════

def print_report(bal):
    start = S["start_bal"]
    growth = ((bal - start) / start * 100) if start > 0 else 0
    dd = ((S["peak_bal"] - bal) / S["peak_bal"] * 100) if S["peak_bal"] > 0 else 0
    total = S["wins"] + S["losses"]
    wr = (S["wins"] / total * 100) if total > 0 else 0
    log.info("╔══════════════════════════════════════╗")
    log.info("║   💰 ARES COMPOUND REPORT            ║")
    log.info(f"║  Start:    ${start:.2f}                   ")
    log.info(f"║  Now:      ${bal:.2f}                   ")
    log.info(f"║  Growth:   {growth:+.2f}%                 ")
    log.info(f"║  Peak:     ${S['peak_bal']:.2f}                   ")
    log.info(f"║  Drawdown: {dd:.2f}%                  ")
    log.info(f"║  PnL:      ${S['total_pnl']:+.4f}              ")
    log.info(f"║  WinRate:  {wr:.1f}% ({S['wins']}W/{S['losses']}L)          ")
    log.info(f"║  Next size:~${bal*COMPOUND_PCT:.2f} margin          ")
    log.info("╚══════════════════════════════════════╝")


# ══════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════

def run():
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║  ▲ ARES ULTRA v4.1 — World #1 Futures Bot       ║")
    log.info("║  Bitget USDT-M | BTC + ETH | BALANCE FIXED      ║")
    log.info(f"║  Compound:{COMPOUND_PCT*100:.0f}% | MaxLev:{MAX_LEVERAGE}x | Trades:{MAX_TRADES}  ║")
    log.info("╚══════════════════════════════════════════════════╝")

    if not API_KEY or not SECRET_KEY or not PASSPHRASE:
        log.error("❌ API keys missing!")
        return

    # Test API connection
    log.info("[TEST] Testing API connection...")
    bal = fetch_balance()
    if bal == 0:
        log.warning("[TEST] Balance $0 — API may need verification")
        log.warning("[TEST] Check: 1) Keys correct 2) Futures enabled 3) Has USDT")
    else:
        log.info(f"[TEST] ✅ API Connected! Balance: ${bal:.2f} USDT")

    S["start_bal"] = bal
    S["peak_bal"] = bal
    log.info(f"[INIT] Start: ${bal:.2f} | Trade size: ~${bal*COMPOUND_PCT:.2f}/trade")

    cycle = 0
    while True:
        try:
            cycle += 1
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Daily reset
            today = datetime.now().date()
            if today != S["daily_start"]:
                log.info("🔄 New day — resetting stats")
                S["daily_pnl"] = 0
                S["daily_start"] = today

            log.info(f"\n{'═'*52}")
            log.info(f"  CYCLE {cycle} | {now}")
            log.info(f"{'═'*52}")

            bal = fetch_balance()
            if bal > S["peak_bal"]:
                S["peak_bal"] = bal

            # Daily loss protection
            if S["start_bal"] > 0:
                loss_pct = S["daily_pnl"] / S["start_bal"]
                if loss_pct <= -MAX_DAILY_LOSS:
                    log.warning(f"⛔ Daily loss {MAX_DAILY_LOSS*100:.0f}% hit! Pausing 2hr...")
                    time.sleep(7200)
                    S["daily_pnl"] = 0
                    continue

            total = S["wins"] + S["losses"]
            wr = (S["wins"] / total * 100) if total > 0 else 0
            log.info(f"[BAL] ${bal:.2f} USDT | Day:${S['daily_pnl']:+.4f} | "
                     f"Total:${S['total_pnl']:+.4f} | {S['wins']}W/{S['losses']}L ({wr:.0f}%)")

            if bal < 6:
                log.warning("[SKIP] Balance < $6")
                time.sleep(SCAN_INTERVAL)
                continue

            # Positions
            open_pos = fetch_positions()
            log.info(f"[POS] {len(open_pos)} open | Tracking: {list(S['trades'].keys())}")
            if open_pos:
                manage_positions(open_pos)
                open_pos = fetch_positions()

            open_syms = [p.get("symbol") for p in open_pos]

            # Report every 20 cycles
            if cycle % 20 == 0:
                print_report(bal)

            if len(open_pos) >= MAX_TRADES:
                log.info(f"[SKIP] {MAX_TRADES} trades open")
                time.sleep(SCAN_INTERVAL)
                continue

            # Analyze
            for sym in SYMBOLS:
                if sym in open_syms:
                    log.info(f"[{sym}] Position open — skip")
                    continue

                log.info(f"\n[SCAN] ━━━ {sym} ━━━━━━━━━━━━━━━━━━━━━━━")

                tick  = pub_ticker(sym)
                cdata = pub_candles(sym, "15", 150)
                fund  = pub_funding(sym)

                if not tick or not cdata:
                    log.warning(f"[{sym}] No data")
                    continue

                price = float(tick.get("lastPr", 0))
                chg   = float(tick.get("change24h", 0)) * 100
                log.info(f"[{sym}] ${price:,.2f} | {chg:+.2f}% | Fund:{fund*100:.4f}%")

                sig = generate_signal(sym, tick, cdata, fund)
                if not sig:
                    log.info(f"[{sym}] No signal")
                    continue

                em = "🟢" if sig["action"]=="LONG" else "🔴" if sig["action"]=="SHORT" else "🟡"
                log.info(f"[SIG] {em} {sig['action']} | Conf:{sig['conf']}% | "
                         f"Lev:{sig['lev']}x | R:R {sig['rr']}")
                log.info(f"[IND] RSI:{sig['rsi']:.0f} MACD:{sig['macd_h']:+.2f} "
                         f"Stoch:{sig['stoch']:.0f} Vol:{sig['vol_r']:.1f}x")
                log.info(f"[SCR] 🟢{sig['bull']} vs 🔴{sig['bear']}")
                for r in sig["reasons"][:3]:
                    log.info(f"  {r}")

                if sig["action"] in ("LONG","SHORT") and sig["conf"] >= MIN_CONF:
                    size, margin = compound_size(bal, price, sig["lev"], sig["conf"])
                    if size <= 0:
                        continue

                    log.info(f"[COMPOUND] ${margin:.2f} margin | {size} {sig['asset']} | {sig['lev']}x")
                    log.info(f"[RISK] SL:${sig['sl']:,.2f} TP1:${sig['tp1']:,.2f} "
                             f"TP2:${sig['tp2']:,.2f} TP3:${sig['tp3']:,.2f}")

                    res = open_order(sym, sig["side"], size, sig["lev"])

                    if res and res.get("code") == "00000":
                        oid = res.get("data", {}).get("orderId", "N/A")
                        log.info(f"[ORDER] ✅ {sig['action']} OPENED! ID:{oid}")
                        time.sleep(1.5)
                        set_sltp(sym, sig["hold"], sig["sl"], sig["tp2"])
                        log.info(f"[RISK] ✅ SL+TP set!")
                        S["trades"][sym] = {
                            "side": sig["hold"], "entry": price,
                            "sl": sig["sl"], "tp1": sig["tp1"],
                            "tp2": sig["tp2"], "tp3": sig["tp3"],
                            "trail": sig["trail"], "size": size,
                            "margin": margin, "lev": sig["lev"],
                        }
                    else:
                        err = res.get("msg") if res else "No response"
                        log.error(f"[ORDER] ❌ {err}")
                else:
                    log.info(f"[{sym}] {sig['action']} conf:{sig['conf']}% — waiting")

                time.sleep(3)

            log.info(f"\n[SLEEP] {SCAN_INTERVAL}s...")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error(f"[ERROR] {e}")
            time.sleep(30)


if __name__ == "__main__":
    run()
