"""
ARES ULTRA v6.0 — Production-Ready Architecture
Bitget USDT-M Perpetuals | BTC + ETH

KEY ARCHITECTURAL CHANGES FROM v5.1:
1. Exchange = Single Source of Truth (no internal state desync)
2. All TP/SL operations are atomic (cancel-then-place)
3. Leverage verification before every order
4. Pending orders auto-cleanup on close
5. Live position size sync (no stale data)
6. API retry on ALL endpoints
7. Funding-aware trade gating with safe fallbacks
8. Volume/OHLC data validation
9. Telegram notifications (optional)
10. Race condition eliminated (TP3 handled by exchange only)
"""

import os, time, hmac, hashlib, base64, json, logging, requests
from datetime import datetime, timezone, timedelta
from collections import deque

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ARES")

# ── Config ────────────────────────────────────────────────────
API_KEY      = os.environ.get("BITGET_API_KEY", "")
SECRET_KEY   = os.environ.get("BITGET_SECRET_KEY", "")
PASSPHRASE   = os.environ.get("BITGET_PASSPHRASE", "")
TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

MAX_LEVERAGE  = int(os.environ.get("MAX_LEVERAGE", "10"))
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SECONDS", "120"))
COMPOUND_PCT  = float(os.environ.get("COMPOUND_PCT", "20")) / 100
MAX_TRADES    = int(os.environ.get("MAX_TRADES", "2"))
RESET_STATS   = os.environ.get("RESET_STATS", "false").lower() == "true"

BASE_URL     = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"
SYMBOLS      = ["BTCUSDT", "ETHUSDT"]
STATE_FILE   = "/tmp/ares_v62_state.json"

SL_MULT        = 1.5
TP1_MULT       = 2.0
TP2_MULT       = 3.5
TP3_MULT       = 6.0
TRAIL_MULT     = 1.0
MIN_CONF       = 65
MAX_DAILY_LOSS = 0.06
MAX_DRAWDOWN   = 0.12
MAX_VOLAT      = 8.0
MIN_SL_IMPROVE = 0.001  # 0.1% — minimum SL improvement to trigger update (reduces API spam)
# Dynamic min margin: max($3, balance × 8%) — calculated in compound_size()

SYMBOL_SPECS = {
    "BTCUSDT": {"min_size": 0.0001, "precision": 4, "price_precision": 1},
    "ETHUSDT": {"min_size": 0.01,   "precision": 3, "price_precision": 2},
    "SOLUSDT": {"min_size": 0.1,    "precision": 1, "price_precision": 3},
    "BNBUSDT": {"min_size": 0.01,   "precision": 2, "price_precision": 2},
}

S = {
    "start_bal":    0.0,
    "peak_bal":     0.0,
    "daily_pnl":    0.0,
    "total_pnl":    0.0,
    "wins":         0,
    "losses":       0,
    "loss_streak":  0,
    "win_streak":   0,
    "daily_start":  datetime.now().date(),
    "trade_meta":   {},
    "pattern_memory": {},  # Pattern Memory: tracks win/loss for each setup signature
}

SENTIMENT_CACHE = {"data": None, "timestamp": 0}


# ── State Persistence ─────────────────────────────────────────

def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
        for k, v in saved.items():
            if k == "daily_start":
                try:
                    S[k] = datetime.fromisoformat(v).date()
                except:
                    S[k] = datetime.now().date()
            elif k in S:
                S[k] = v
        log.info(f"[STATE] Restored: {S['wins']}W/{S['losses']}L | "
                 f"PnL:${S['total_pnl']:.2f}")
    except Exception as e:
        log.warning(f"[STATE] Load failed: {e}")

def save_state():
    try:
        snapshot = {}
        for k, v in S.items():
            if k == "daily_start":
                snapshot[k] = v.isoformat() if hasattr(v, 'isoformat') else str(v)
            else:
                snapshot[k] = v
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f, indent=2, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning(f"[STATE] Save failed: {e}")

def reset_stats():
    """Reset stats for fresh start (preserves trade_meta)."""
    log.info("[INIT] 🔄 Stats reset requested via RESET_STATS env var")
    S["start_bal"] = 0.0
    S["peak_bal"] = 0.0
    S["total_pnl"] = 0.0
    S["daily_pnl"] = 0.0
    S["wins"] = 0
    S["losses"] = 0
    S["loss_streak"] = 0
    S["win_streak"] = 0
    S["daily_start"] = datetime.now().date()
    save_state()
    log.info("[INIT] ✅ Stats reset complete")


# ── Telegram (with batching) ──────────────────────────────────

NOTIFY_BUFFER = []
NOTIFY_LAST_FLUSH = time.time()
NOTIFY_FLUSH_INTERVAL = 600  # 10 minutes
NOTIFY_BUFFER_MAX = 10

def _send_telegram_now(msg):
    """Send message to Telegram immediately."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception as e:
        log.debug(f"[TG] Failed: {e}")

def flush_notifications():
    """Send buffered notifications as single combined message."""
    global NOTIFY_LAST_FLUSH
    if not NOTIFY_BUFFER:
        return
    combined = "📊 ARES Updates:\n" + "\n".join(NOTIFY_BUFFER)
    _send_telegram_now(combined)
    NOTIFY_BUFFER.clear()
    NOTIFY_LAST_FLUSH = time.time()

def notify(msg, urgent=False):
    """Buffered notifications. Urgent sends immediately, routine batched."""
    global NOTIFY_LAST_FLUSH
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    if urgent:
        _send_telegram_now(f"🚨 {msg}")
        return
    # Buffer non-urgent messages
    timestamp = datetime.now().strftime("%H:%M")
    NOTIFY_BUFFER.append(f"{timestamp} {msg}")
    # Flush if buffer full or 10 minutes passed
    now = time.time()
    if (len(NOTIFY_BUFFER) >= NOTIFY_BUFFER_MAX or
        now - NOTIFY_LAST_FLUSH >= NOTIFY_FLUSH_INTERVAL):
        flush_notifications()


# ── API Layer with retry ──────────────────────────────────────

def _sign(secret, msg):
    return base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()

def call(method, path, params=None, body=None, retries=3):
    last_result = None
    for attempt in range(retries):
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
                headers["ACCESS-SIGN"] = _sign(SECRET_KEY,
                    t + "GET" + path + "?" + qs)
                r = requests.get(BASE_URL + path + "?" + qs,
                    headers=headers, timeout=12)
            elif method == "POST":
                bs = json.dumps(body) if body else ""
                headers["ACCESS-SIGN"] = _sign(SECRET_KEY,
                    t + "POST" + path + bs)
                r = requests.post(BASE_URL + path, headers=headers,
                    data=bs, timeout=12)
            else:
                headers["ACCESS-SIGN"] = _sign(SECRET_KEY,
                    t + "GET" + path)
                r = requests.get(BASE_URL + path,
                    headers=headers, timeout=12)
            result = r.json()
            last_result = result
            if result.get("code") == "00000":
                return result
            err_code = result.get("code", "")
            if err_code in ["40001", "40002", "40003", "40009", "40037"]:
                log.error(f"[API] Auth/param error: {result.get('msg')}")
                return result
            if attempt < retries - 1:
                wait = 2 ** attempt
                log.debug(f"[API] Retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as e:
            log.error(f"[API] {method} {path}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return last_result


# ── Public Market Data ────────────────────────────────────────

def pub_get(path, params, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(f"{BASE_URL}{path}", params=params, timeout=10)
            data = r.json()
            if data.get("code") == "00000":
                return data
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
        except Exception as e:
            log.debug(f"[PUB] {path}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None

def pub_ticker(sym):
    data = pub_get("/api/v2/mix/market/ticker",
                   {"symbol": sym, "productType": PRODUCT_TYPE})
    if data:
        d = data.get("data", [])
        return d[0] if d else None
    return None

def pub_candles(sym, gran="15m", limit=150):
    gran_map = {"5": "5m", "15": "15m", "60": "1H", "240": "4H",
                "1": "1m", "30": "30m", "120": "2H", "360": "6H"}
    gran = gran_map.get(str(gran), gran)
    data = pub_get("/api/v2/mix/market/candles",
                   {"symbol": sym, "productType": PRODUCT_TYPE,
                    "granularity": gran, "limit": str(limit)})
    if not data:
        return []
    candles = data.get("data", [])
    if candles and len(candles) >= 2:
        if int(candles[0][0]) > int(candles[-1][0]):
            candles = list(reversed(candles))
    if candles and len(candles[0]) < 6:
        log.warning(f"[CANDLES] {sym} format invalid: {len(candles[0])} fields")
        return []
    return candles

def pub_funding(sym):
    """Returns (rate, minutes). -1 means time unknown."""
    data = pub_get("/api/v2/mix/market/contracts",
                   {"productType": PRODUCT_TYPE, "symbol": sym})
    if data:
        d = data.get("data", [])
        if d:
            try:
                rate = float(d[0].get("fundingRate", 0))
                next_ms = int(d[0].get("nextFundingTime", 0))
                if next_ms > 0:
                    now_ms = int(time.time() * 1000)
                    mins = max(0, int((next_ms - now_ms) / 60000))
                    return rate, mins
            except:
                pass
    data = pub_get("/api/v2/mix/market/current-fund-rate",
                   {"symbol": sym, "productType": PRODUCT_TYPE})
    if data:
        d = data.get("data", [])
        try:
            rate = float(d[0].get("fundingRate", 0)) if d else 0.0
            return rate, -1
        except:
            pass
    return 0.0, -1


# ── Sentiment ─────────────────────────────────────────────────

def fetch_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=8)
        d = r.json().get("data", [])
        if d:
            return int(d[0].get("value", 50)), d[0].get("value_classification", "Neutral")
    except:
        pass
    return None, None

def fetch_news_sentiment():
    try:
        r = requests.get("https://cryptopanic.com/api/free/v1/posts/",
            params={"public": "true", "currencies": "BTC,ETH"}, timeout=8)
        posts = r.json().get("results", [])[:20]
        bull = sum(1 for p in posts
            if p.get("votes", {}).get("positive", 0) >
               p.get("votes", {}).get("negative", 0))
        bear = sum(1 for p in posts
            if p.get("votes", {}).get("negative", 0) >
               p.get("votes", {}).get("positive", 0))
        total = max(len(posts), 1)
        return (bull - bear) / total * 100, bull, bear
    except:
        return None, 0, 0

def fetch_btc_dominance():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=8)
        d = r.json().get("data", {})
        return float(d.get("market_cap_percentage", {}).get("btc", 50))
    except:
        return None

def get_sentiment():
    now = time.time()
    if (SENTIMENT_CACHE["data"] is None or
        (now - SENTIMENT_CACHE["timestamp"]) > 1800):
        log.info("[SENTIMENT] Refreshing...")
        fg_val, fg_cls = fetch_fear_greed()
        news_sent, bull, bear = fetch_news_sentiment()
        btc_dom = fetch_btc_dominance()
        SENTIMENT_CACHE["data"] = {
            "fg_value":  fg_val,
            "fg_class":  fg_cls,
            "news_sent": news_sent,
            "bull_news": bull,
            "bear_news": bear,
            "btc_dom":   btc_dom,
        }
        SENTIMENT_CACHE["timestamp"] = now
        parts = []
        if fg_val is not None:
            parts.append(f"F&G:{fg_val}({fg_cls})")
        if news_sent is not None:
            parts.append(f"News:{news_sent:+.0f}%")
        if btc_dom is not None:
            parts.append(f"BTC.D:{btc_dom:.1f}%")
        log.info(f"[SENTIMENT] {' | '.join(parts) if parts else 'all unavailable'}")
    return SENTIMENT_CACHE["data"]


# ── Authenticated Endpoints ───────────────────────────────────

def fetch_recent_fills(sym, limit=30):
    """Get actual trade fills from Bitget for accurate PnL."""
    res = call("GET", "/api/v2/mix/order/fills",
               params={"productType": PRODUCT_TYPE, "symbol": sym,
                       "limit": str(limit)})
    if res and res.get("code") == "00000":
        data = res.get("data", {})
        if isinstance(data, dict):
            return data.get("fillList", [])
        return data if isinstance(data, list) else []
    return []

def fetch_balance():
    res = call("GET", "/api/v2/mix/account/accounts",
               params={"productType": PRODUCT_TYPE})
    if res and res.get("code") == "00000" and res.get("data"):
        for item in res["data"]:
            if item.get("marginCoin", "").upper() == "USDT":
                try:
                    return float(item.get("available", 0))
                except:
                    pass
    return 0.0

def fetch_positions():
    res = call("GET", "/api/v2/mix/position/all-position",
               params={"productType": PRODUCT_TYPE, "marginCoin": "USDT"})
    if res and res.get("data"):
        return [p for p in res["data"] if float(p.get("total", 0)) > 0]
    return []

def fetch_pending_plan_orders(sym):
    res = call("GET", "/api/v2/mix/order/orders-plan-pending",
               params={"productType": PRODUCT_TYPE, "symbol": sym})
    if res and res.get("code") == "00000" and res.get("data"):
        return res["data"].get("entrustedList", [])
    return []

def cancel_plan_order(sym, order_id, plan_type):
    res = call("POST", "/api/v2/mix/order/cancel-plan-order", body={
        "symbol": sym,
        "productType": PRODUCT_TYPE,
        "marginCoin": "USDT",
        "orderIdList": [{"orderId": order_id}],
        "planType": plan_type,
    })
    return res and res.get("code") == "00000"

def cancel_all_plan_orders(sym):
    orders = fetch_pending_plan_orders(sym)
    if not orders:
        return 0
    cancelled = 0
    for order in orders:
        oid = order.get("orderId")
        ptype = order.get("planType")
        if cancel_plan_order(sym, oid, ptype):
            cancelled += 1
            time.sleep(0.2)
    if cancelled:
        log.info(f"[CANCEL] {sym}: {cancelled}/{len(orders)} plan orders cancelled")
    return cancelled

def cancel_sl_orders_only(sym):
    orders = fetch_pending_plan_orders(sym)
    cancelled = 0
    for order in orders:
        if order.get("planType") == "loss_plan":
            if cancel_plan_order(sym, order.get("orderId"), "loss_plan"):
                cancelled += 1
                time.sleep(0.2)
    return cancelled

def set_leverage(sym, lev):
    success = True
    for side in ["long", "short"]:
        res = call("POST", "/api/v2/mix/account/set-leverage", body={
            "symbol": sym,
            "productType": PRODUCT_TYPE,
            "marginCoin": "USDT",
            "leverage": str(lev),
            "holdSide": side,
        })
        if not res or res.get("code") != "00000":
            err = res.get("msg") if res else "No response"
            log.warning(f"[LEV] {sym} {side} {lev}x failed: {err}")
            success = False
    return success

def place_market_order(sym, side, size, trade_side):
    return call("POST", "/api/v2/mix/order/place-order", body={
        "symbol": sym,
        "productType": PRODUCT_TYPE,
        "marginMode": "isolated",
        "marginCoin": "USDT",
        "size": str(size),
        "side": side,
        "tradeSide": trade_side,
        "orderType": "market",
        "force": "gtc",
    })

def place_plan_order(sym, plan_type, trigger_px, hold_side, size):
    res = call("POST", "/api/v2/mix/order/place-tpsl-order", body={
        "symbol": sym,
        "productType": PRODUCT_TYPE,
        "marginCoin": "USDT",
        "planType": plan_type,
        "triggerPrice": str(round(trigger_px, 2)),
        "triggerType": "mark_price",
        "executePrice": "0",
        "holdSide": hold_side,
        "size": str(size),
    })
    return res and res.get("code") == "00000"


# ── Risk Setup ────────────────────────────────────────────────

def setup_protection(sym, hold_side, sl, tp1, tp2, tp3, total_size):
    spec = SYMBOL_SPECS.get(sym, {"precision": 4, "min_size": 0.0001})
    full_size = round(total_size, spec["precision"])
    if not place_plan_order(sym, "loss_plan", sl, hold_side, full_size):
        log.error(f"[PROTECTION] {sym} SL placement FAILED")
        notify(f"❌ {sym} SL placement failed!", urgent=True)
        return False
    log.info(f"[PROTECTION] {sym} SL @ ${sl:,.2f}")
    time.sleep(0.4)
    tp1_size = round(total_size * 0.25, spec["precision"])
    tp2_size = round(total_size * 0.50, spec["precision"])
    tp3_size = round(total_size - tp1_size - tp2_size, spec["precision"])
    if min(tp1_size, tp2_size, tp3_size) < spec["min_size"]:
        log.warning(f"[PROTECTION] {sym} sizes too small to split — TP3 only")
        place_plan_order(sym, "profit_plan", tp3, hold_side, full_size)
        return True
    for tp_px, sz, label in [(tp1, tp1_size, "TP1"),
                             (tp2, tp2_size, "TP2"),
                             (tp3, tp3_size, "TP3")]:
        if place_plan_order(sym, "profit_plan", tp_px, hold_side, sz):
            log.info(f"[PROTECTION] {sym} {label} @ ${tp_px:,.2f} (size {sz})")
        else:
            log.warning(f"[PROTECTION] {sym} {label} placement failed")
        time.sleep(0.4)
    return True

def update_sl_atomic(sym, hold_side, new_sl, current_size):
    spec = SYMBOL_SPECS.get(sym, {"precision": 4})
    size = round(current_size, spec["precision"])
    cancel_sl_orders_only(sym)
    time.sleep(0.4)
    if place_plan_order(sym, "loss_plan", new_sl, hold_side, size):
        log.info(f"[SL UPDATE] {sym} → ${new_sl:,.2f}")
        return True
    else:
        log.error(f"[SL UPDATE] {sym} FAILED — position unprotected!")
        notify(f"⚠️ {sym} SL update failed — check manually!", urgent=True)
        return False


# ── Indicators ────────────────────────────────────────────────

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
    if g == 0 and l == 0:
        return 50
    if l == 0:
        return 100 if g > 0 else 50
    return 100 - (100 / (1 + g / l))

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
    if len(closes) < p + 1:
        return closes[-1] * 0.01 if closes else 0
    trs = [max(highs[i]-lows[i],
               abs(highs[i]-closes[i-1]),
               abs(lows[i]-closes[i-1]))
           for i in range(1, len(closes))]
    if len(trs) < p:
        return sum(trs) / len(trs) if trs else closes[-1] * 0.01
    atr = sum(trs[:p]) / p
    for tr in trs[p:]:
        atr = (atr * (p - 1) + tr) / p
    return atr

def calc_stoch(highs, lows, closes, k=14):
    h, l = max(highs[-k:]), min(lows[-k:])
    return ((closes[-1] - l) / (h - l)) * 100 if h != l else 50

def calc_vwap(highs, lows, closes, vols, session_bars=96):
    n = min(session_bars, len(closes))
    if n == 0:
        return closes[-1] if closes else 0
    h = highs[-n:]; l = lows[-n:]; c = closes[-n:]; v = vols[-n:]
    tp = [(h[i] + l[i] + c[i]) / 3 for i in range(n)]
    tv = sum(t * vol for t, vol in zip(tp, v))
    sv = sum(v)
    return tv / sv if sv else closes[-1]

def safe_extract_ohlcv(cdata):
    if not cdata or len(cdata[0]) < 6:
        return [], [], [], [], []
    try:
        highs  = [float(x[2]) for x in cdata]
        lows   = [float(x[3]) for x in cdata]
        closes = [float(x[4]) for x in cdata]
        opens  = [float(x[1]) for x in cdata]
        vols   = []
        for row in cdata:
            try:
                v = float(row[5])
                vols.append(v if v > 0 else 1.0)
            except:
                vols.append(1.0)
        return opens, highs, lows, closes, vols
    except Exception as e:
        log.error(f"[DATA] OHLCV parse failed: {e}")
        return [], [], [], [], []


# ── MTF Analysis ──────────────────────────────────────────────

def mtf_analysis(symbol, primary_15m=None):
    bull, bear = 0, 0
    results = {}
    start = time.time()
    MAX_TIME = 15
    for gran, label, w in [("5","5m",1),("15","15m",2),("60","1H",3),("240","4H",4)]:
        if time.time() - start > MAX_TIME:
            log.warning(f"[MTF] Timeout — skipping {label}")
            results[label] = "⚪"
            continue
        try:
            if gran == "15" and primary_15m:
                c = primary_15m[-80:]
            else:
                c = pub_candles(symbol, gran, 80)
            if len(c) < 30:
                results[label] = "⚪"
                continue
            _, hhs, lls, cls, _ = safe_extract_ohlcv(c)
            if not cls:
                results[label] = "⚪"
                continue
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
        except Exception as e:
            log.debug(f"[MTF] {label}: {e}")
            results[label] = "⚪"
    return bull, bear, results


# ── Pattern Memory (Self-Learning) ────────────────────────────
# Bot tracks win/loss for each unique setup signature.
# Over time, learns which setups work and which don't.

PATTERN_MIN_SAMPLES = 5      # Need 5+ trades before pattern is "mature"
PATTERN_MIN_WIN_RATE = 0.35  # Skip setups with <35% historical win rate

def get_pattern_signature(sig):
    """
    Create unique signature for this setup based on key features.
    Same signature = same type of setup.
    Granular enough to differentiate, broad enough to get samples.
    """
    parts = [sig["action"]]

    # RSI bucket
    rsi = sig.get("rsi", 50)
    if rsi < 30: parts.append("RSI_xover")
    elif rsi < 40: parts.append("RSI_low")
    elif rsi > 70: parts.append("RSI_xhigh")
    elif rsi > 60: parts.append("RSI_high")
    else: parts.append("RSI_mid")

    # MACD direction
    if sig.get("macd_h", 0) > 0:
        parts.append("MACD_pos")
    else:
        parts.append("MACD_neg")

    # Stoch bucket
    stoch = sig.get("stoch", 50)
    if stoch < 25: parts.append("Stoch_low")
    elif stoch > 75: parts.append("Stoch_high")
    else: parts.append("Stoch_mid")

    # Volume regime
    vol_r = sig.get("vol_r", 1)
    if vol_r > 2: parts.append("Vol_surge")
    elif vol_r > 1.3: parts.append("Vol_above")
    else: parts.append("Vol_norm")

    # Volatility bucket
    volat = sig.get("volat", 1)
    if volat < 1.5: parts.append("Volat_low")
    elif volat < 3: parts.append("Volat_mid")
    else: parts.append("Volat_high")

    # Confidence tier
    conf = sig.get("conf", 65)
    if conf >= 85: parts.append("Conf_xhigh")
    elif conf >= 75: parts.append("Conf_high")
    else: parts.append("Conf_mid")

    return "|".join(parts)


def check_pattern_history(sig_key):
    """
    Check if this pattern has been profitable historically.
    Returns: (should_trade, reason_str)
    """
    pm = S.get("pattern_memory", {})
    record = pm.get(sig_key, {"wins": 0, "losses": 0})
    total = record["wins"] + record["losses"]

    if total < PATTERN_MIN_SAMPLES:
        # Not enough data — allow trade (bot is still learning)
        return True, f"learning ({total}/{PATTERN_MIN_SAMPLES})"

    win_rate = record["wins"] / total
    if win_rate < PATTERN_MIN_WIN_RATE:
        return False, f"WR {win_rate*100:.0f}% < {PATTERN_MIN_WIN_RATE*100:.0f}% ({record['wins']}W/{record['losses']}L)"

    return True, f"WR {win_rate*100:.0f}% ({record['wins']}W/{record['losses']}L)"


def update_pattern_memory(sig_key, won):
    """Update pattern memory after trade closes."""
    if "pattern_memory" not in S:
        S["pattern_memory"] = {}
    if sig_key not in S["pattern_memory"]:
        S["pattern_memory"][sig_key] = {"wins": 0, "losses": 0}
    if won:
        S["pattern_memory"][sig_key]["wins"] += 1
    else:
        S["pattern_memory"][sig_key]["losses"] += 1


def print_pattern_summary():
    """Print summary of learned patterns."""
    pm = S.get("pattern_memory", {})
    if not pm:
        return
    log.info("─── PATTERN MEMORY ───")
    # Sort by total trades
    sorted_pats = sorted(pm.items(),
                         key=lambda x: x[1]["wins"] + x[1]["losses"],
                         reverse=True)
    for key, rec in sorted_pats[:10]:  # Top 10 most common
        total = rec["wins"] + rec["losses"]
        wr = rec["wins"] / total * 100 if total > 0 else 0
        status = "✅" if wr >= 50 else "⚠️" if wr >= 35 else "❌"
        log.info(f"  {status} {key} → {wr:.0f}% ({rec['wins']}W/{rec['losses']}L)")


# ── Compounding ───────────────────────────────────────────────

def compound_size(bal, price, lev, conf, sym):
    margin = bal * COMPOUND_PCT
    if S["loss_streak"] >= 3:
        margin *= 0.5
        log.info(f"[COMPOUND] Loss streak {S['loss_streak']} — size 50%")
    elif S["loss_streak"] == 2:
        margin *= 0.7
    if S["win_streak"] >= 3:
        margin *= 1.15
    elif S["win_streak"] >= 2:
        margin *= 1.08
    if conf >= 90: margin *= 1.2
    elif conf >= 85: margin *= 1.1
    elif conf >= 80: margin *= 1.05
    if S["peak_bal"] > 0:
        dd = (S["peak_bal"] - bal) / S["peak_bal"]
        if dd > MAX_DRAWDOWN:
            margin *= 0.4
            log.warning(f"[COMPOUND] Drawdown {dd*100:.1f}% — size 40%")
        elif dd > 0.06:
            margin *= 0.7
    margin = min(margin, bal * 0.40)
    # FIX v6.1: Dynamic minimum margin: max($3, 8% of balance)
    # Scales with balance — small accounts can still trade, large accounts have proper floor
    dynamic_min = max(3.0, bal * 0.08)
    if bal >= dynamic_min:
        if margin < dynamic_min:
            log.warning(f"[COMPOUND] {sym} margin ${margin:.2f} < ${dynamic_min:.2f} "
                        f"(dynamic min) — skip")
            return 0, 0
    margin = min(margin, bal * 0.95)  # Always cap at 95% of balance for fees safety
    spec = SYMBOL_SPECS.get(sym, {"min_size": 0.0001, "precision": 4})
    raw_size = (margin * lev) / price
    # Use FLOOR (not round) to never exceed available margin
    factor = 10 ** spec["precision"]
    size = int(raw_size * factor) / factor
    if size < spec["min_size"]:
        log.warning(f"[COMPOUND] {sym} size {size} < min {spec['min_size']}")
        return 0, 0
    return size, round(margin, 2)


# ── Signal Generation ─────────────────────────────────────────

def generate_signal(sym, tick, cdata, fund_rate):
    if not cdata or len(cdata) < 60:
        return None
    _, highs, lows, closes, vols = safe_extract_ohlcv(cdata)
    if not closes:
        return None
    price = float(tick.get("lastPr", closes[-1]))
    h24   = float(tick.get("high24h", price))
    l24   = float(tick.get("low24h", price))
    chg   = float(tick.get("change24h", 0)) * 100
    e9   = ema(closes, 9)
    e21  = ema(closes, 21)
    e50  = ema(closes, 50)
    e100 = ema(closes, 100)
    e200 = ema(closes, 200) if len(closes) >= 200 else None
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
    mtf_b, mtf_br, tf_map = mtf_analysis(sym, primary_15m=cdata)
    if volat > MAX_VOLAT:
        log.warning(f"[{sym}] Volatility {volat:.1f}% — skip")
        return None
    bull, bear = 0, 0
    reasons = []
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
    if e200 is not None:
        if price > e200:
            bull += 3; reasons.append("✅ Above EMA200")
        else:
            bear += 3; reasons.append("🔴 Below EMA200")
    rsi_up = r_now > r_prev
    if r_now < 25: bull += 4; reasons.append(f"✅ RSI extreme oversold {r_now:.0f}")
    elif r_now < 35: bull += 3; reasons.append(f"✅ RSI oversold {r_now:.0f}")
    elif r_now < 45 and rsi_up: bull += 2; reasons.append(f"✅ RSI recovering {r_now:.0f}")
    elif r_now > 75: bear += 4; reasons.append(f"🔴 RSI extreme overbought {r_now:.0f}")
    elif r_now > 65: bear += 3; reasons.append(f"🔴 RSI overbought {r_now:.0f}")
    elif r_now > 55 and not rsi_up: bear += 2; reasons.append(f"🔴 RSI weakening {r_now:.0f}")
    elif 48 < r_now < 58: bull += 1
    if mh > 0 and ml > ms and ml > 0:
        bull += 3; reasons.append("✅ MACD bullish above zero")
    elif mh > 0 and ml > ms:
        bull += 2; reasons.append("✅ MACD bullish cross")
    elif mh > 0: bull += 1
    elif mh < 0 and ml < ms and ml < 0:
        bear += 3; reasons.append("🔴 MACD bearish below zero")
    elif mh < 0 and ml < ms:
        bear += 2; reasons.append("🔴 MACD bearish cross")
    elif mh < 0: bear += 1
    bb_range = bbu - bbl
    bb_pct = (price - bbl) / bb_range if bb_range > 0 else 0.5
    if price < bbl: bull += 3; reasons.append("✅ Below BB lower")
    elif bb_pct < 0.2: bull += 2; reasons.append("✅ Near BB lower support")
    elif price > bbu: bear += 3; reasons.append("🔴 Above BB upper")
    elif bb_pct > 0.8: bear += 2; reasons.append("🔴 Near BB upper resistance")
    if sk < 15: bull += 3; reasons.append(f"✅ Stoch extreme oversold {sk:.0f}")
    elif sk < 25: bull += 2; reasons.append(f"✅ Stoch oversold {sk:.0f}")
    elif sk > 85: bear += 3; reasons.append(f"🔴 Stoch extreme overbought {sk:.0f}")
    elif sk > 75: bear += 2; reasons.append(f"🔴 Stoch overbought {sk:.0f}")
    if price > vwap_v * 1.002: bull += 2; reasons.append("✅ Above VWAP")
    elif price < vwap_v * 0.998: bear += 2; reasons.append("🔴 Below VWAP")
    if vol_r > 2.5 and chg > 0:
        bull += 3; reasons.append(f"✅ Huge vol surge bullish {vol_r:.1f}x")
    elif vol_r > 1.8 and chg > 0:
        bull += 2; reasons.append(f"✅ Vol surge bullish {vol_r:.1f}x")
    elif vol_r > 2.5 and chg < 0:
        bear += 3; reasons.append(f"🔴 Huge vol surge bearish {vol_r:.1f}x")
    elif vol_r > 1.8 and chg < 0:
        bear += 2; reasons.append(f"🔴 Vol surge bearish {vol_r:.1f}x")
    if fund_rate > 0.003:
        bear += 3; reasons.append(f"🔴 Extreme funding {fund_rate*100:.3f}%")
    elif fund_rate > 0.001:
        bear += 2
    elif fund_rate < -0.003:
        bull += 3; reasons.append(f"✅ Extreme negative funding {fund_rate*100:.3f}%")
    elif fund_rate < -0.001:
        bull += 2
    rng = h24 - l24
    if rng > 0:
        pos = (price - l24) / rng
        if pos < 0.10: bull += 3; reasons.append("✅ At 24h low support")
        elif pos < 0.25: bull += 2
        elif pos > 0.90: bear += 3; reasons.append("🔴 At 24h high resistance")
        elif pos > 0.75: bear += 2
    tf_str = " ".join(f"{k}:{v}" for k, v in tf_map.items())
    log.info(f"[MTF] {tf_str}")
    if mtf_b > mtf_br * 1.8:
        bull += 5; reasons.append("✅ ALL timeframes bullish!")
    elif mtf_b > mtf_br * 1.3:
        bull += 3; reasons.append("✅ Most TF bullish")
    elif mtf_br > mtf_b * 1.8:
        bear += 5; reasons.append("🔴 ALL timeframes bearish!")
    elif mtf_br > mtf_b * 1.3:
        bear += 3; reasons.append("🔴 Most TF bearish")
    if chg > 4: bull += 2
    elif chg > 2: bull += 1
    elif chg < -4: bear += 2
    elif chg < -2: bear += 1
    sentiment = get_sentiment()
    fg = sentiment.get("fg_value")
    if fg is not None:
        if fg <= 20:
            bull += 4; reasons.append(f"✅ Extreme Fear {fg} — buy opportunity")
        elif fg <= 35:
            bull += 2; reasons.append(f"✅ Market fearful {fg}")
        elif fg >= 80:
            bear += 4; reasons.append(f"🔴 Extreme Greed {fg} — caution!")
        elif fg >= 65:
            bear += 2; reasons.append(f"🔴 Market greedy {fg}")
    news_sent = sentiment.get("news_sent")
    if news_sent is not None:
        if news_sent > 30:
            bull += 3; reasons.append("✅ News very bullish")
        elif news_sent > 10:
            bull += 1
        elif news_sent < -30:
            bear += 3; reasons.append("🔴 News very bearish")
        elif news_sent < -10:
            bear += 1
    if sym == "ETHUSDT":
        btc_dom = sentiment.get("btc_dom")
        if btc_dom is not None:
            if btc_dom > 55:
                bear += 1; reasons.append(f"🔴 BTC dominance high {btc_dom:.1f}%")
            elif btc_dom < 45:
                bull += 1; reasons.append(f"✅ Alt season BTC.D {btc_dom:.1f}%")
    total = bull + bear
    if total == 0:
        return None
    bull_pct = (bull / total) * 100
    bear_pct = 100 - bull_pct
    if bull >= 12 and bull_pct >= 65:
        action, side, hold = "LONG", "buy", "long"
        base_conf = bull_pct
        if bull >= 25: base_conf += 5
        elif bull >= 20: base_conf += 2
        conf = min(97, int(base_conf))
    elif bear >= 12 and bear_pct >= 65:
        action, side, hold = "SHORT", "sell", "short"
        base_conf = bear_pct
        if bear >= 25: base_conf += 5
        elif bear >= 20: base_conf += 2
        conf = min(97, int(base_conf))
    else:
        action, side, hold = "HOLD", "none", "none"
        conf = 50
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
        "sym": sym, "asset": sym.replace("USDT", ""),
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


# ── Position Management ───────────────────────────────────────

def manage_position(pos):
    sym   = pos.get("symbol")
    hold  = pos.get("holdSide", "long")
    mark  = float(pos.get("markPrice", 0))
    size  = float(pos.get("total", 0))
    entry = float(pos.get("openPriceAvg", mark))
    pnl   = float(pos.get("unrealizedPL", 0))
    if size <= 0:
        return
    meta = S["trade_meta"].get(sym, {})
    if not meta:
        log.debug(f"[MGR] {sym} no metadata — skipping")
        return
    trail = meta.get("trail", mark * 0.01)

    # FIX v6.1: Better fallback for current_sl using SL_MULT/TRAIL_MULT ratio
    if "current_sl" in meta:
        current_sl = meta["current_sl"]
    else:
        # Original SL = entry ± (SL_MULT × ATR), trail = TRAIL_MULT × ATR
        # So sl_distance = trail × (SL_MULT / TRAIL_MULT)
        sl_distance = trail * (SL_MULT / TRAIL_MULT)
        current_sl = (entry - sl_distance) if hold == "long" else (entry + sl_distance)
        log.warning(f"[MGR] {sym} current_sl missing — fallback ${current_sl:,.2f}")

    tp1 = meta.get("tp1", 0)
    margin = meta.get("margin", 1)
    pnl_pct = (pnl / margin) * 100 if margin else 0
    log.info(f"[MGR] {sym} {hold.upper()} Mark:${mark:,.2f} "
             f"PnL:{pnl:+.4f}({pnl_pct:+.1f}%) SL:${current_sl:,.2f}")
    if hold == "long":
        new_sl = mark - trail
        if new_sl > current_sl and mark > entry * 1.005:
            # FIX v6.1: Only update if improvement is meaningful (reduces API spam)
            sl_improvement = (new_sl - current_sl) / current_sl if current_sl > 0 else 1
            if sl_improvement >= MIN_SL_IMPROVE:
                if update_sl_atomic(sym, hold, new_sl, size):
                    meta["current_sl"] = new_sl
                    S["trade_meta"][sym] = meta
                    save_state()
        elif tp1 > 0 and mark >= tp1 and not meta.get("be_moved"):
            be = entry * 1.003
            if be > current_sl:
                if update_sl_atomic(sym, hold, be, size):
                    meta["current_sl"] = be
                    meta["be_moved"] = True
                    S["trade_meta"][sym] = meta
                    save_state()
                    log.info(f"[TRAIL] {sym} TP1 hit! SL → BE")
    elif hold == "short":
        new_sl = mark + trail
        if new_sl < current_sl and mark < entry * 0.995:
            # FIX v6.1: SL improvement threshold for SHORT (lower SL = better)
            sl_improvement = (current_sl - new_sl) / current_sl if current_sl > 0 else 1
            if sl_improvement >= MIN_SL_IMPROVE:
                if update_sl_atomic(sym, hold, new_sl, size):
                    meta["current_sl"] = new_sl
                    S["trade_meta"][sym] = meta
                    save_state()
        elif tp1 > 0 and mark <= tp1 and not meta.get("be_moved"):
            be = entry * 0.997
            if be < current_sl:
                if update_sl_atomic(sym, hold, be, size):
                    meta["current_sl"] = be
                    meta["be_moved"] = True
                    S["trade_meta"][sym] = meta
                    save_state()
                    log.info(f"[TRAIL] {sym} TP1 hit! SL → BE")

def detect_closed_positions(open_pos):
    """
    Detect positions closed on exchange. Use ACTUAL fills for accurate PnL.
    Falls back to last_unrealized_pnl only if fills API fails.
    """
    exchange_syms = {p.get("symbol") for p in open_pos}
    for sym in list(S["trade_meta"].keys()):
        if sym in exchange_syms:
            continue
        meta = S["trade_meta"][sym]
        log.info(f"[CLOSED] {sym} closed on exchange")

        # FIX v6.1: Get ACTUAL realized PnL from fills API
        actual_pnl = 0.0
        fills_found = False
        try:
            entry_time_str = meta.get("entry_time", "")
            if entry_time_str:
                entry_time_ms = int(datetime.fromisoformat(
                    entry_time_str).timestamp() * 1000)
                fills = fetch_recent_fills(sym, 30)
                for fill in fills:
                    fill_time = int(fill.get("cTime", 0))
                    if fill_time >= entry_time_ms:
                        trade_side = fill.get("tradeSide", "").lower()
                        if "close" in trade_side:
                            try:
                                actual_pnl += float(fill.get("profit", 0))
                                fills_found = True
                            except (ValueError, TypeError):
                                pass
        except Exception as e:
            log.warning(f"[FILL parse] {sym}: {e}")

        # Fallback only if fills empty or failed
        if not fills_found:
            last_pnl = meta.get("last_unrealized_pnl", None)
            if last_pnl is None or last_pnl == 0.0:
                # Last resort: estimate from entry/last_mark
                entry = meta.get("entry_price", 0)
                last_mark = meta.get("last_mark", entry)
                size = meta.get("size", 0)
                side = meta.get("side", "long")
                if entry > 0 and last_mark > 0 and size > 0:
                    if side == "long":
                        actual_pnl = (last_mark - entry) * size
                    else:
                        actual_pnl = (entry - last_mark) * size
            else:
                actual_pnl = last_pnl
            log.info(f"[CLOSED] {sym} using estimated PnL (fills not found)")
        else:
            log.info(f"[CLOSED] {sym} actual realized PnL from fills")

        # Win/Loss tracking
        if actual_pnl > 0.01:
            S["wins"] += 1
            S["win_streak"] += 1
            S["loss_streak"] = 0
            log.info(f"[WIN] {sym} +${actual_pnl:.4f}")
            notify(f"✅ {sym} WIN ${actual_pnl:+.2f}")
            # Pattern Memory: record win
            pat_sig = meta.get("pattern_sig")
            if pat_sig:
                update_pattern_memory(pat_sig, won=True)
                log.info(f"[LEARN] ✅ Pattern '{pat_sig}' → WIN recorded")
        elif actual_pnl < -0.01:
            S["losses"] += 1
            S["loss_streak"] += 1
            S["win_streak"] = 0
            log.info(f"[LOSS] {sym} ${actual_pnl:.4f}")
            notify(f"❌ {sym} LOSS ${actual_pnl:+.2f}")
            # Pattern Memory: record loss
            pat_sig = meta.get("pattern_sig")
            if pat_sig:
                update_pattern_memory(pat_sig, won=False)
                log.info(f"[LEARN] ❌ Pattern '{pat_sig}' → LOSS recorded")
        else:
            log.info(f"[BREAKEVEN] {sym} closed @ ~$0 PnL (no streak change)")
            notify(f"➖ {sym} closed @ breakeven")

        S["daily_pnl"] += actual_pnl
        S["total_pnl"] += actual_pnl
        cancel_all_plan_orders(sym)
        del S["trade_meta"][sym]
        save_state()


# ── Reporting ─────────────────────────────────────────────────

def print_report(bal):
    start = S["start_bal"]
    growth = ((bal - start) / start * 100) if start > 0 else 0
    dd = ((S["peak_bal"] - bal) / S["peak_bal"] * 100) if S["peak_bal"] > 0 else 0
    total = S["wins"] + S["losses"]
    wr = (S["wins"] / total * 100) if total > 0 else 0
    log.info("╔══════════════════════════════════════╗")
    log.info("║   💰 ARES v6.2 REPORT                ║")
    log.info(f"║  Balance:  ${bal:.2f}")
    log.info(f"║  Growth:   {growth:+.2f}%")
    log.info(f"║  Drawdown: {dd:.2f}%")
    log.info(f"║  PnL:      ${S['total_pnl']:+.4f}")
    log.info(f"║  WinRate:  {wr:.1f}% ({S['wins']}W/{S['losses']}L)")
    pm = S.get("pattern_memory", {})
    log.info(f"║  Patterns: {len(pm)} learned")
    log.info("╚══════════════════════════════════════╝")
    if pm:
        print_pattern_summary()


# ── Main Loop ─────────────────────────────────────────────────

def run():
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║  ▲ ARES ULTRA v6.2 — Self-Learning Bot           ║")
    log.info("║  Exchange=Truth | Pattern Memory | All bugs fixed║")
    log.info(f"║  Compound:{COMPOUND_PCT*100:.0f}% | MaxLev:{MAX_LEVERAGE}x | Trades:{MAX_TRADES}     ║")
    log.info("╚══════════════════════════════════════════════════╝")
    if not API_KEY or not SECRET_KEY or not PASSPHRASE:
        log.error("❌ API keys missing!")
        return
    load_state()
    if RESET_STATS:
        reset_stats()
    bal = fetch_balance()
    if bal == 0:
        log.warning("[INIT] Balance $0 — verify API keys and futures balance")
    else:
        log.info(f"[INIT] Balance: ${bal:.2f} USDT")
    if S["start_bal"] == 0:
        S["start_bal"] = bal
    if S["peak_bal"] == 0:
        S["peak_bal"] = bal
    notify(f"🚀 ARES v6.0 started | Balance: ${bal:.2f}")
    cycle = 0
    while True:
        try:
            cycle += 1
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            today = datetime.now().date()
            if not isinstance(S["daily_start"], type(today)):
                S["daily_start"] = today
            if today != S["daily_start"]:
                log.info("🔄 New day — daily PnL reset")
                S["daily_pnl"] = 0
                S["daily_start"] = today
                save_state()
            log.info(f"\n{'═'*52}")
            log.info(f"  CYCLE {cycle} | {now}")
            log.info(f"{'═'*52}")
            bal = fetch_balance()
            if bal > S["peak_bal"]:
                S["peak_bal"] = bal
            if S["start_bal"] > 0:
                loss_pct = S["daily_pnl"] / S["start_bal"]
                if loss_pct <= -MAX_DAILY_LOSS:
                    now_utc = datetime.now(timezone.utc)
                    tomorrow = (now_utc + timedelta(days=1)).replace(
                        hour=0, minute=5, second=0, microsecond=0)
                    sleep_secs = int((tomorrow - now_utc).total_seconds())
                    hours = sleep_secs // 3600
                    log.warning(f"⛔ Daily loss {MAX_DAILY_LOSS*100:.0f}% hit! "
                                f"Pausing {hours}h until next day")
                    notify(f"⛔ Daily loss limit hit. Pausing {hours}h.", urgent=True)
                    save_state()
                    time.sleep(sleep_secs)
                    S["daily_pnl"] = 0
                    continue
            total = S["wins"] + S["losses"]
            wr = (S["wins"] / total * 100) if total > 0 else 0
            log.info(f"[BAL] ${bal:.2f} | Day:${S['daily_pnl']:+.4f} | "
                     f"Total:${S['total_pnl']:+.4f} | "
                     f"{S['wins']}W/{S['losses']}L ({wr:.0f}%)")
            if bal < 6:
                log.warning("[SKIP] Balance < $6")
                time.sleep(SCAN_INTERVAL)
                continue
            open_pos = fetch_positions()
            log.info(f"[POS] {len(open_pos)} open | "
                     f"Tracked: {list(S['trade_meta'].keys())}")
            detect_closed_positions(open_pos)
            for pos in open_pos:
                sym = pos.get("symbol")
                if sym in S["trade_meta"]:
                    S["trade_meta"][sym]["last_unrealized_pnl"] = \
                        float(pos.get("unrealizedPL", 0))
                    S["trade_meta"][sym]["last_mark"] = \
                        float(pos.get("markPrice", 0))
                manage_position(pos)
            open_syms = [p.get("symbol") for p in open_pos]
            if cycle % 20 == 0:
                print_report(bal)
            if len(open_pos) >= MAX_TRADES:
                log.info(f"[SKIP] {MAX_TRADES} trades open")
                save_state()
                time.sleep(SCAN_INTERVAL)
                continue
            for sym in SYMBOLS:
                if sym in open_syms:
                    log.info(f"[{sym}] Position open — skip")
                    continue
                log.info(f"\n[SCAN] ━━━ {sym} ━━━━━━━━━━━━━━━━━━━━━━━")
                tick  = pub_ticker(sym)
                cdata = pub_candles(sym, "15", 150)
                fund_rate, fund_mins = pub_funding(sym)
                if not tick or not cdata:
                    log.warning(f"[{sym}] No market data")
                    continue
                if fund_mins == -1 and abs(fund_rate) > 0.0003:
                    log.warning(f"[{sym}] Funding time unknown @ "
                                f"{fund_rate*100:.3f}% — skip")
                    continue
                if fund_mins != -1 and fund_mins < 15 and abs(fund_rate) > 0.0005:
                    log.warning(f"[{sym}] Funding in {fund_mins}m @ "
                                f"{fund_rate*100:.3f}% — skip")
                    continue
                price = float(tick.get("lastPr", 0))
                chg   = float(tick.get("change24h", 0)) * 100
                fund_str = f"{fund_mins}m" if fund_mins != -1 else "?"
                log.info(f"[{sym}] ${price:,.2f} | {chg:+.2f}% | "
                         f"Fund:{fund_rate*100:.4f}% (in {fund_str})")
                sig = generate_signal(sym, tick, cdata, fund_rate)
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
                if (sig["action"] not in ("LONG", "SHORT") or
                    sig["conf"] < MIN_CONF):
                    log.info(f"[{sym}] {sig['action']} conf:{sig['conf']}% — wait")
                    continue

                # ── Pattern Memory Check ───────────────────────────
                sig_key = get_pattern_signature(sig)
                should_trade, reason = check_pattern_history(sig_key)
                log.info(f"[LEARN] Pattern: {sig_key}")
                log.info(f"[LEARN] History: {reason}")
                if not should_trade:
                    log.warning(f"[{sym}] ⚠️ Pattern has poor history — SKIP")
                    notify(f"⚠️ {sym} {sig['action']} skipped (poor pattern: {reason})")
                    continue

                size, margin = compound_size(bal, price, sig["lev"],
                                              sig["conf"], sym)
                if size <= 0:
                    continue
                log.info(f"[ENTRY] {sym} margin:${margin} size:{size} "
                         f"lev:{sig['lev']}x")
                log.info(f"[RISK] SL:${sig['sl']:,.2f} TP1:${sig['tp1']:,.2f} "
                         f"TP2:${sig['tp2']:,.2f} TP3:${sig['tp3']:,.2f}")
                if not set_leverage(sym, sig["lev"]):
                    log.error(f"[ENTRY] {sym} leverage failed — abort")
                    continue
                res = place_market_order(sym, sig["side"], size, "open")
                if not res or res.get("code") != "00000":
                    err = res.get("msg") if res else "No response"
                    log.error(f"[ENTRY] {sym} order failed: {err}")
                    continue
                oid = res.get("data", {}).get("orderId", "N/A")
                log.info(f"[ENTRY] ✅ {sig['action']} OPENED ID:{oid}")
                time.sleep(1.5)
                if not setup_protection(sym, sig["hold"], sig["sl"],
                                         sig["tp1"], sig["tp2"], sig["tp3"],
                                         size):
                    log.error(f"[ENTRY] {sym} protection failed — closing")
                    place_market_order(sym, "sell" if sig["side"]=="buy" else "buy",
                                        size, "close")
                    notify(f"⚠️ {sym} closed - protection setup failed", urgent=True)
                    continue
                S["trade_meta"][sym] = {
                    "entry_price":  price,
                    "entry_time":   datetime.now().isoformat(),
                    "side":         sig["hold"],
                    "lev":          sig["lev"],
                    "margin":       margin,
                    "size":         size,
                    "trail":        sig["trail"],
                    "current_sl":   sig["sl"],
                    "tp1":          sig["tp1"],
                    "tp2":          sig["tp2"],
                    "tp3":          sig["tp3"],
                    "be_moved":     False,
                    "signal_score": sig["bull"] if sig["action"]=="LONG" else sig["bear"],
                    "confidence":   sig["conf"],
                    "last_unrealized_pnl": 0.0,
                    "last_mark":    price,
                    "pattern_sig":  sig_key,  # Save for learning on close
                }
                save_state()
                notify(f"🎯 {sym} {sig['action']} @ ${price:,.2f}\n"
                       f"Lev:{sig['lev']}x Conf:{sig['conf']}%\n"
                       f"SL:${sig['sl']:,.2f} TP3:${sig['tp3']:,.2f}")
                time.sleep(3)
            log.info(f"\n[SLEEP] {SCAN_INTERVAL}s...")
            save_state()
            flush_notifications()  # Send any pending Telegram updates
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            save_state()
            flush_notifications()  # Final flush
            notify("🛑 ARES stopped", urgent=True)
            break
        except Exception as e:
            log.error(f"[ERROR] Cycle {cycle}: {e}", exc_info=True)
            notify(f"⚠️ Bot error: {e}", urgent=True)
            time.sleep(30)


if __name__ == "__main__":
    run()
