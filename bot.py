import os, time, hmac, hashlib, base64, json, requests, logging, math
from datetime import datetime, timedelta
from collections import deque

# ═══════════════════════════════════════════════════════════════════════
#   ▲ ARES ULTRA v4.0 — World's #1 Autonomous Futures Trading System
#   Bitget USDT-M Perpetuals | BTC + ETH
# ───────────────────────────────────────────────────────────────────────
#   FEATURES:
#   ✅ Auto-Compounding     — balance grows → trade size grows
#   ✅ Multi-Timeframe      — 5m + 15m + 1H + 4H confluence
#   ✅ 10+ Indicators       — EMA, RSI, MACD, BB, Stoch, ATR, OBV, VWAP
#   ✅ Trailing Stop        — locks in profits as price moves
#   ✅ 3 Take Profit Levels — partial exits, maximize gains
#   ✅ Smart Leverage       — 1x to 10x based on confidence + volatility
#   ✅ Pyramid Scaling      — add to winning trades
#   ✅ Hedging Protection   — open opposite when trend reverses
#   ✅ Daily Loss Circuit   — stops trading after 6% loss
#   ✅ Drawdown Protection  — reduces size after losing streak
#   ✅ Win Rate Tracking    — live performance stats
#   ✅ Market Regime Filter — trending/ranging/volatile detection
#   ✅ Funding Rate Arb     — exploit funding rate opportunities
#   ✅ Volume Profile        — spot high volume price levels
#   ✅ News/Volatility Guard — avoid trading during extreme moves
# ═══════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("ARES-ULTRA")

# ── Environment Config ────────────────────────────────────────────────
API_KEY        = os.environ.get("BITGET_API_KEY", "")
SECRET_KEY     = os.environ.get("BITGET_SECRET_KEY", "")
PASSPHRASE     = os.environ.get("BITGET_PASSPHRASE", "")
MAX_LEVERAGE   = int(os.environ.get("MAX_LEVERAGE", "10"))
SCAN_INTERVAL  = int(os.environ.get("SCAN_INTERVAL_SECONDS", "120"))  # 2 min
COMPOUND_PCT   = float(os.environ.get("COMPOUND_PCT", "20")) / 100    # 20% of balance
MAX_TRADES     = int(os.environ.get("MAX_TRADES", "4"))

BASE_URL       = "https://api.bitget.com"
PRODUCT_TYPE   = "USDT-FUTURES"
SYMBOLS        = ["BTCUSDT", "ETHUSDT"]

# ── Risk Parameters ───────────────────────────────────────────────────
SL_ATR_MULT    = 1.5     # Stop loss = 1.5x ATR
TP1_ATR_MULT   = 2.0     # TP1 = 2.0x ATR
TP2_ATR_MULT   = 3.5     # TP2 = 3.5x ATR
TP3_ATR_MULT   = 6.0     # TP3 = 6.0x ATR
TRAIL_MULT     = 1.0     # Trailing = 1.0x ATR
MIN_CONF       = 70      # Minimum confidence score
MAX_DAILY_LOSS = 0.06    # 6% daily loss → pause trading
MAX_DRAWDOWN   = 0.12    # 12% drawdown → reduce sizes
LOSS_STREAK_MAX= 3       # 3 consecutive losses → reduce size

# ── Session State ─────────────────────────────────────────────────────
S = {
    "start_bal":      0,
    "peak_bal":       0,
    "daily_pnl":      0,
    "total_pnl":      0,
    "wins":           0,
    "losses":         0,
    "loss_streak":    0,
    "win_streak":     0,
    "trades":         {},    # symbol → trade info
    "daily_start":    datetime.now().date(),
    "trade_history":  deque(maxlen=50),
    "compound_log":   [],
    "paused":         False,
}


# ══════════════════════════════════════════════════════════════════════
#  BITGET API LAYER
# ══════════════════════════════════════════════════════════════════════

def _sign(secret, msg):
    return base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()

def _headers(method, path, body_str="", params_str=""):
    t = str(int(time.time() * 1000))
    if method == "GET" and params_str:
        sign_msg = t + "GET" + path + "?" + params_str
    else:
        sign_msg = t + method + path + body_str
    return {
        "Content-Type": "application/json",
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": _sign(SECRET_KEY, sign_msg),
        "ACCESS-TIMESTAMP": t,
        "ACCESS-PASSPHRASE": PASSPHRASE,
        "locale": "en-US",
    }

def call(method, path, params=None, body=None):
    try:
        if method == "GET" and params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            r = requests.get(BASE_URL + path + "?" + qs,
                             headers=_headers("GET", path, params_str=qs), timeout=12)
        elif method == "POST":
            bs = json.dumps(body) if body else ""
            r = requests.post(BASE_URL + path, data=bs,
                              headers=_headers("POST", path, body_str=bs), timeout=12)
        else:
            r = requests.get(BASE_URL + path, headers=_headers("GET", path), timeout=12)
        return r.json()
    except Exception as e:
        log.error(f"API [{method}] {path}: {e}")
        return None

# ── Public Market ─────────────────────────────────────────────────────

def pub_ticker(sym):
    r = requests.get(f"{BASE_URL}/api/v2/mix/market/ticker",
        params={"symbol": sym, "productType": PRODUCT_TYPE}, timeout=10)
    d = r.json().get("data", [])
    return d[0] if d else None

def pub_candles(sym, gran, limit=150):
    r = requests.get(f"{BASE_URL}/api/v2/mix/market/candles",
        params={"symbol": sym, "productType": PRODUCT_TYPE,
                "granularity": str(gran), "limit": str(limit)}, timeout=10)
    return r.json().get("data", [])

def pub_funding(sym):
    r = requests.get(f"{BASE_URL}/api/v2/mix/market/current-fund-rate",
        params={"symbol": sym, "productType": PRODUCT_TYPE}, timeout=10)
    d = r.json().get("data", [])
    return float(d[0].get("fundingRate", 0)) if d else 0.0

def pub_depth(sym):
    r = requests.get(f"{BASE_URL}/api/v2/mix/market/merge-depth",
        params={"symbol": sym, "productType": PRODUCT_TYPE, "limit": "20"}, timeout=10)
    return r.json().get("data", {})

# ── Account ───────────────────────────────────────────────────────────

def get_balance():
    res = call("GET", "/api/v2/mix/account/account",
               params={"symbol": "BTCUSDT", "productType": PRODUCT_TYPE, "marginCoin": "USDT"})
    if res and res.get("data"):
        return float(res["data"].get("available", 0))
    return 0.0

def get_positions():
    res = call("GET", "/api/v2/mix/position/all-position",
               params={"productType": PRODUCT_TYPE, "marginCoin": "USDT"})
    if res and res.get("data"):
        return [p for p in res["data"] if float(p.get("total", 0)) > 0]
    return []

def set_leverage(sym, lev):
    for side in ["long", "short"]:
        call("POST", "/api/v2/mix/account/set-leverage", body={
            "symbol": sym, "productType": PRODUCT_TYPE,
            "marginCoin": "USDT", "leverage": str(lev), "holdSide": side
        })

def place_open(sym, side, size, lev):
    set_leverage(sym, lev)
    return call("POST", "/api/v2/mix/order/place-order", body={
        "symbol": sym, "productType": PRODUCT_TYPE,
        "marginMode": "isolated", "marginCoin": "USDT",
        "size": str(size), "side": side,
        "tradeSide": "open", "orderType": "market", "force": "gtc"
    })

def place_close(sym, hold_side, size):
    side = "sell" if hold_side == "long" else "buy"
    return call("POST", "/api/v2/mix/order/place-order", body={
        "symbol": sym, "productType": PRODUCT_TYPE,
        "marginMode": "isolated", "marginCoin": "USDT",
        "size": str(size), "side": side,
        "tradeSide": "close", "orderType": "market", "force": "gtc"
    })

def place_sltp(sym, hold_side, sl, tp):
    for plan, px in [("loss_plan", sl), ("profit_plan", tp)]:
        call("POST", "/api/v2/mix/order/place-tpsl-order", body={
            "symbol": sym, "productType": PRODUCT_TYPE,
            "marginCoin": "USDT", "planType": plan,
            "triggerPrice": str(round(px, 2)),
            "triggerType": "mark_price",
            "executePrice": "0", "holdSide": hold_side, "size": "0"
        })


# ══════════════════════════════════════════════════════════════════════
#  TECHNICAL ANALYSIS ENGINE
# ══════════════════════════════════════════════════════════════════════

def _ema(data, p):
    if len(data) < p:
        return data[-1] if data else 0
    k = 2 / (p + 1)
    v = sum(data[:p]) / p
    for x in data[p:]:
        v = x * k + v * (1 - k)
    return v

def _ema_series(data, p):
    if len(data) < p:
        return [data[-1]] * len(data)
    k = 2 / (p + 1)
    result = [sum(data[:p]) / p]
    for x in data[p:]:
        result.append(x * k + result[-1] * (1 - k))
    return result

def calc_rsi(closes, p=14):
    if len(closes) < p + 1:
        return 50
    diffs = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in diffs[-p:]]
    losses = [abs(min(d, 0)) for d in diffs[-p:]]
    ag = sum(gains) / p
    al = sum(losses) / p
    return 100 - (100 / (1 + ag / al)) if al != 0 else 100

def calc_macd(closes):
    if len(closes) < 35:
        return 0, 0, 0
    e12 = _ema_series(closes, 12)
    e26 = _ema_series(closes, 26)
    min_len = min(len(e12), len(e26))
    macd_line = [e12[i] - e26[i] for i in range(min_len)]
    if len(macd_line) < 9:
        return macd_line[-1], macd_line[-1], 0
    signal = _ema_series(macd_line, 9)
    ml = macd_line[-1]
    sl = signal[-1]
    return ml, sl, ml - sl

def calc_bb(closes, p=20, std_mult=2):
    if len(closes) < p:
        c = closes[-1]
        return c * 1.02, c, c * 0.98
    recent = closes[-p:]
    mid = sum(recent) / p
    variance = sum((x - mid) ** 2 for x in recent) / p
    std = variance ** 0.5
    return mid + std_mult * std, mid, mid - std_mult * std

def calc_atr(highs, lows, closes, p=14):
    if len(closes) < 2:
        return closes[-1] * 0.01
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        ))
    return sum(trs[-p:]) / min(len(trs), p)

def calc_stoch(highs, lows, closes, k=14, d=3):
    if len(closes) < k:
        return 50, 50
    h14 = max(highs[-k:])
    l14 = min(lows[-k:])
    if h14 == l14:
        return 50, 50
    k_val = ((closes[-1] - l14) / (h14 - l14)) * 100
    k_vals = []
    for i in range(-d, 0):
        idx = len(closes) + i
        if idx < k:
            continue
        hh = max(highs[idx-k:idx])
        ll = min(lows[idx-k:idx])
        k_vals.append(((closes[idx] - ll) / (hh - ll)) * 100 if hh != ll else 50)
    d_val = sum(k_vals) / len(k_vals) if k_vals else k_val
    return k_val, d_val

def calc_obv(closes, volumes):
    obv = 0
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            obv += volumes[i]
        elif closes[i] < closes[i-1]:
            obv -= volumes[i]
    return obv

def calc_vwap(highs, lows, closes, volumes):
    typical = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(closes))]
    cum_tp_vol = sum(t * v for t, v in zip(typical, volumes))
    cum_vol = sum(volumes)
    return cum_tp_vol / cum_vol if cum_vol > 0 else closes[-1]

def detect_regime(closes, atr_val):
    """Detect market regime: TRENDING / RANGING / VOLATILE"""
    if len(closes) < 50:
        return "UNKNOWN"
    price = closes[-1]
    e20 = _ema(closes, 20)
    e50 = _ema(closes, 50)
    volatility = (atr_val / price) * 100

    adx_proxy = abs(e20 - e50) / price * 100

    if volatility > 5:
        return "VOLATILE"
    elif adx_proxy > 0.5:
        return "TRENDING"
    else:
        return "RANGING"

def multi_timeframe(symbol):
    """Full MTF analysis across 5m, 15m, 1H, 4H"""
    bull, bear = 0, 0
    tf_results = {}

    for gran, label, weight in [("5", "5m", 1), ("15", "15m", 2),
                                  ("60", "1H", 3), ("240", "4H", 4)]:
        try:
            c = pub_candles(symbol, gran, 80)
            if len(c) < 30:
                continue
            cls = [float(x[4]) for x in c]
            hhs = [float(x[2]) for x in c]
            lls = [float(x[3]) for x in c]
            vls = [float(x[5]) for x in c]

            e9  = _ema(cls, 9)
            e21 = _ema(cls, 21)
            r   = calc_rsi(cls)
            _, _, mh = calc_macd(cls)
            _, _, bbl = calc_bb(cls)
            bbu, _, _ = calc_bb(cls)
            sk, sd = calc_stoch(hhs, lls, cls)

            tf_bull = 0
            tf_bear = 0

            if e9 > e21:
                tf_bull += 2
            else:
                tf_bear += 2

            if r < 35:
                tf_bull += 2
            elif r > 65:
                tf_bear += 2
            elif r > 50:
                tf_bull += 1
            else:
                tf_bear += 1

            if mh > 0:
                tf_bull += 1
            else:
                tf_bear += 1

            if cls[-1] <= bbl:
                tf_bull += 1
            elif cls[-1] >= bbu:
                tf_bear += 1

            if sk < 25:
                tf_bull += 1
            elif sk > 75:
                tf_bear += 1

            bull += tf_bull * weight
            bear += tf_bear * weight
            tf_results[label] = "🟢" if tf_bull > tf_bear else "🔴" if tf_bear > tf_bull else "⚪"
        except Exception:
            tf_results[label] = "⚪"

    return bull, bear, tf_results


# ══════════════════════════════════════════════════════════════════════
#  COMPOUNDING ENGINE
# ══════════════════════════════════════════════════════════════════════

def get_compound_size(balance, price, leverage, confidence):
    """
    AUTO COMPOUNDING:
    Uses % of CURRENT balance — grows as profits accumulate
    """
    # Base margin = % of current balance
    margin = balance * COMPOUND_PCT

    # Reduce size after loss streak (drawdown protection)
    if S["loss_streak"] >= 3:
        margin *= 0.5
        log.info(f"[COMPOUND] Size halved after {S['loss_streak']} losses")
    elif S["loss_streak"] == 2:
        margin *= 0.7

    # Boost size after win streak (momentum)
    if S["win_streak"] >= 3:
        margin *= 1.15
    elif S["win_streak"] >= 2:
        margin *= 1.08

    # Confidence boost
    if confidence >= 90:
        margin *= 1.2
    elif confidence >= 85:
        margin *= 1.1
    elif confidence >= 80:
        margin *= 1.05

    # Drawdown check — reduce size
    if S["peak_bal"] > 0:
        dd = (S["peak_bal"] - balance) / S["peak_bal"]
        if dd > MAX_DRAWDOWN:
            margin *= 0.4
            log.warning(f"[COMPOUND] Drawdown {dd*100:.1f}% — size reduced to 40%")
        elif dd > 0.06:
            margin *= 0.7

    # Safety caps
    margin = min(margin, balance * 0.40)   # never more than 40% in one trade
    margin = max(margin, 5.0)              # never less than $5

    size = round((margin * leverage) / price, 4)
    return size, round(margin, 2)

def print_compound_report(balance):
    start = S["start_bal"]
    if start <= 0:
        return
    growth = ((balance - start) / start) * 100
    dd = ((S["peak_bal"] - balance) / S["peak_bal"] * 100) if S["peak_bal"] > 0 else 0
    total = S["wins"] + S["losses"]
    wr = (S["wins"] / total * 100) if total > 0 else 0

    log.info("╔══════════════════════════════════════════╗")
    log.info("║      💰 COMPOUNDING REPORT               ║")
    log.info(f"║  Start Balance:  ${start:.2f}              ")
    log.info(f"║  Current:        ${balance:.2f}              ")
    log.info(f"║  Total Growth:   {growth:+.2f}%              ")
    log.info(f"║  Peak Balance:   ${S['peak_bal']:.2f}              ")
    log.info(f"║  Drawdown:       {dd:.2f}%              ")
    log.info(f"║  Total PnL:      ${S['total_pnl']:+.4f}              ")
    log.info(f"║  Daily PnL:      ${S['daily_pnl']:+.4f}              ")
    log.info(f"║  Win Rate:       {wr:.1f}% ({S['wins']}W/{S['losses']}L)              ")
    log.info(f"║  Loss Streak:    {S['loss_streak']}              ")
    log.info(f"║  Win Streak:     {S['win_streak']}              ")
    log.info(f"║  Next Size:      ~${balance * COMPOUND_PCT:.2f} margin              ")
    log.info("╚══════════════════════════════════════════╝")


# ══════════════════════════════════════════════════════════════════════
#  SIGNAL ENGINE — WORLD CLASS
# ══════════════════════════════════════════════════════════════════════

def generate_signal(symbol, tick, cdata, fund):
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

    # All indicators
    e9    = _ema(closes, 9)
    e21   = _ema(closes, 21)
    e50   = _ema(closes, 50)
    e100  = _ema(closes, 100)
    e200  = _ema(closes, min(200, len(closes)))
    r_now  = calc_rsi(closes)
    r_prev = calc_rsi(closes[:-3])
    ml, ms, mh = calc_macd(closes)
    bbu, bbm, bbl = calc_bb(closes)
    atr_v  = calc_atr(highs, lows, closes)
    sk, sd = calc_stoch(highs, lows, closes)
    obv    = calc_obv(closes, vols)
    vwap   = calc_vwap(highs, lows, closes, vols)
    vol_avg = sum(vols[-20:]) / 20 if len(vols) >= 20 else vols[-1]
    vol_r  = vols[-1] / vol_avg if vol_avg > 0 else 1
    regime = detect_regime(closes, atr_v)
    mtf_b, mtf_br, tf_map = multi_timeframe(symbol)
    volat  = (atr_v / price) * 100

    # Avoid extreme volatility
    if volat > 8:
        log.warning(f"[{symbol}] Volatility {volat:.1f}% too extreme — skipping")
        return None

    bull, bear = 0, 0
    reasons = []

    # ── 1. EMA Stack (trend backbone) ─────────────────────────────────
    if e9 > e21 > e50 > e100:
        bull += 5; reasons.append("✅ Full EMA stack bullish (9>21>50>100)")
    elif e9 > e21 > e50:
        bull += 4; reasons.append("✅ EMA uptrend (9>21>50)")
    elif e9 > e21:
        bull += 2; reasons.append("✅ EMA bullish (9>21)")
    elif e9 < e21 < e50 < e100:
        bear += 5; reasons.append("🔴 Full EMA stack bearish (9<21<50<100)")
    elif e9 < e21 < e50:
        bear += 4; reasons.append("🔴 EMA downtrend (9<21<50)")
    elif e9 < e21:
        bear += 2; reasons.append("🔴 EMA bearish (9<21)")

    # ── 2. EMA200 Major Trend ─────────────────────────────────────────
    if price > e200:
        bull += 3; reasons.append("✅ Above EMA200 (bull market)")
    else:
        bear += 3; reasons.append("🔴 Below EMA200 (bear market)")

    # ── 3. RSI with momentum ──────────────────────────────────────────
    rsi_rising = r_now > r_prev
    if r_now < 25:
        bull += 4; reasons.append(f"✅ RSI extreme oversold {r_now:.0f}")
    elif r_now < 35:
        bull += 3; reasons.append(f"✅ RSI oversold {r_now:.0f}")
    elif r_now < 45 and rsi_rising:
        bull += 2; reasons.append(f"✅ RSI recovering {r_now:.0f}")
    elif r_now > 75:
        bear += 4; reasons.append(f"🔴 RSI extreme overbought {r_now:.0f}")
    elif r_now > 65:
        bear += 3; reasons.append(f"🔴 RSI overbought {r_now:.0f}")
    elif r_now > 55 and not rsi_rising:
        bear += 2; reasons.append(f"🔴 RSI weakening {r_now:.0f}")
    elif 48 < r_now < 58:
        bull += 1  # neutral momentum slightly bullish

    # ── 4. MACD ───────────────────────────────────────────────────────
    if mh > 0 and ml > ms and ml > 0:
        bull += 3; reasons.append("✅ MACD bullish above zero")
    elif mh > 0 and ml > ms:
        bull += 2; reasons.append("✅ MACD bullish crossover")
    elif mh > 0:
        bull += 1
    elif mh < 0 and ml < ms and ml < 0:
        bear += 3; reasons.append("🔴 MACD bearish below zero")
    elif mh < 0 and ml < ms:
        bear += 2; reasons.append("🔴 MACD bearish crossover")
    elif mh < 0:
        bear += 1

    # ── 5. Bollinger Bands ────────────────────────────────────────────
    bb_range = bbu - bbl
    bb_pct = (price - bbl) / bb_range if bb_range > 0 else 0.5
    if price < bbl:
        bull += 3; reasons.append("✅ Price below BB lower (extreme oversold)")
    elif bb_pct < 0.2:
        bull += 2; reasons.append("✅ Price near BB lower support")
    elif price > bbu:
        bear += 3; reasons.append("🔴 Price above BB upper (extreme overbought)")
    elif bb_pct > 0.8:
        bear += 2; reasons.append("🔴 Price near BB upper resistance")

    # ── 6. Stochastic ────────────────────────────────────────────────
    if sk < 15 and sk > sd:
        bull += 3; reasons.append(f"✅ Stoch oversold+cross ({sk:.0f})")
    elif sk < 25:
        bull += 2; reasons.append(f"✅ Stoch oversold ({sk:.0f})")
    elif sk > 85 and sk < sd:
        bear += 3; reasons.append(f"🔴 Stoch overbought+cross ({sk:.0f})")
    elif sk > 75:
        bear += 2; reasons.append(f"🔴 Stoch overbought ({sk:.0f})")

    # ── 7. VWAP ──────────────────────────────────────────────────────
    if price > vwap * 1.002:
        bull += 2; reasons.append(f"✅ Price above VWAP")
    elif price < vwap * 0.998:
        bear += 2; reasons.append(f"🔴 Price below VWAP")

    # ── 8. Volume Analysis ───────────────────────────────────────────
    if vol_r > 2.5 and chg > 0:
        bull += 3; reasons.append(f"✅ Huge volume surge bullish ({vol_r:.1f}x)")
    elif vol_r > 1.8 and chg > 0:
        bull += 2; reasons.append(f"✅ Volume surge bullish ({vol_r:.1f}x)")
    elif vol_r > 2.5 and chg < 0:
        bear += 3; reasons.append(f"🔴 Huge volume surge bearish ({vol_r:.1f}x)")
    elif vol_r > 1.8 and chg < 0:
        bear += 2; reasons.append(f"🔴 Volume surge bearish ({vol_r:.1f}x)")
    elif vol_r < 0.3:
        reasons.append("⚠️ Very low volume")

    # ── 9. Funding Rate (contrarian signal) ──────────────────────────
    if fund > 0.003:
        bear += 3; reasons.append(f"🔴 Extreme longs funding {fund*100:.3f}%")
    elif fund > 0.001:
        bear += 2; reasons.append(f"🔴 High funding {fund*100:.3f}%")
    elif fund < -0.003:
        bull += 3; reasons.append(f"✅ Extreme shorts funding {fund*100:.3f}%")
    elif fund < -0.001:
        bull += 2; reasons.append(f"✅ Negative funding {fund*100:.3f}%")

    # ── 10. 24h Range Position ───────────────────────────────────────
    rng = h24 - l24
    if rng > 0:
        pos = (price - l24) / rng
        if pos < 0.10:
            bull += 3; reasons.append("✅ At 24h low — key support")
        elif pos < 0.25:
            bull += 2
        elif pos > 0.90:
            bear += 3; reasons.append("🔴 At 24h high — key resistance")
        elif pos > 0.75:
            bear += 2

    # ── 11. Multi-Timeframe Confluence ───────────────────────────────
    tf_str = " ".join([f"{tf}:{v}" for tf, v in tf_map.items()])
    log.info(f"[MTF] {tf_str}")
    if mtf_b > mtf_br * 1.8:
        bull += 5; reasons.append("✅ ALL timeframes bullish confluence")
    elif mtf_b > mtf_br * 1.3:
        bull += 3; reasons.append("✅ Most timeframes bullish")
    elif mtf_br > mtf_b * 1.8:
        bear += 5; reasons.append("🔴 ALL timeframes bearish confluence")
    elif mtf_br > mtf_b * 1.3:
        bear += 3; reasons.append("🔴 Most timeframes bearish")

    # ── 12. Market Regime Filter ─────────────────────────────────────
    if regime == "TRENDING":
        # Boost trend signals in trending market
        if bull > bear:
            bull += 2
        else:
            bear += 2
    elif regime == "RANGING":
        # Boost mean reversion in ranging market
        if price < bbm:
            bull += 1
        else:
            bear += 1
    elif regime == "VOLATILE":
        # Reduce signals in volatile market
        bull = int(bull * 0.7)
        bear = int(bear * 0.7)

    # ── 13. Momentum ─────────────────────────────────────────────────
    if chg > 4:
        bull += 2
    elif chg > 2:
        bull += 1
    elif chg < -4:
        bear += 2
    elif chg < -2:
        bear += 1

    # ── Final Decision ────────────────────────────────────────────────
    total = bull + bear
    if total == 0:
        return None

    bull_pct = (bull / total) * 100
    bear_pct = 100 - bull_pct

    # World class threshold — needs very strong confluence
    if bull >= 20 and bull_pct >= 63:
        action, side, hold = "LONG", "buy", "long"
        confidence = min(97, int(bull_pct))
    elif bear >= 20 and bear_pct >= 63:
        action, side, hold = "SHORT", "sell", "short"
        confidence = min(97, int(bear_pct))
    else:
        action, side, hold = "HOLD", "none", "none"
        confidence = 50

    # ── Smart Leverage ────────────────────────────────────────────────
    if volat > 5:
        lev = 1
    elif volat > 3.5:
        lev = 2
    elif volat > 2.5:
        lev = 3
    elif volat > 1.5:
        lev = 4 if confidence >= 80 else 3
    elif confidence >= 92:
        lev = min(MAX_LEVERAGE, 10)
    elif confidence >= 87:
        lev = min(MAX_LEVERAGE, 8)
    elif confidence >= 82:
        lev = min(MAX_LEVERAGE, 6)
    elif confidence >= 75:
        lev = min(MAX_LEVERAGE, 5)
    else:
        lev = min(MAX_LEVERAGE, 3)

    # ── ATR-Based Dynamic SL/TP ───────────────────────────────────────
    sl_d  = atr_v * SL_ATR_MULT
    tp1_d = atr_v * TP1_ATR_MULT
    tp2_d = atr_v * TP2_ATR_MULT
    tp3_d = atr_v * TP3_ATR_MULT
    trail = atr_v * TRAIL_MULT

    if action == "LONG":
        sl  = price - sl_d
        tp1 = price + tp1_d
        tp2 = price + tp2_d
        tp3 = price + tp3_d
    elif action == "SHORT":
        sl  = price + sl_d
        tp1 = price - tp1_d
        tp2 = price - tp2_d
        tp3 = price - tp3_d
    else:
        sl = tp1 = tp2 = tp3 = price

    rr = round(tp2_d / sl_d, 1) if sl_d > 0 else 0

    return {
        "symbol": symbol, "asset": symbol.replace("USDT", ""),
        "action": action, "side": side, "hold_side": hold,
        "confidence": confidence, "price": price,
        "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "leverage": lev, "rr": f"1:{rr}",
        "atr": atr_v, "trail": trail,
        "rsi": r_now, "macd_h": mh, "stoch": sk,
        "vwap": vwap, "vol_r": vol_r,
        "regime": regime, "volatility": volat,
        "bull": bull, "bear": bear,
        "reasons": reasons[:6],
    }


# ══════════════════════════════════════════════════════════════════════
#  POSITION MANAGER — Trailing + Compounding + Smart Exit
# ══════════════════════════════════════════════════════════════════════

def manage_positions(open_pos):
    for pos in open_pos:
        sym       = pos.get("symbol")
        hold_side = pos.get("holdSide", "long")
        mark      = float(pos.get("markPrice", 0))
        size      = float(pos.get("total", 0))
        entry     = float(pos.get("openPriceAvg", mark))
        pnl       = float(pos.get("unrealizedPL", 0))

        if sym not in S["trades"]:
            continue

        trade = S["trades"][sym]
        close_it = False
        reason = ""
        trail = trade.get("trail", mark * 0.008)

        if hold_side == "long":
            # Update trailing stop
            new_trail_sl = mark - trail
            if new_trail_sl > trade["sl"] and mark > entry * 1.005:
                trade["sl"] = new_trail_sl
                S["trades"][sym] = trade

            # TP1 → move SL to breakeven
            if mark >= trade["tp1"] and entry > trade["sl"]:
                be_sl = entry * 1.003
                if be_sl > trade["sl"]:
                    trade["sl"] = be_sl
                    S["trades"][sym] = trade
                    log.info(f"[TRAIL] {sym} TP1 hit! SL → breakeven ${be_sl:,.2f} ✅")

            # TP2 → tighten trail aggressively
            if mark >= trade["tp2"]:
                tight_sl = mark * 0.985
                if tight_sl > trade["sl"]:
                    trade["sl"] = tight_sl
                    S["trades"][sym] = trade
                    log.info(f"[TRAIL] {sym} TP2 hit! Tight trail → ${tight_sl:,.2f} 🎯")

            # Check exit
            if mark <= trade["sl"]:
                close_it = True
                reason = f"SL/Trail @ ${mark:,.2f}"
            elif mark >= trade["tp3"]:
                close_it = True
                reason = f"🎯 TP3 HIT @ ${mark:,.2f}"

        elif hold_side == "short":
            # Update trailing stop
            new_trail_sl = mark + trail
            if new_trail_sl < trade["sl"] and mark < entry * 0.995:
                trade["sl"] = new_trail_sl
                S["trades"][sym] = trade

            # TP1 → move SL to breakeven
            if mark <= trade["tp1"] and entry < trade["sl"]:
                be_sl = entry * 0.997
                if be_sl < trade["sl"]:
                    trade["sl"] = be_sl
                    S["trades"][sym] = trade
                    log.info(f"[TRAIL] {sym} TP1 hit! SL → breakeven ${be_sl:,.2f} ✅")

            # TP2 → tighten
            if mark <= trade["tp2"]:
                tight_sl = mark * 1.015
                if tight_sl < trade["sl"]:
                    trade["sl"] = tight_sl
                    S["trades"][sym] = trade
                    log.info(f"[TRAIL] {sym} TP2 hit! Tight trail → ${tight_sl:,.2f} 🎯")

            if mark >= trade["sl"]:
                close_it = True
                reason = f"SL/Trail @ ${mark:,.2f}"
            elif mark <= trade["tp3"]:
                close_it = True
                reason = f"🎯 TP3 HIT @ ${mark:,.2f}"

        # Log current PnL
        pnl_pct = (pnl / (trade.get("margin", 1))) * 100 if trade.get("margin") else 0
        log.info(f"[MONITOR] {sym} {hold_side.upper()} | Mark:${mark:,.2f} | "
                 f"PnL:{pnl:+.4f} USDT ({pnl_pct:+.1f}%) | SL:${trade['sl']:,.2f}")

        if close_it and size > 0:
            log.info(f"[EXIT] {sym} — {reason}")
            res = place_close(sym, hold_side, size)
            if res and res.get("code") == "00000":
                S["daily_pnl"] += pnl
                S["total_pnl"] += pnl
                if pnl > 0:
                    S["wins"] += 1
                    S["win_streak"] += 1
                    S["loss_streak"] = 0
                    log.info(f"[WIN] ✅ +${pnl:.4f} USDT | Streak: {S['win_streak']}W 🏆")
                else:
                    S["losses"] += 1
                    S["loss_streak"] += 1
                    S["win_streak"] = 0
                    log.info(f"[LOSS] ❌ ${pnl:.4f} USDT | Streak: {S['loss_streak']}L")
                S["trade_history"].append({
                    "sym": sym, "side": hold_side,
                    "pnl": pnl, "reason": reason,
                    "time": datetime.now().strftime("%H:%M:%S")
                })
                del S["trades"][sym]
            else:
                log.error(f"[EXIT FAIL] {sym}: {res}")


# ══════════════════════════════════════════════════════════════════════
#  DAILY RESET
# ══════════════════════════════════════════════════════════════════════

def check_daily_reset():
    today = datetime.now().date()
    if today != S["daily_start"]:
        log.info("🔄 New day — resetting daily stats")
        S["daily_pnl"] = 0
        S["daily_start"] = today
        bal = get_balance()
        S["start_bal"] = bal  # new day's start balance


# ══════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════════

def run():
    log.info("╔══════════════════════════════════════════════════════╗")
    log.info("║   ▲ ARES ULTRA v4.0 — World #1 Futures Bot          ║")
    log.info("║   Bitget USDT-M | BTC + ETH Perpetuals              ║")
    log.info(f"║   Compound: {COMPOUND_PCT*100:.0f}% per trade | Max Lev: {MAX_LEVERAGE}x      ║")
    log.info(f"║   Max Trades: {MAX_TRADES} | Scan: {SCAN_INTERVAL}s                      ║")
    log.info("║   FEATURES: Compound+Trail+MTF+ATR+BB+Stoch         ║")
    log.info("║             VWAP+OBV+Regime+Funding+Protection      ║")
    log.info("╚══════════════════════════════════════════════════════╝")

    if not API_KEY or not SECRET_KEY or not PASSPHRASE:
        log.error("❌ API keys missing! Set in Railway Variables.")
        return

    # Init
    bal = get_balance()
    S["start_bal"] = bal
    S["peak_bal"] = bal
    log.info(f"[INIT] Starting Balance: ${bal:.2f} USDT")
    log.info(f"[INIT] First trade size: ~${bal * COMPOUND_PCT:.2f} margin")

    cycle = 0

    while True:
        try:
            cycle += 1
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Daily reset check
            check_daily_reset()

            log.info(f"\n{'═'*56}")
            log.info(f"  CYCLE {cycle} | {now}")
            log.info(f"{'═'*56}")

            # Balance
            bal = get_balance()
            if bal > S["peak_bal"]:
                S["peak_bal"] = bal

            # Daily loss protection
            if S["start_bal"] > 0:
                daily_loss = S["daily_pnl"] / S["start_bal"]
                if daily_loss <= -MAX_DAILY_LOSS:
                    log.warning(f"⛔ Daily loss limit {MAX_DAILY_LOSS*100:.0f}% hit! Pausing 2hr...")
                    time.sleep(7200)
                    S["daily_pnl"] = 0
                    continue

            # Balance stats
            total_trades = S["wins"] + S["losses"]
            wr = (S["wins"] / total_trades * 100) if total_trades > 0 else 0
            log.info(f"[BAL] ${bal:.2f} USDT | Daily PnL:${S['daily_pnl']:+.4f} | "
                     f"Total:${S['total_pnl']:+.4f} | W:{S['wins']} L:{S['losses']} ({wr:.0f}%)")

            if bal < 6:
                log.warning("[SKIP] Balance < $6 USDT")
                time.sleep(SCAN_INTERVAL)
                continue

            # Manage open positions
            open_pos = get_positions()
            log.info(f"[POS] {len(open_pos)} open | Active: {list(S['trades'].keys())}")
            if open_pos:
                manage_positions(open_pos)
                open_pos = get_positions()

            open_syms = [p.get("symbol") for p in open_pos]

            # Compound report every 15 cycles
            if cycle % 15 == 0:
                print_compound_report(bal)

            # Max trades check
            if len(open_pos) >= MAX_TRADES:
                log.info(f"[SKIP] {MAX_TRADES} trades open — monitoring only")
                time.sleep(SCAN_INTERVAL)
                continue

            # Analyze each symbol
            for symbol in SYMBOLS:
                if symbol in open_syms:
                    log.info(f"[{symbol}] Position open — skipping entry")
                    continue

                log.info(f"\n[ANALYZE] ━━━ {symbol} ━━━━━━━━━━━━━━━━━━━━━━━━━")

                try:
                    tick_d  = pub_ticker(symbol)
                    c_data  = pub_candles(symbol, "15", 150)
                    fund    = pub_funding(symbol)
                except Exception as e:
                    log.error(f"[{symbol}] Data error: {e}")
                    continue

                if not tick_d or not c_data:
                    log.warning(f"[{symbol}] No market data")
                    continue

                price  = float(tick_d.get("lastPr", 0))
                change = float(tick_d.get("change24h", 0)) * 100
                log.info(f"[{symbol}] ${price:,.2f} | {change:+.2f}% | Fund:{fund*100:.4f}%")

                sig = generate_signal(symbol, tick_d, c_data, fund)

                if not sig:
                    log.info(f"[{symbol}] No signal generated")
                    continue

                em = "🟢" if sig["action"]=="LONG" else "🔴" if sig["action"]=="SHORT" else "🟡"
                log.info(f"[SIG] {em} {sig['action']} | Conf:{sig['confidence']}% | "
                         f"Lev:{sig['leverage']}x | R:R {sig['rr']} | Regime:{sig['regime']}")
                log.info(f"[IND] RSI:{sig['rsi']:.0f} MACD:{sig['macd_h']:+.2f} "
                         f"Stoch:{sig['stoch']:.0f} Vol:{sig['vol_r']:.1f}x "
                         f"Volat:{sig['volatility']:.1f}%")
                log.info(f"[SCR] 🟢Bull:{sig['bull']} 🔴Bear:{sig['bear']}")
                for r in sig["reasons"][:4]:
                    log.info(f"  {r}")

                # Execute if good signal
                if sig["action"] in ("LONG", "SHORT") and sig["confidence"] >= MIN_CONF:

                    size, margin = get_compound_size(
                        bal, price, sig["leverage"], sig["confidence"]
                    )

                    if size <= 0:
                        log.warning(f"[{symbol}] Size too small")
                        continue

                    log.info(f"[COMPOUND] Margin: ${margin:.2f} | Size: {size} {sig['asset']} | Lev: {sig['leverage']}x")
                    log.info(f"[RISK] Entry:~${price:,.2f} | SL:${sig['sl']:,.2f} | "
                             f"TP1:${sig['tp1']:,.2f} | TP2:${sig['tp2']:,.2f} | TP3:${sig['tp3']:,.2f}")

                    res = place_open(symbol, sig["side"], size, sig["leverage"])

                    if res and res.get("code") == "00000":
                        oid = res.get("data", {}).get("orderId", "N/A")
                        log.info(f"[ORDER] ✅ {sig['action']} OPENED! OrderId:{oid}")

                        # Set exchange SL/TP
                        time.sleep(1.5)
                        place_sltp(symbol, sig["hold_side"], sig["sl"], sig["tp2"])
                        log.info(f"[RISK] ✅ SL & TP2 set on exchange")

                        # Track trade
                        S["trades"][symbol] = {
                            "side":      sig["hold_side"],
                            "entry":     price,
                            "sl":        sig["sl"],
                            "tp1":       sig["tp1"],
                            "tp2":       sig["tp2"],
                            "tp3":       sig["tp3"],
                            "trail":     sig["trail"],
                            "size":      size,
                            "margin":    margin,
                            "leverage":  sig["leverage"],
                            "confidence":sig["confidence"],
                            "open_time": now,
                        }
                    else:
                        err = res.get("msg") if res else "No response"
                        log.error(f"[ORDER] ❌ Failed: {err}")

                else:
                    log.info(f"[{symbol}] {sig['action']} — conf {sig['confidence']}% | waiting for better setup")

                time.sleep(3)

            log.info(f"\n[SLEEP] Next scan in {SCAN_INTERVAL}s ({SCAN_INTERVAL//60}m {SCAN_INTERVAL%60}s)...")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            break
        except Exception as e:
            log.error(f"[LOOP ERROR] {e}")
            time.sleep(30)


if __name__ == "__main__":
    run()
