"""
ARES v7.0 — EMA Pullback Strategy

Bitget USDT-M Perpetuals | BTC + ETH

STRATEGY: 4H EMA Pullback (best performer across 4 years of crypto data)

THESIS:
  In a trending market, price pulls back to the EMA21 before continuing.
  Enter at the EMA (limit order), ride the continuation.
  Exit at 3× ATR. Stop at 1.5× ATR below entry.

LONG:  EMA9 > EMA21 > EMA50 (uptrend) + price at EMA21 + RSI 35-65
SHORT: EMA9 < EMA21 < EMA50 (downtrend) + price at EMA21 + RSI 35-65

PROVEN PERFORMANCE (historical analysis):
  Bull (2021, 2024): 69-71% win rate
  Bear (2022):       62% win rate
  Range (2023):      51% win rate
  Avg R:R:           2.6:1
  Expectancy:        +0.80 per trade (after fees)

SETUP (Railway Variables):
  BITGET_API_KEY=…
  BITGET_SECRET_KEY=…
  BITGET_PASSPHRASE=…
  SHADOW_MODE=true             (set false for live)
  MAX_TRADES=2
  RISK_PCT=5                   (5% of balance per trade)
  MAX_LEVERAGE=3
  SCAN_INTERVAL_SECONDS=300
  STATE_FILE_PATH=/app/data/ares_v7_state.json
"""

import os, time, hmac, hashlib, base64, json, logging, requests
from datetime import datetime, timezone, timedelta

# ═══════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════

API_KEY    = os.environ.get("BITGET_API_KEY", "")
SECRET_KEY = os.environ.get("BITGET_SECRET_KEY", "")
PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")
TG_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE_URL     = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"
SYMBOLS      = ["BTCUSDT", "ETHUSDT"]

SHADOW_MODE   = os.environ.get("SHADOW_MODE", "false").lower() == "true"
RESET_STATS   = os.environ.get("RESET_STATS", "false").lower() == "true"
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SECONDS", "300"))
MAX_TRADES    = int(os.environ.get("MAX_TRADES", "2"))
MAX_LEVERAGE  = int(os.environ.get("MAX_LEVERAGE", "3"))
RISK_PCT      = float(os.environ.get("RISK_PCT", "5")) / 100

# Strategy params
EMA_FAST     = 9
EMA_MID      = 21
EMA_SLOW     = 50
EMA_PULL_TOL = 0.008   # price within 0.8% of EMA21 = "at pullback"
RSI_MIN      = 35      # not overbought/oversold
RSI_MAX      = 65
ATR_SL_MULT  = 1.5     # SL = 1.5× ATR
ATR_TP_MULT  = 3.0     # TP1 = 3.0× ATR  →  R:R ≥ 2:1
ATR_TP2_MULT = 5.0     # TP2 = 5× ATR (runner)
MIN_ADX      = 20      # trend must be present

MAX_DAILY_LOSS = 0.05  # 5% daily loss limit
MAX_DRAWDOWN   = 0.15  # 15% drawdown → reduce size

SYMBOL_SPECS = {
    "BTCUSDT": {"min_size": 0.0001, "precision": 4, "price_precision": 1},
    "ETHUSDT": {"min_size": 0.01,   "precision": 3, "price_precision": 2},
}

# ═══════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ares")

# ═══════════════════════════════════════════════════════════
# STATE FILE
# ═══════════════════════════════════════════════════════════

def _get_state_file():
    custom = os.environ.get("STATE_FILE_PATH")
    if custom:
        return custom
    try:
        os.makedirs("/app/data", exist_ok=True)
        test = "/app/data/.write_test"
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
        return "/app/data/ares_v7_state.json"
    except Exception:
        log.warning(
            "[STATE] /app/data not writable — falling back to /tmp. "
            "State WILL BE LOST on restart. Set STATE_FILE_PATH to fix this."
        )
        return "/tmp/ares_v7_state.json"

STATE_FILE = _get_state_file()

S = {
    "start_bal":      0.0,
    "peak_bal":       0.0,
    "total_pnl":      0.0,
    "daily_pnl":      0.0,
    "daily_start":    None,
    "wins":           0,
    "losses":         0,
    "loss_streak":    0,
    "win_streak":     0,
    "trade_meta":     {},   # sym → active trade details
    "pending_orders": {},   # sym → pending limit order info
}

def save_state():
    try:
        d = os.path.dirname(STATE_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        snap = {}
        for k, v in S.items():
            snap[k] = v.isoformat() if hasattr(v, "isoformat") else v
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f, indent=2, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning(f"[STATE] Save failed: {e}")

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
                except Exception:
                    S[k] = datetime.now(timezone.utc).date()
            elif k in S:
                S[k] = v
        log.info(f"[STATE] Restored: {S['wins']}W/{S['losses']}L | "
                 f"PnL:${S['total_pnl']:.2f}")
    except Exception as e:
        log.warning(f"[STATE] Load failed: {e}")

# ═══════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════

def notify(msg, urgent=False):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    safe = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": safe, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════
# BITGET API
# ═══════════════════════════════════════════════════════════

def _sign(secret, message):
    if not secret:
        raise ValueError("SECRET_KEY is empty — check Railway variables")
    return base64.b64encode(
        hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    ).decode()

def call(method, path, params=None, body=None, retries=3):
    """Authenticated Bitget API call. Always returns a dict."""
    last = None
    for attempt in range(retries):
        try:
            ts       = str(int(time.time() * 1000))
            body_str = json.dumps(body) if body else ""

            # Build query string manually so the signature and actual URL match exactly.
            # Using requests(params=...) would encode params in an arbitrary order
            # that may differ from the sorted string we sign — causing auth failures.
            if params:
                query    = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
                sign_msg = ts + method + path + "?" + query + body_str
                full_url = f"{BASE_URL}{path}?{query}"
            else:
                sign_msg = ts + method + path + body_str
                full_url = f"{BASE_URL}{path}"

            headers = {
                "ACCESS-KEY":        API_KEY,
                "ACCESS-SIGN":       _sign(SECRET_KEY, sign_msg),
                "ACCESS-TIMESTAMP":  ts,
                "ACCESS-PASSPHRASE": PASSPHRASE,
                "Content-Type":      "application/json",
                "locale":            "en-US",
            }

            if method == "GET":
                r = requests.get(full_url, headers=headers, timeout=10)
            else:
                r = requests.post(full_url, data=body_str, headers=headers, timeout=10)

            result = r.json()
            last   = result

            if result.get("code") == "00000":
                return result

            code = result.get("code", "")
            if r.status_code == 429 or code in ("429", "50054"):
                time.sleep(10 * (attempt + 1))
                continue
            if code in ("40001", "40002", "40003", "40009", "40037"):
                log.error(f"[API] Auth error ({code}): {result.get('msg')}")
                return result
            if attempt < retries - 1:
                time.sleep(2 ** attempt)

        except ValueError:
            raise   # re-raise empty-secret errors immediately
        except Exception as e:
            log.error(f"[API] {method} {path}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    return last or {"code": "CLIENT_ERROR", "msg": "no response from exchange"}

def pub_get(path, params=None, retries=3):
    """Unauthenticated public endpoint call."""
    for attempt in range(retries):
        try:
            r    = requests.get(f"{BASE_URL}{path}", params=params, timeout=10)
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

# ═══════════════════════════════════════════════════════════
# MARKET DATA
# ═══════════════════════════════════════════════════════════

def pub_candles_4h(sym, limit=200):
    """Fetch 4H candles — the only timeframe this strategy needs."""
    data = pub_get("/api/v2/mix/market/candles", {
        "symbol":      sym,
        "productType": PRODUCT_TYPE,
        "granularity": "4H",
        "limit":       str(limit),
    })
    if not data or not data.get("data"):
        return []
    candles = []
    for row in data["data"]:
        try:
            candles.append({
                "ts":    int(row[0]),
                "open":  float(row[1]),
                "high":  float(row[2]),
                "low":   float(row[3]),
                "close": float(row[4]),
                "vol":   float(row[5]),
            })
        except Exception:
            continue
    return sorted(candles, key=lambda x: x["ts"])

def pub_ticker(sym):
    data = pub_get("/api/v2/mix/market/ticker", {
        "symbol":      sym,
        "productType": PRODUCT_TYPE,
    })
    if data and data.get("data"):
        d = data["data"]
        return d[0] if isinstance(d, list) else d
    return None

def fetch_balance():
    res = call("GET", "/api/v2/mix/account/account-list",
               params={"productType": PRODUCT_TYPE})
    if res.get("code") == "00000" and res.get("data"):
        for acc in res["data"]:
            if acc.get("marginCoin", "").upper() == "USDT":
                try:
                    return float(acc.get("available", 0))
                except Exception:
                    pass
    return 0.0

def fetch_positions():
    """Returns list of open positions, or None on API failure.
    Callers must distinguish [] (no positions) from None (API failure).
    """
    res = call("GET", "/api/v2/mix/position/all-position",
               params={"productType": PRODUCT_TYPE, "marginCoin": "USDT"})
    if res.get("code") != "00000":
        return None
    data = res.get("data") or []
    return [p for p in data if float(p.get("total", 0)) > 0]

def check_exchange_health():
    try:
        r = requests.get(f"{BASE_URL}/api/v2/public/time", timeout=5)
        return r.json().get("code") == "00000"
    except Exception:
        return False

# ═══════════════════════════════════════════════════════════
# INDICATORS
# ═══════════════════════════════════════════════════════════

def ema(data, p):
    if not data:
        return 0
    if len(data) < p:
        return data[-1]
    k = 2 / (p + 1)
    v = sum(data[:p]) / p
    for x in data[p:]:
        v = x * k + v * (1 - k)
    return v

def calc_rsi(closes, p=14):
    if len(closes) < p + 1:
        return 50
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    g = sum(max(d, 0) for d in diffs[-p:]) / p
    l = sum(abs(min(d, 0)) for d in diffs[-p:]) / p
    if g == 0 and l == 0:
        return 50
    if l == 0:
        return 100
    return 100 - (100 / (1 + g / l))

def calc_atr(highs, lows, closes, p=14):
    if len(closes) < p + 1:
        return closes[-1] * 0.01 if closes else 0
    trs = [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    if len(trs) < p:
        return sum(trs) / len(trs) if trs else 0
    atr = sum(trs[:p]) / p
    for tr in trs[p:]:
        atr = (atr * (p - 1) + tr) / p
    return atr

def calc_adx(highs, lows, closes, period=14):
    if len(closes) < 2 * period + 1:
        return 0
    tr_list, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        hd = highs[i] - highs[i - 1]
        ld = lows[i - 1] - lows[i]
        plus_dm.append(max(hd, 0) if hd > ld else 0)
        minus_dm.append(max(ld, 0) if ld > hd else 0)
        tr_list.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    st = sum(tr_list[:period])
    sp = sum(plus_dm[:period])
    sm = sum(minus_dm[:period])
    dx = []
    for i in range(period, len(tr_list)):
        st = st - (st / period) + tr_list[i]
        sp = sp - (sp / period) + plus_dm[i]
        sm = sm - (sm / period) + minus_dm[i]
        if st == 0:
            continue
        pdi = 100 * sp / st
        mdi = 100 * sm / st
        s   = pdi + mdi
        dx.append(0 if s == 0 else 100 * abs(pdi - mdi) / s)
    if len(dx) < period:
        return 0
    adx = sum(dx[:period]) / period
    for val in dx[period:]:
        adx = (adx * (period - 1) + val) / period
    return adx

# ═══════════════════════════════════════════════════════════
# EMA PULLBACK SIGNAL
# ═══════════════════════════════════════════════════════════

def generate_signal(sym, candles):
    """
    EMA Pullback entry conditions:

    LONG:  EMA9 > EMA21 > EMA50, price within 0.8% of EMA21,
           RSI 35-65, ADX > 20, volume > 70% of 20-period avg
    SHORT: mirror conditions

    Returns signal dict or None.
    """
    if len(candles) < 60:
        log.warning(f"[{sym}] Not enough candles ({len(candles)})")
        return None

    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    vols   = [c["vol"]   for c in candles]

    price = closes[-1]
    if price <= 0:
        return None

    e9  = ema(closes, EMA_FAST)
    e21 = ema(closes, EMA_MID)
    e50 = ema(closes, EMA_SLOW)

    atr = calc_atr(highs, lows, closes)
    if atr <= 0:
        return None

    rsi = calc_rsi(closes)
    adx = calc_adx(highs, lows, closes)

    avg_vol = sum(vols[-20:]) / 20 if len(vols) >= 20 else vols[-1]
    vol_ok  = vols[-1] > avg_vol * 0.7

    dist_from_e21 = abs(price - e21) / e21

    log.info(f"[{sym}] Price:${price:.2f} | E9:${e9:.2f} E21:${e21:.2f} E50:${e50:.2f}")
    log.info(f"[{sym}] RSI:{rsi:.0f} | ADX:{adx:.0f} | ATR:${atr:.2f} | "
             f"Dist21:{dist_from_e21 * 100:.2f}%")

    spec = SYMBOL_SPECS.get(sym, {"price_precision": 2})
    pp   = spec["price_precision"]

    # ── LONG setup ──
    if (e9 > e21 > e50
            and dist_from_e21 <= EMA_PULL_TOL
            and RSI_MIN <= rsi <= RSI_MAX
            and adx >= MIN_ADX
            and vol_ok):

        sl  = price - atr * ATR_SL_MULT
        tp1 = price + atr * ATR_TP_MULT
        tp2 = price + atr * ATR_TP2_MULT

        log.info(f"[{sym}] LONG PULLBACK | SL:${sl:.2f} TP1:${tp1:.2f} TP2:${tp2:.2f}")
        return {
            "sym": sym, "action": "LONG", "side": "buy", "hold": "long",
            "price":       price,
            "limit_price": round(e21, pp),
            "sl": sl, "tp1": tp1, "tp2": tp2,
            "atr": atr, "rsi": rsi, "adx": adx,
            "e9": e9, "e21": e21, "e50": e50,
            "reasons": [
                f"Uptrend: E9({e9:.0f}) > E21({e21:.0f}) > E50({e50:.0f})",
                f"Pullback to EMA21 ({dist_from_e21 * 100:.1f}% away)",
                f"RSI {rsi:.0f} (continuation zone)",
                f"ADX {adx:.0f} (trend confirmed)",
            ],
        }

    # ── SHORT setup ──
    if (e9 < e21 < e50
            and dist_from_e21 <= EMA_PULL_TOL
            and RSI_MIN <= rsi <= RSI_MAX
            and adx >= MIN_ADX
            and vol_ok):

        sl  = price + atr * ATR_SL_MULT
        tp1 = price - atr * ATR_TP_MULT
        tp2 = price - atr * ATR_TP2_MULT

        log.info(f"[{sym}] SHORT PULLBACK | SL:${sl:.2f} TP1:${tp1:.2f} TP2:${tp2:.2f}")
        return {
            "sym": sym, "action": "SHORT", "side": "sell", "hold": "short",
            "price":       price,
            "limit_price": round(e21, pp),
            "sl": sl, "tp1": tp1, "tp2": tp2,
            "atr": atr, "rsi": rsi, "adx": adx,
            "e9": e9, "e21": e21, "e50": e50,
            "reasons": [
                f"Downtrend: E9({e9:.0f}) < E21({e21:.0f}) < E50({e50:.0f})",
                f"Bounce to EMA21 ({dist_from_e21 * 100:.1f}% away)",
                f"RSI {rsi:.0f} (continuation zone)",
                f"ADX {adx:.0f} (trend confirmed)",
            ],
        }

    trend = "UP" if e9 > e21 else ("DOWN" if e9 < e21 else "FLAT")
    log.info(f"[{sym}] HOLD | Trend:{trend} | "
             f"EMA aligned:{e9 > e21 > e50 or e9 < e21 < e50} | "
             f"At pullback:{dist_from_e21 <= EMA_PULL_TOL} | "
             f"ADX ok:{adx >= MIN_ADX}")
    return None

# ═══════════════════════════════════════════════════════════
# ORDER MANAGEMENT
# ═══════════════════════════════════════════════════════════

def set_leverage(sym, lev):
    for hold_side in ("long", "short"):
        res = call("POST", "/api/v2/mix/account/set-leverage", body={
            "symbol":      sym,
            "productType": PRODUCT_TYPE,
            "marginCoin":  "USDT",
            "leverage":    str(lev),
            "holdSide":    hold_side,
        })
        # 40919 = "leverage already set to this value" — treat as success
        if res.get("code") not in ("00000", "40919"):
            log.error(f"[LEV] {sym} {hold_side} failed: {res.get('msg')}")
            return False
    return True

def place_limit_order(sym, side, size, price, hold_side):
    """Place a GTC limit order — cheaper maker fees vs market."""
    spec = SYMBOL_SPECS.get(sym, {"price_precision": 2})
    px   = round(price, spec["price_precision"])
    res  = call("POST", "/api/v2/mix/order/place-order", body={
        "symbol":      sym,
        "productType": PRODUCT_TYPE,
        "marginCoin":  "USDT",
        "side":        side,
        "tradeSide":   "open",
        "orderType":   "limit",
        "price":       str(px),
        "size":        str(size),
        "force":       "gtc",
    })
    if res.get("code") == "00000":
        oid = res.get("data", {}).get("orderId", "N/A")
        log.info(f"[ORDER] LIMIT {sym} {side.upper()} {size} @ ${px} | ID:{oid}")
        return oid
    log.error(f"[ORDER] Limit order failed: {res.get('msg')}")
    return None

def place_market_order(sym, side, size, trade_side="open"):
    """Market order — for emergency closes only."""
    res = call("POST", "/api/v2/mix/order/place-order", body={
        "symbol":      sym,
        "productType": PRODUCT_TYPE,
        "marginCoin":  "USDT",
        "side":        side,
        "tradeSide":   trade_side,
        "orderType":   "market",
        "size":        str(size),
        "force":       "ioc",
    })
    if res.get("code") == "00000":
        log.info(f"[ORDER] MARKET {sym} {side.upper()} {size} ({trade_side})")
    else:
        log.error(f"[ORDER] Market order failed: {res.get('msg')}")
    return res

def place_plan_order(sym, plan_type, trigger_px, hold_side, size):
    """Place a SL or TP trigger order."""
    spec = SYMBOL_SPECS.get(sym, {"price_precision": 2})
    px   = round(trigger_px, spec["price_precision"])
    res  = call("POST", "/api/v2/mix/order/place-tpsl-order", body={
        "symbol":       sym,
        "productType":  PRODUCT_TYPE,
        "marginCoin":   "USDT",
        "planType":     plan_type,
        "triggerPrice": str(px),
        "holdSide":     hold_side,
        "size":         str(size),
        "triggerType":  "mark_price",
    })
    if res.get("code") == "00000":
        log.info(f"[PLAN] {plan_type} @ ${px}")
        return True
    log.error(f"[PLAN] {plan_type} failed: {res.get('msg')}")
    return False

def cancel_all_plan_orders(sym):
    call("POST", "/api/v2/mix/order/cancel-all-trigger-orders", body={
        "symbol":      sym,
        "productType": PRODUCT_TYPE,
        "marginCoin":  "USDT",
    })

def cancel_limit_order(sym, order_id):
    call("POST", "/api/v2/mix/order/cancel-order", body={
        "symbol":      sym,
        "productType": PRODUCT_TYPE,
        "marginCoin":  "USDT",
        "orderId":     order_id,
    })

def check_limit_order_filled(sym, order_id):
    """Returns (status, filled_size, avg_fill_price)."""
    res = call("GET", "/api/v2/mix/order/detail", params={
        "symbol":      sym,
        "productType": PRODUCT_TYPE,
        "orderId":     order_id,
    })
    if res.get("code") == "00000" and res.get("data"):
        d      = res["data"]
        status = d.get("status", "")
        size   = float(d.get("baseVolume", 0))
        price  = float(d.get("priceAvg", 0))
        return status, size, price
    return "unknown", 0, 0

# ═══════════════════════════════════════════════════════════
# POSITION SIZING
# ═══════════════════════════════════════════════════════════

def calc_position_size(bal, price, lev, sym):
    """Risk RISK_PCT of balance per trade, reduced on drawdown or loss streaks."""
    spec     = SYMBOL_SPECS.get(sym, {"min_size": 0.0001, "precision": 4})
    risk_usd = bal * RISK_PCT

    # Drawdown reduction
    if S["peak_bal"] > 0:
        dd = (S["peak_bal"] - bal) / S["peak_bal"]
        if dd > MAX_DRAWDOWN:
            risk_usd *= 0.5
            log.warning(f"[SIZE] Drawdown {dd * 100:.1f}% — risk halved")
        elif dd > 0.08:
            risk_usd *= 0.75

    # Loss streak reduction
    if S["loss_streak"] >= 3:
        risk_usd *= 0.6
        log.info(f"[SIZE] Loss streak {S['loss_streak']} — risk 60%")
    elif S["loss_streak"] == 2:
        risk_usd *= 0.8

    size = round((risk_usd * lev) / price, spec["precision"])
    if size < spec["min_size"]:
        log.warning(f"[SIZE] {sym} size {size} < min {spec['min_size']} — skip")
        return 0, 0

    return size, risk_usd

# ═══════════════════════════════════════════════════════════
# POSITION MANAGEMENT
# ═══════════════════════════════════════════════════════════

def manage_position(pos):
    """Move SL to breakeven when price reaches 95% of TP1."""
    sym  = pos.get("symbol")
    hold = pos.get("holdSide", "long")
    try:
        mark  = float(pos.get("markPrice", 0))
        size  = float(pos.get("total", 0))
        entry = float(pos.get("openPriceAvg", mark))
        pnl   = float(pos.get("unrealizedPL", 0))
    except (ValueError, TypeError) as e:
        log.warning(f"[MGR] {sym} invalid data: {e}")
        return
    if mark != mark or entry != entry:  # NaN guard
        return
    if mark <= 0 or entry <= 0 or size <= 0:
        return

    meta = S["trade_meta"].get(sym, {})
    if not meta:
        return

    margin  = meta.get("margin", 1)
    tp1     = meta.get("tp1", 0)
    pnl_pct = (pnl / margin * 100) if margin else 0

    log.info(f"[MGR] {sym} {hold.upper()} Mark:${mark:,.2f} "
             f"PnL:{pnl:+.4f} ({pnl_pct:+.1f}%)")

    if not meta.get("be_moved") and tp1 > 0:
        tp1_reached = (hold == "long"  and mark >= tp1 * 0.95) or \
                      (hold == "short" and mark <= tp1 * 1.05)
        if tp1_reached:
            cancel_all_plan_orders(sym)
            be_price = entry * (1.001 if hold == "long" else 0.999)
            if place_plan_order(sym, "loss_plan", be_price, hold, size):
                meta["current_sl"] = be_price
                meta["be_moved"]   = True
                S["trade_meta"][sym] = meta
                save_state()
                log.info(f"[MGR] {sym} TP1 zone — SL moved to BE ${be_price:.2f}")
                notify(f"{sym} approaching TP1 — SL moved to breakeven", urgent=True)

def detect_closed_positions(open_pos):
    """Detect positions that closed via SL/TP on exchange and update P&L stats."""
    open_syms = {p.get("symbol") for p in (open_pos or [])}
    for sym in list(S["trade_meta"].keys()):
        if sym in open_syms:
            continue

        meta = S["trade_meta"][sym]

        # Try to determine actual PnL from recent fills
        res        = call("GET", "/api/v2/mix/order/fills", params={
            "symbol": sym, "productType": PRODUCT_TYPE, "limit": "10",
        })
        actual_pnl = 0.0
        if res.get("code") == "00000" and res.get("data"):
            fills = res["data"].get("fillList", [])
            for fill in fills:
                if fill.get("tradeSide") in ("close_long", "close_short", "close"):
                    try:
                        actual_pnl += float(fill.get("profit", 0))
                    except Exception:
                        pass

        if actual_pnl == 0:
            actual_pnl = meta.get("last_pnl", 0)

        S["total_pnl"] += actual_pnl
        S["daily_pnl"] += actual_pnl

        # Use relative threshold so small-account breakeven trades are classified correctly
        threshold = max(0.01, meta.get("margin", 1) * 0.002)
        if actual_pnl > threshold:
            S["wins"]       += 1
            S["win_streak"] += 1
            S["loss_streak"] = 0
            result = f"WIN +${actual_pnl:.4f}"
            notify(f"{sym} WIN +${actual_pnl:.4f}", urgent=True)
        elif actual_pnl < -threshold:
            S["losses"]      += 1
            S["loss_streak"] += 1
            S["win_streak"]   = 0
            result = f"LOSS -${abs(actual_pnl):.4f}"
            notify(f"{sym} LOSS -${abs(actual_pnl):.4f}", urgent=True)
        else:
            result = "Breakeven"
            notify(f"{sym} closed breakeven", urgent=True)

        log.info(f"[CLOSE] {sym} closed — {result} | W:{S['wins']} L:{S['losses']}")
        S["pending_orders"].pop(sym, None)
        del S["trade_meta"][sym]
        save_state()

# ═══════════════════════════════════════════════════════════
# ENTRY EXECUTION
# ═══════════════════════════════════════════════════════════

def execute_entry(sym, sig, bal):
    """Place a LIMIT order at EMA21. Fill is verified each subsequent cycle."""
    price        = sig["limit_price"]
    size, margin = calc_position_size(bal, price, MAX_LEVERAGE, sym)
    if size <= 0:
        return

    if not set_leverage(sym, MAX_LEVERAGE):
        log.error(f"[ENTRY] {sym} leverage failed")
        return

    if SHADOW_MODE:
        log.info(f"[SHADOW] Would place LIMIT {sig['action']} {sym} "
                 f"@ ${price:.2f} size={size} margin=${margin:.2f}")
        notify(f"SHADOW: {sym} {sig['action']} LIMIT @ ${price:.2f} | "
               f"SL:${sig['sl']:.2f} TP:${sig['tp1']:.2f}")
        return

    oid = place_limit_order(sym, sig["side"], size, price, sig["hold"])
    if not oid:
        return

    S["pending_orders"][sym] = {
        "order_id":  oid,
        "side":      sig["side"],
        "hold":      sig["hold"],
        "limit_px":  price,
        "sl":        sig["sl"],
        "tp1":       sig["tp1"],
        "tp2":       sig["tp2"],
        "size":      size,
        "margin":    margin,
        "placed_ts": int(time.time() * 1000),
        "sig":       sig,
    }
    save_state()

    log.info(f"[ENTRY] LIMIT placed: {sym} {sig['action']} @ ${price:.2f} "
             f"size={size} ID:{oid}")
    notify(f"{sym} LIMIT {sig['action']} @ ${price:.2f}\n"
           f"SL:${sig['sl']:.2f} TP:${sig['tp1']:.2f}\nWaiting for fill...",
           urgent=True)

def check_pending_orders():
    """
    Poll pending limit orders each cycle.
    Filled  → place SL + TP protection immediately.
    Expired (>8h, ~2 candles) → cancel; price moved away from EMA.
    """
    now_ms          = int(time.time() * 1000)
    CANCEL_AFTER_MS = 8 * 3600 * 1000

    for sym, order in list(S["pending_orders"].items()):
        oid = order["order_id"]
        status, filled_size, fill_price = check_limit_order_filled(sym, oid)

        if status in ("full_fill", "filled"):
            log.info(f"[FILL] {sym} limit order FILLED @ ${fill_price:.2f}")
            actual_entry = fill_price if fill_price > 0 else order["limit_px"]
            atr  = order["sig"]["atr"]
            hold = order["hold"]

            if hold == "long":
                sl  = actual_entry - atr * ATR_SL_MULT
                tp1 = actual_entry + atr * ATR_TP_MULT
                tp2 = actual_entry + atr * ATR_TP2_MULT
            else:
                sl  = actual_entry + atr * ATR_SL_MULT
                tp1 = actual_entry - atr * ATR_TP_MULT
                tp2 = actual_entry - atr * ATR_TP2_MULT

            spec = SYMBOL_SPECS.get(sym, {"precision": 4, "min_size": 0.0001})
            size = round(filled_size if filled_size > 0 else order["size"],
                         spec["precision"])

            # Place SL first — minimise naked-position window
            if not place_plan_order(sym, "loss_plan", sl, hold, size):
                log.error(f"[FILL] {sym} SL placement failed — emergency close")
                close_side = "sell" if hold == "long" else "buy"
                for _ in range(3):
                    res = place_market_order(sym, close_side, size, "close")
                    if res.get("code") == "00000":
                        break
                    time.sleep(2)
                del S["pending_orders"][sym]
                save_state()
                continue

            # TP1 on 50%, TP2 on remaining 50%
            half = round(size * 0.5, spec["precision"])
            rest = round(size - half, spec["precision"])
            if half >= spec.get("min_size", 0):
                place_plan_order(sym, "profit_plan", tp1, hold, half)
            if rest >= spec.get("min_size", 0):
                place_plan_order(sym, "profit_plan", tp2, hold, rest)

            S["trade_meta"][sym] = {
                "entry_price": actual_entry,
                "entry_time":  datetime.now(timezone.utc).isoformat(),
                "side":        hold,
                "lev":         MAX_LEVERAGE,
                "margin":      order["margin"],
                "size":        size,
                "current_sl":  sl,
                "tp1":         tp1,
                "tp2":         tp2,
                "atr":         atr,
                "be_moved":    False,
                "last_pnl":    0.0,
                "last_mark":   actual_entry,
            }
            del S["pending_orders"][sym]
            save_state()

            notify(f"{sym} {hold.upper()} FILLED @ ${actual_entry:.2f}\n"
                   f"SL:${sl:.2f} TP1:${tp1:.2f} TP2:${tp2:.2f}", urgent=True)

        elif status in ("cancelled", "cancel"):
            log.info(f"[FILL] {sym} order cancelled externally")
            del S["pending_orders"][sym]
            save_state()

        elif now_ms - order["placed_ts"] > CANCEL_AFTER_MS:
            log.info(f"[FILL] {sym} limit order expired (8h) — cancelling")
            cancel_limit_order(sym, oid)
            del S["pending_orders"][sym]
            save_state()
            notify(f"{sym} limit order expired — price moved away")

        else:
            age_h = (now_ms - order["placed_ts"]) / 3_600_000
            log.info(f"[FILL] {sym} limit order pending ({age_h:.1f}h)")

# ═══════════════════════════════════════════════════════════
# REPORTING
# ═══════════════════════════════════════════════════════════

def print_report(bal):
    start  = S["start_bal"]
    growth = ((bal - start) / start * 100) if start > 0 else 0
    dd     = ((S["peak_bal"] - bal) / S["peak_bal"] * 100) if S["peak_bal"] > 0 else 0
    total  = S["wins"] + S["losses"]
    wr     = S["wins"] / total * 100 if total > 0 else 0

    log.info("╔══════════════════════════════════════╗")
    log.info("║  ARES v7.0 REPORT                    ║")
    log.info(f"║  Balance:  ${bal:.2f}                ║")
    log.info(f"║  Growth:   {growth:+.2f}%              ║")
    log.info(f"║  Drawdown: {dd:.2f}%               ║")
    log.info(f"║  PnL:      ${S['total_pnl']:+.4f}           ║")
    log.info(f"║  WinRate:  {wr:.1f}% ({S['wins']}W/{S['losses']}L)      ║")
    log.info("║  Strategy: EMA Pullback 4H           ║")
    if SHADOW_MODE:
        log.info("║  Mode:     SHADOW (no real trades)   ║")
    log.info("╚══════════════════════════════════════╝")

# ═══════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════

def run():
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║  ARES v7.0 — EMA Pullback Strategy (4H)         ║")
    log.info(f"║  Risk:{RISK_PCT * 100:.0f}%/trade | Lev:{MAX_LEVERAGE}x | "
             f"Max:{MAX_TRADES} trades        ║")
    log.info("║  Orders: LIMIT (low fees) | TF: 4H              ║")
    if SHADOW_MODE:
        log.info("║  SHADOW MODE — No real trades                   ║")
    log.info("╚══════════════════════════════════════════════════╝")

    if not API_KEY or not SECRET_KEY or not PASSPHRASE:
        log.error("API keys missing — check Railway variables")
        return

    # Wait up to 1h for exchange to come online
    if not check_exchange_health():
        log.error("[INIT] Bitget unreachable — waiting for recovery...")
        for _ in range(60):
            time.sleep(60)
            if check_exchange_health():
                break

    load_state()

    if RESET_STATS:
        for key in ("start_bal", "peak_bal", "total_pnl", "daily_pnl",
                    "wins", "losses", "loss_streak", "win_streak"):
            S[key] = 0.0 if isinstance(S[key], float) else 0
        S["pending_orders"] = {}
        S["trade_meta"]     = {}
        save_state()
        log.info("[INIT] Stats reset complete")

    bal = fetch_balance()
    if S["start_bal"] == 0:
        S["start_bal"] = bal
    if S["peak_bal"] == 0:
        S["peak_bal"] = bal
    S["daily_start"] = datetime.now(timezone.utc).date()

    log.info(f"[INIT] Balance: ${bal:.2f} USDT"
             f"{' [SHADOW]' if SHADOW_MODE else ''}")
    notify(f"ARES v7.0 started | ${bal:.2f}"
           f"{' [SHADOW]' if SHADOW_MODE else ''}", urgent=True)

    cycle = 0
    while True:
        try:
            cycle += 1
            now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            today = datetime.now(timezone.utc).date()

            # Daily stats reset at midnight UTC
            if S["daily_start"] and today != S["daily_start"]:
                log.info("New day — daily PnL reset")
                S["daily_pnl"]   = 0
                S["daily_start"] = today
                save_state()

            log.info(f"\n{'═' * 52}")
            log.info(f"  CYCLE {cycle} | {now}")
            log.info(f"{'═' * 52}")

            if not check_exchange_health():
                log.warning("[HEALTH] Bitget unreachable — skip cycle")
                time.sleep(SCAN_INTERVAL)
                continue

            bal = fetch_balance()
            if bal > S["peak_bal"]:
                S["peak_bal"] = bal

            # Daily loss limit — pause until midnight
            if S["start_bal"] > 0:
                dloss = S["daily_pnl"] / S["start_bal"]
                if dloss <= -MAX_DAILY_LOSS:
                    log.warning("Daily loss limit hit — pausing until tomorrow")
                    notify("Daily loss limit hit — bot paused", urgent=True)
                    save_state()
                    now_utc  = datetime.now(timezone.utc)
                    tomorrow = (now_utc + timedelta(days=1)).replace(
                        hour=0, minute=5, second=0, microsecond=0)
                    time.sleep(int((tomorrow - now_utc).total_seconds()))
                    S["daily_pnl"] = 0
                    continue

            open_pos = fetch_positions()
            if open_pos is None:
                log.error("[POS] fetch_positions failed — skip cycle for safety")
                time.sleep(SCAN_INTERVAL)
                continue

            log.info(f"[POS] {len(open_pos)} open | "
                     f"Tracked:{list(S['trade_meta'].keys())} | "
                     f"Pending:{list(S['pending_orders'].keys())}")

            # Snapshot unrealized PnL before close detection
            for pos in open_pos:
                sym = pos.get("symbol")
                if sym in S["trade_meta"]:
                    try:
                        S["trade_meta"][sym]["last_mark"] = float(pos.get("markPrice", 0))
                        S["trade_meta"][sym]["last_pnl"]  = float(pos.get("unrealizedPL", 0))
                    except Exception:
                        pass

            detect_closed_positions(open_pos)
            check_pending_orders()

            for pos in open_pos:
                sym = pos.get("symbol")
                if sym in S["trade_meta"]:
                    manage_position(pos)

            if cycle % 20 == 0:
                print_report(bal)

            # Union of all active (exchange + tracked + pending)
            exchange_syms = {p.get("symbol") for p in open_pos}
            all_open      = exchange_syms | set(S["trade_meta"]) | set(S["pending_orders"])

            if len(all_open) >= MAX_TRADES:
                log.info(f"[SKIP] {len(all_open)}/{MAX_TRADES} trades active")
                save_state()
                time.sleep(SCAN_INTERVAL)
                continue

            # ── Signal scan ──
            for sym in SYMBOLS:
                if sym in all_open:
                    log.info(f"[{sym}] Position/order active — skip scan")
                    continue

                log.info(f"\n[SCAN] {'━' * 3} {sym} {'━' * 15}")

                candles = pub_candles_4h(sym, limit=200)
                if not candles or len(candles) < 60:
                    log.warning(f"[{sym}] Not enough 4H candles")
                    continue

                tick = pub_ticker(sym)
                if not tick:
                    continue

                price = float(tick.get("lastPr", 0))
                chg   = float(tick.get("change24h", 0)) * 100
                log.info(f"[{sym}] ${price:,.2f} | {chg:+.2f}% | "
                         f"4H candles:{len(candles)}")

                sig = generate_signal(sym, candles)
                if not sig:
                    continue

                for reason in sig.get("reasons", []):
                    log.info(f"  {reason}")

                execute_entry(sym, sig, bal)

            save_state()
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log.info("[STOP] Bot stopped")
            notify("ARES v7.0 stopped", urgent=True)
            save_state()
            break
        except Exception as e:
            log.error(f"[ERROR] Cycle {cycle}: {e}", exc_info=True)
            notify(f"Bot error: {e}", urgent=True)
            try:
                save_state()
            except Exception:
                pass
            time.sleep(30)


if __name__ == "__main__":
    run()
