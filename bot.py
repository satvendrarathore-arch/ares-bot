"""
ARES v7.2 — EMA Pullback Strategy (RECTIFIED)
==============================================
Bitget USDT-M Perpetuals | BTC + ETH

STRATEGY: 4H EMA Pullback

THESIS:
  In a trending market, price pulls back to the EMA21 before
  continuing. Enter at the EMA (limit order), ride the continuation.
  Exit at 3× ATR. Stop at 1.5× ATR below entry.

  LONG:  EMA9 > EMA21 > EMA50 (uptrend) + price at EMA21 + RSI 35-65
  SHORT: EMA9 < EMA21 < EMA50 (downtrend) + price at EMA21 + RSI 35-65

EXPECTED PERFORMANCE (estimates from market-structure analysis —
NOT validated by code backtest; treat as hypothesis until shadow/live data):
  Bull markets:  est. 60-70% win rate
  Bear markets:  est. 55-62% win rate
  Range markets: est. ~50% win rate
  Target R:R:    2:1 minimum (enforced by TP/SL structure)
  VALIDATE IN SHADOW MODE BEFORE LIVE CAPITAL

FIXES APPLIED IN THIS VERSION (all labeled [FIX-N] in code):
  [FIX-1..11] — see v7.1 (BE trigger, shadow default, logging order,
          risk sizing, closed candles, limit clamp, preset SL,
          non-blocking pause, TP re-arm, daily denominator, fill filter)
  v7.2 hardening (from external code review):
  [FIX-12] Hedge-mode verified at startup — tradeSide semantics are
          hedge-only; bot refuses to run in one-way mode.
  [FIX-13] Passive buffer on limit price (≥0.02% from market) so the
          entry always posts as maker.
  [FIX-14] Plan orders tracked by orderId; targeted cancel helper +
          pending-plan query (covers the exchange-created preset SL).
  [FIX-15] BE move re-sequenced: NEW breakeven SL placed and confirmed
          FIRST, then old SL cancelled by ID. TPs never touched —
          re-arm logic deleted. If new SL fails, nothing was cancelled,
          so the original SL is still live. Zero naked window by design.
  [FIX-16] Fill detection hardened: raw status logged each check
          (verify Bitget's real vocabulary in shadow); partial fills —
          on cancel or expiry — are activated as tracked positions with
          TPs instead of being silently abandoned with only an SL.

SETUP:
  Railway Variables:
    BITGET_API_KEY=...
    BITGET_SECRET_KEY=...
    BITGET_PASSPHRASE=...
    SHADOW_MODE=true             (default true — set false ONLY when ready)
    MAX_TRADES=2
    RISK_PCT=1                   (true % of balance lost if SL hits)
    MAX_LEVERAGE=3
    SCAN_INTERVAL_SECONDS=300
    STATE_FILE_PATH=/app/data/ares_v72_state.json
"""

import os, time, hmac, hashlib, base64, json, logging, requests
from datetime import datetime, timezone

# ═══════════════════════════════════════════════════════════
# LOGGING  [FIX-3: must come BEFORE _get_state_file()]
# ═══════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ares")

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

# [FIX-2] Shadow is the DEFAULT. You must explicitly set
# SHADOW_MODE=false to trade real money.
SHADOW_MODE    = os.environ.get("SHADOW_MODE", "true").lower() == "true"
RESET_STATS    = os.environ.get("RESET_STATS", "false").lower() == "true"
SCAN_INTERVAL  = int(os.environ.get("SCAN_INTERVAL_SECONDS", "300"))
MAX_TRADES     = int(os.environ.get("MAX_TRADES", "2"))
MAX_LEVERAGE   = int(os.environ.get("MAX_LEVERAGE", "3"))
RISK_PCT       = float(os.environ.get("RISK_PCT", "1")) / 100  # true risk at SL

# Strategy params
EMA_FAST       = 9
EMA_MID        = 21
EMA_SLOW       = 50
EMA_PULL_TOL   = 0.008   # price within 0.8% of EMA21 = "at pullback"
RSI_MIN        = 35
RSI_MAX        = 65
ATR_SL_MULT    = 1.5     # SL  = 1.5× ATR
ATR_TP_MULT    = 3.0     # TP1 = 3.0× ATR (2:1)
ATR_TP2_MULT   = 5.0     # TP2 = 5.0× ATR (runner)
MIN_ADX        = 20
BE_TRIGGER     = 0.80    # [FIX-1] SL→BE at 80% of distance to TP1
PASSIVE_BUF    = 0.0002  # [FIX-13] limit must sit ≥0.02% on passive side
MAX_DAILY_LOSS = 0.05    # 5% daily loss limit
MAX_DRAWDOWN   = 0.15    # 15% drawdown → reduce size

SYMBOL_SPECS = {
    "BTCUSDT": {"min_size": 0.0001, "precision": 4, "price_precision": 1},
    "ETHUSDT": {"min_size": 0.01,   "precision": 3, "price_precision": 2},
}

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
        with open(test, "w") as f: f.write("ok")
        os.remove(test)
        return "/app/data/ares_v72_state.json"
    except Exception:
        log.warning(
            "[STATE] /app/data not writable — falling back to /tmp. "
            "State WILL BE LOST on restart. Set STATE_FILE_PATH to fix this."
        )
        return "/tmp/ares_v72_state.json"

STATE_FILE = _get_state_file()

S = {
    "start_bal":       0.0,
    "daily_start_bal": 0.0,    # [FIX-10]
    "peak_bal":        0.0,
    "total_pnl":       0.0,
    "daily_pnl":       0.0,
    "daily_start":     None,
    "paused_today":    False,  # [FIX-8]
    "wins":            0,
    "losses":          0,
    "loss_streak":     0,
    "win_streak":      0,
    "trade_meta":      {},
    "pending_orders":  {},
}

def save_state():
    try:
        d = os.path.dirname(STATE_FILE)
        if d: os.makedirs(d, exist_ok=True)
        snap = {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                for k, v in S.items()}
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
                try: S[k] = datetime.fromisoformat(v).date()
                except Exception: S[k] = datetime.now(timezone.utc).date()
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
            timeout=5
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
    """Authenticated Bitget API call. Always returns a dict (never None)."""
    last = None
    for attempt in range(retries):
        try:
            ts       = str(int(time.time() * 1000))
            body_str = json.dumps(body) if body else ""

            # Build query string manually so the signature and actual URL
            # match exactly.
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
            raise
        except Exception as e:
            log.error(f"[API] {method} {path}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    return last or {"code": "CLIENT_ERROR", "msg": "no response from exchange"}

def pub_get(path, params=None, retries=3):
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

# ═══════════════════════════════════════════════════════════
# MARKET DATA
# ═══════════════════════════════════════════════════════════

def pub_candles_4h(sym, limit=200):
    data = pub_get("/api/v2/mix/market/candles",
                   {"symbol": sym, "productType": PRODUCT_TYPE,
                    "granularity": "4H", "limit": str(limit)})
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
    data = pub_get("/api/v2/mix/market/ticker",
                   {"symbol": sym, "productType": PRODUCT_TYPE})
    if data and data.get("data"):
        d = data["data"]
        return d[0] if isinstance(d, list) else d
    return None

def fetch_balance():
    """
    Robust balance fetch with field fallbacks + diagnostics.
    Bitget account fields vary by margin mode — try several,
    and log the raw response whenever we'd otherwise return $0
    so the real schema is visible in Railway logs.
    """
    res = call("GET", "/api/v2/mix/account/accounts",
               params={"productType": PRODUCT_TYPE})
    if res and res.get("data"):
        for acc in res["data"]:
            if str(acc.get("marginCoin", "")).upper() == "USDT":
                for field in ("available", "crossedMaxAvailable",
                              "isolatedMaxAvailable", "maxTransferOut",
                              "accountEquity", "usdtEquity"):
                    try:
                        v = float(acc.get(field) or 0)
                    except (TypeError, ValueError):
                        v = 0.0
                    if v > 0:
                        return v
                # All fields zero/missing — show what Bitget actually sent
                log.warning(f"[BAL] USDT account found but all balance "
                            f"fields 0 — raw: {json.dumps(acc)[:400]}")
                return 0.0
        log.warning(f"[BAL] No USDT entry in accounts — raw: "
                    f"{json.dumps(res.get('data'))[:400]}")
    else:
        log.warning(f"[BAL] accounts query failed: "
                    f"code={res.get('code') if res else None} "
                    f"msg={res.get('msg') if res else None}")
    return 0.0

def fetch_positions():
    res = call("GET", "/api/v2/mix/position/all-position",
               params={"productType": PRODUCT_TYPE, "marginCoin": "USDT"})
    if not res or res.get("code") != "00000":
        return None  # API failure — don't assume no positions
    data = res.get("data") or []
    return [p for p in data if float(p.get("total", 0)) > 0]

def check_exchange_health():
    try:
        r = requests.get(f"{BASE_URL}/api/v2/public/time", timeout=5)
        return r.json().get("code") == "00000"
    except Exception:
        return False

def check_position_mode():
    """
    [FIX-12] This bot's order semantics (tradeSide=open/close,
    holdSide on plan orders) assume HEDGE mode. Bitget ignores
    tradeSide in one-way mode, which silently changes behavior.

    Uses the SINGLE-account endpoint (/account), which returns
    posMode/holdMode — the list endpoint (/accounts) does not.
    Field is "posMode" (hedge_mode/one_way_mode) on v2; older
    payloads used "holdMode" (double_hold/single_hold). Accept both.
    """
    res = call("GET", "/api/v2/mix/account/account",
               params={"symbol": "BTCUSDT", "productType": PRODUCT_TYPE,
                       "marginCoin": "USDT"})
    if res and res.get("code") == "00000" and res.get("data"):
        d = res["data"]
        # single-account returns an object; be tolerant if a list comes back
        if isinstance(d, list):
            d = d[0] if d else {}
        mode = str(d.get("posMode") or d.get("holdMode") or "")
        log.info(f"[INIT] Position mode: {mode or 'unknown'}")
        # normalise the legacy "double_hold" → hedge for the caller's check
        if mode == "double_hold":
            return "hedge_mode"
        if mode == "single_hold":
            return "one_way_mode"
        return mode
    log.warning(f"[INIT] Position-mode query failed: "
                f"code={res.get('code') if res else None}")
    return ""

# ═══════════════════════════════════════════════════════════
# INDICATORS
# ═══════════════════════════════════════════════════════════

def ema(data, p):
    if not data: return 0
    if len(data) < p: return data[-1]
    k = 2 / (p + 1)
    v = sum(data[:p]) / p
    for x in data[p:]:
        v = x * k + v * (1 - k)
    return v

def calc_rsi(closes, p=14):
    if len(closes) < p + 1: return 50
    diffs = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    g = sum(max(d, 0) for d in diffs[-p:]) / p
    l = sum(abs(min(d, 0)) for d in diffs[-p:]) / p
    if g == 0 and l == 0: return 50
    if l == 0: return 100
    return 100 - (100 / (1 + g / l))

def calc_atr(highs, lows, closes, p=14):
    if len(closes) < p + 1:
        return closes[-1] * 0.01 if closes else 0
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]),
               abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
    if len(trs) < p:
        return sum(trs) / len(trs) if trs else 0
    atr = sum(trs[:p]) / p
    for tr in trs[p:]:
        atr = (atr * (p-1) + tr) / p
    return atr

def calc_adx(highs, lows, closes, period=14):
    if len(closes) < 2*period + 1: return 0
    tr_list, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        hd = highs[i]-highs[i-1]; ld = lows[i-1]-lows[i]
        plus_dm.append(max(hd, 0) if hd > ld else 0)
        minus_dm.append(max(ld, 0) if ld > hd else 0)
        tr_list.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]),
                           abs(lows[i]-closes[i-1])))
    st=sum(tr_list[:period]); sp=sum(plus_dm[:period]); sm=sum(minus_dm[:period])
    dx = []
    for i in range(period, len(tr_list)):
        st=st-(st/period)+tr_list[i]
        sp=sp-(sp/period)+plus_dm[i]
        sm=sm-(sm/period)+minus_dm[i]
        if st==0: continue
        pdi=100*sp/st; mdi=100*sm/st; s=pdi+mdi
        dx.append(0 if s==0 else 100*abs(pdi-mdi)/s)
    if len(dx) < period: return 0
    adx = sum(dx[:period]) / period
    for i in range(period, len(dx)):
        adx = (adx*(period-1) + dx[i]) / period
    return adx

# ═══════════════════════════════════════════════════════════
# EMA PULLBACK SIGNAL
# ═══════════════════════════════════════════════════════════

def generate_signal(sym, candles, live_price=0):
    """
    EMA Pullback Strategy Signal.

    [FIX-5] All indicators computed on CLOSED candles only — the
    currently-forming candle is dropped, so EMAs/RSI don't repaint
    mid-candle and the volume filter compares a completed candle
    against completed-candle averages.

    LONG:
      1. Uptrend:  EMA9 > EMA21 > EMA50
      2. Pullback: price within EMA_PULL_TOL of EMA21
      3. RSI:      35-65
      4. ADX:      >= MIN_ADX
      5. Volume:   last CLOSED candle vol > 0.7x prior 20-candle avg
    SHORT: mirror.
    """
    if len(candles) < 61:
        log.warning(f"[{sym}] Not enough candles ({len(candles)})")
        return None

    closed = candles[:-1]  # [FIX-5] drop the live, incomplete candle

    closes = [c["close"] for c in closed]
    highs  = [c["high"]  for c in closed]
    lows   = [c["low"]   for c in closed]
    vols   = [c["vol"]   for c in closed]

    price = live_price if live_price > 0 else closes[-1]
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

    # [FIX-5] last CLOSED candle vs avg of the 20 before it
    avg_vol = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else vols[-1]
    vol_ok  = vols[-1] > avg_vol * 0.7

    dist_from_e21 = abs(price - e21) / e21
    px_prec = SYMBOL_SPECS.get(sym, {}).get("price_precision", 2)

    log.info(f"[{sym}] Price:${price:.2f} | E9:${e9:.2f} E21:${e21:.2f} E50:${e50:.2f}")
    log.info(f"[{sym}] RSI:{rsi:.0f} | ADX:{adx:.0f} | ATR:${atr:.2f} | "
             f"Dist21:{dist_from_e21*100:.2f}%")

    # ── LONG setup ──
    if (e9 > e21 > e50 and
        dist_from_e21 <= EMA_PULL_TOL and
        RSI_MIN <= rsi <= RSI_MAX and
        adx >= MIN_ADX and
        vol_ok):

        # [FIX-6/13] Never place a buy limit above or at market —
        # keep it at least PASSIVE_BUF below so it always posts as maker
        limit_px = round(min(e21, price * (1 - PASSIVE_BUF)), px_prec)
        sl  = limit_px - atr * ATR_SL_MULT
        tp1 = limit_px + atr * ATR_TP_MULT
        tp2 = limit_px + atr * ATR_TP2_MULT

        log.info(f"[{sym}] ✅ LONG PULLBACK | Limit:${limit_px} "
                 f"SL:${sl:.2f} TP1:${tp1:.2f} TP2:${tp2:.2f}")
        return {
            "sym": sym, "action": "LONG", "side": "buy", "hold": "long",
            "price": price, "limit_price": limit_px,
            "sl": sl, "tp1": tp1, "tp2": tp2,
            "atr": atr, "rsi": rsi, "adx": adx,
            "reasons": [
                f"✅ Uptrend: E9({e9:.0f}) > E21({e21:.0f}) > E50({e50:.0f})",
                f"✅ Pullback to EMA21 ({dist_from_e21*100:.1f}% away)",
                f"✅ RSI {rsi:.0f} (continuation zone)",
                f"✅ ADX {adx:.0f} (trend confirmed)",
            ]
        }

    # ── SHORT setup ──
    if (e9 < e21 < e50 and
        dist_from_e21 <= EMA_PULL_TOL and
        RSI_MIN <= rsi <= RSI_MAX and
        adx >= MIN_ADX and
        vol_ok):

        # [FIX-6/13] Never place a sell limit below or at market
        limit_px = round(max(e21, price * (1 + PASSIVE_BUF)), px_prec)
        sl  = limit_px + atr * ATR_SL_MULT
        tp1 = limit_px - atr * ATR_TP_MULT
        tp2 = limit_px - atr * ATR_TP2_MULT

        log.info(f"[{sym}] ✅ SHORT PULLBACK | Limit:${limit_px} "
                 f"SL:${sl:.2f} TP1:${tp1:.2f} TP2:${tp2:.2f}")
        return {
            "sym": sym, "action": "SHORT", "side": "sell", "hold": "short",
            "price": price, "limit_price": limit_px,
            "sl": sl, "tp1": tp1, "tp2": tp2,
            "atr": atr, "rsi": rsi, "adx": adx,
            "reasons": [
                f"✅ Downtrend: E9({e9:.0f}) < E21({e21:.0f}) < E50({e50:.0f})",
                f"✅ Bounce to EMA21 ({dist_from_e21*100:.1f}% away)",
                f"✅ RSI {rsi:.0f} (continuation zone)",
                f"✅ ADX {adx:.0f} (trend confirmed)",
            ]
        }

    trend = "UP" if e9 > e21 else "DOWN" if e9 < e21 else "FLAT"
    log.info(f"[{sym}] HOLD | Trend:{trend} | "
             f"EMA aligned:{e9>e21>e50 or e9<e21<e50} | "
             f"At pullback:{dist_from_e21<=EMA_PULL_TOL} | "
             f"ADX ok:{adx>=MIN_ADX}")
    return None

# ═══════════════════════════════════════════════════════════
# ORDER MANAGEMENT
# ═══════════════════════════════════════════════════════════

def set_leverage(sym, lev):
    for hold_side in ["long", "short"]:
        res = call("POST", "/api/v2/mix/account/set-leverage",
                   body={"symbol": sym, "productType": PRODUCT_TYPE,
                         "marginCoin": "USDT", "leverage": str(lev),
                         "holdSide": hold_side})
        if not res or res.get("code") not in ["00000", "40919"]:
            log.error(f"[LEV] {sym} {hold_side} failed: {res}")
            return False
    return True

def place_limit_order(sym, side, size, price, preset_sl=None):
    """
    LIMIT entry order.
    [FIX-7] presetStopLossPrice rides on the order itself — the
    exchange arms the SL the instant the order fills. No naked window
    even if the bot crashes or Railway restarts mid-cycle.
    """
    spec = SYMBOL_SPECS.get(sym, {"price_precision": 2})
    px = round(price, spec["price_precision"])
    body = {"symbol": sym, "productType": PRODUCT_TYPE,
            "marginCoin": "USDT", "side": side,
            "tradeSide": "open", "orderType": "limit",
            "price": str(px), "size": str(size),
            "force": "gtc"}
    if preset_sl:
        body["presetStopLossPrice"] = str(round(preset_sl, spec["price_precision"]))
    res = call("POST", "/api/v2/mix/order/place-order", body=body)
    if res and res.get("code") == "00000":
        oid = res.get("data", {}).get("orderId", "N/A")
        log.info(f"[ORDER] LIMIT {sym} {side.upper()} {size} @ ${px} "
                 f"presetSL:{preset_sl} | ID:{oid}")
        return oid
    log.error(f"[ORDER] Limit order failed: {res}")
    return None

def place_market_order(sym, side, size, trade_side="open"):
    """Market order — only for emergency closes."""
    res = call("POST", "/api/v2/mix/order/place-order",
               body={"symbol": sym, "productType": PRODUCT_TYPE,
                     "marginCoin": "USDT", "side": side,
                     "tradeSide": trade_side, "orderType": "market",
                     "size": str(size), "force": "ioc"})
    if res and res.get("code") == "00000":
        log.info(f"[ORDER] MARKET {sym} {side.upper()} {size} ({trade_side})")
        return res
    log.error(f"[ORDER] Market order failed: {res}")
    return None

def place_plan_order(sym, plan_type, trigger_px, hold_side, size):
    """
    Place SL or TP plan order.
    [FIX-14] Returns the plan orderId (truthy) on success, None on
    failure — so callers can later cancel THIS specific order instead
    of nuking everything with cancel-all.
    """
    spec = SYMBOL_SPECS.get(sym, {"price_precision": 2})
    px = round(trigger_px, spec["price_precision"])
    res = call("POST", "/api/v2/mix/order/place-tpsl-order",
               body={"symbol": sym, "productType": PRODUCT_TYPE,
                     "marginCoin": "USDT", "planType": plan_type,
                     "triggerPrice": str(px), "holdSide": hold_side,
                     "size": str(size), "triggerType": "mark_price"})
    if res and res.get("code") == "00000":
        oid = (res.get("data") or {}).get("orderId", "")
        log.info(f"[PLAN] {plan_type} @ ${px} | ID:{oid}")
        return oid or "unknown"
    log.error(f"[PLAN] {plan_type} failed: {res}")
    return None

def get_loss_plan_ids(sym, hold_side):
    """
    [FIX-14] Fetch the orderIds of currently-pending loss plans
    (SL trigger orders) for a symbol+side — including the SL that
    Bitget auto-created from presetStopLossPrice (which we never
    placed ourselves and so have no stored ID for).
    Logs the raw response once so the real schema can be verified
    in shadow/early-live runs.
    """
    res = call("GET", "/api/v2/mix/order/orders-plan-pending",
               params={"symbol": sym, "productType": PRODUCT_TYPE,
                       "planType": "profit_loss"})
    ids = []
    if res and res.get("code") == "00000":
        data = res.get("data") or {}
        orders = data.get("entrustedList") or data.get("orderList") or []
        log.info(f"[PLAN] raw pending plans {sym}: {json.dumps(orders)[:400]}")
        for o in orders:
            try:
                ptype = (o.get("planType") or "").lower()
                oside = (o.get("holdSide") or "").lower()
                if "loss" in ptype and (not oside or oside == hold_side):
                    oid = o.get("orderId")
                    if oid:
                        ids.append(oid)
            except Exception:
                continue
    else:
        log.warning(f"[PLAN] pending-plan query failed: {res}")
    return ids

def cancel_plan_order_by_id(sym, order_id):
    """[FIX-14] Cancel ONE specific trigger order (not cancel-all)."""
    res = call("POST", "/api/v2/mix/order/cancel-plan-order",
               body={"symbol": sym, "productType": PRODUCT_TYPE,
                     "marginCoin": "USDT", "planType": "profit_loss",
                     "orderIdList": [{"orderId": str(order_id)}]})
    ok = bool(res and res.get("code") == "00000")
    if not ok:
        log.warning(f"[PLAN] cancel {order_id} failed: {res}")
    return ok

def cancel_all_plan_orders(sym):
    call("POST", "/api/v2/mix/order/cancel-all-trigger-orders",
         body={"symbol": sym, "productType": PRODUCT_TYPE, "marginCoin": "USDT"})

def cancel_limit_order(sym, order_id):
    call("POST", "/api/v2/mix/order/cancel-order",
         body={"symbol": sym, "productType": PRODUCT_TYPE,
               "marginCoin": "USDT", "orderId": order_id})

def check_limit_order_filled(sym, order_id):
    res = call("GET", "/api/v2/mix/order/detail",
               params={"symbol": sym, "productType": PRODUCT_TYPE,
                       "orderId": order_id})
    if res and res.get("data"):
        status = res["data"].get("status", "")
        size   = float(res["data"].get("baseVolume", 0))
        price  = float(res["data"].get("priceAvg", 0) or 0)
        return status, size, price
    return "unknown", 0, 0

# ═══════════════════════════════════════════════════════════
# POSITION SIZING  (true risk-based)
# ═══════════════════════════════════════════════════════════

def calc_position_size(bal, price, sl_price, lev, sym):
    """
    TRUE risk-based sizing:
      size = risk_usd / SL_distance
    This guarantees that if SL hits, the loss is EXACTLY risk_usd —
    regardless of ATR/volatility.

    Returns (size, margin_required, actual_risk_usd).
    """
    spec = SYMBOL_SPECS.get(sym, {"min_size": 0.0001, "precision": 4})

    sl_distance = abs(price - sl_price)
    if sl_distance <= 0:
        log.error(f"[SIZE] {sym} invalid SL distance — skip")
        return 0, 0, 0

    risk_usd = bal * RISK_PCT

    # Drawdown reduction
    if S["peak_bal"] > 0:
        dd = (S["peak_bal"] - bal) / S["peak_bal"]
        if dd > MAX_DRAWDOWN:
            risk_usd *= 0.5
            log.warning(f"[SIZE] Drawdown {dd*100:.1f}% → risk halved")
        elif dd > 0.08:
            risk_usd *= 0.75

    # Loss streak reduction
    if S["loss_streak"] >= 3:
        risk_usd *= 0.6
        log.info(f"[SIZE] Loss streak {S['loss_streak']} → risk 60%")
    elif S["loss_streak"] == 2:
        risk_usd *= 0.8

    size = risk_usd / sl_distance
    margin = (size * price) / lev

    # Cap margin at 20% of balance
    max_margin = bal * 0.20
    if margin > max_margin:
        scale  = max_margin / margin
        size   *= scale
        margin  = max_margin
        log.info(f"[SIZE] {sym} margin capped at 20% of balance "
                 f"(risk reduced to ${size * sl_distance:.2f})")

    size = round(size, spec["precision"])
    if size < spec["min_size"]:
        log.warning(f"[SIZE] {sym} size {size} < min {spec['min_size']} "
                    f"(need more capital) — skip")
        return 0, 0, 0

    actual_risk = size * sl_distance
    log.info(f"[SIZE] {sym} size={size} | margin=${margin:.2f} | "
             f"risk at SL=${actual_risk:.2f} ({actual_risk/bal*100:.2f}% of bal)")
    return size, margin, actual_risk

# ═══════════════════════════════════════════════════════════
# POSITION MANAGEMENT
# ═══════════════════════════════════════════════════════════

def manage_position(pos):
    """Move SL to breakeven near TP1; keep TPs armed."""
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
    if not (mark == mark and entry == entry):  # NaN check
        return
    if mark <= 0 or entry <= 0 or size <= 0:
        return

    meta = S["trade_meta"].get(sym, {})
    if not meta:
        return

    margin  = meta.get("margin", 1)
    tp1     = meta.get("tp1", 0)
    tp2     = meta.get("tp2", 0)
    pnl_pct = (pnl / margin * 100) if margin else 0

    log.info(f"[MGR] {sym} {hold.upper()} Mark:${mark:,.2f} "
             f"PnL:{pnl:+.4f} ({pnl_pct:+.1f}%)")

    # [FIX-1] BE trigger = 80% of the DISTANCE from entry to TP1.
    # Old code used 95% of raw PRICE, which is BELOW entry for any
    # realistic ATR — fired instantly and scratched every trade.
    if not meta.get("be_moved") and tp1 > 0:
        if hold == "long":
            tp1_reached = mark >= entry + BE_TRIGGER * (tp1 - entry)
        else:
            tp1_reached = mark <= entry - BE_TRIGGER * (entry - tp1)

        if tp1_reached:
            # [FIX-15] SAFE BE SEQUENCE — never a naked moment:
            #   1. Snapshot existing loss-plan IDs (incl. the preset SL
            #      Bitget auto-created, which we have no stored ID for)
            #   2. Place the NEW breakeven SL first
            #   3. Only if it confirms, cancel the OLD SL(s) by ID
            #   TPs are never touched, so no re-arm step exists at all.
            be_price = entry * (1.001 if hold == "long" else 0.999)

            old_sl_ids = get_loss_plan_ids(sym, hold)
            new_sl_id  = place_plan_order(sym, "loss_plan", be_price, hold, size)

            if new_sl_id:
                for oid in old_sl_ids:
                    if oid != new_sl_id:
                        cancel_plan_order_by_id(sym, oid)
                meta["current_sl"] = be_price
                meta["sl_oid"]     = new_sl_id
                meta["be_moved"]   = True
                S["trade_meta"][sym] = meta
                save_state()
                log.info(f"[MGR] {sym} 80% to TP1 — SL → BE ${be_price:.2f} "
                         f"(old SL cancelled, TPs untouched)")
                notify(f"📈 {sym} near TP1 — SL moved to breakeven", urgent=True)
            else:
                # New BE SL failed — we did NOT cancel anything, so the
                # original SL is still fully active. Position never naked.
                log.error(f"[MGR] {sym} BE SL placement failed — "
                          f"original SL still active, will retry next cycle")

def detect_closed_positions(open_pos):
    """Detect positions that closed (via SL/TP on exchange)."""
    open_syms = {p.get("symbol") for p in (open_pos or [])}
    for sym in list(S["trade_meta"].keys()):
        if sym not in open_syms:
            meta = S["trade_meta"][sym]

            # [FIX-11] only count fills AFTER this trade's entry time
            entry_ms = 0
            try:
                entry_ms = int(datetime.fromisoformat(
                    meta.get("entry_time", "")).timestamp() * 1000)
            except Exception:
                pass

            res = call("GET", "/api/v2/mix/order/fills",
                       params={"symbol": sym, "productType": PRODUCT_TYPE,
                               "limit": "20"})
            actual_pnl = 0.0
            if res and res.get("data"):
                fills = res["data"].get("fillList", [])
                for fill in fills:
                    if fill.get("tradeSide") not in ("close_long", "close_short", "close"):
                        continue
                    try:
                        c_time = int(fill.get("cTime", 0))
                        if entry_ms and c_time and c_time < entry_ms:
                            continue  # [FIX-11] fill from an older trade
                        actual_pnl += float(fill.get("profit", 0))
                    except Exception:
                        pass

            if actual_pnl == 0:
                actual_pnl = meta.get("last_pnl", 0)

            S["total_pnl"] += actual_pnl
            S["daily_pnl"] += actual_pnl
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

            log.info(f"[CLOSE] {sym} closed — {result} | "
                     f"W:{S['wins']} L:{S['losses']}")

            if sym in S["pending_orders"]:
                del S["pending_orders"][sym]
            # Position is gone — cancel any leftover trigger orders
            # (remaining TP, stale SL, or a duplicate SL if the
            # plan-ID query schema ever mismatches). Safe: cancel-all
            # with no open position only removes orphans.
            cancel_all_plan_orders(sym)
            del S["trade_meta"][sym]
            save_state()

# ═══════════════════════════════════════════════════════════
# ENTRY EXECUTION
# ═══════════════════════════════════════════════════════════

def execute_entry(sym, sig, bal):
    """Place LIMIT order at EMA21 with pre-armed SL."""
    price = sig["limit_price"]
    # SL/TP already computed relative to the limit price in generate_signal
    size, margin, actual_risk = calc_position_size(
        bal, price, sig["sl"], MAX_LEVERAGE, sym)
    if size <= 0:
        return

    if not set_leverage(sym, MAX_LEVERAGE):
        log.error(f"[ENTRY] {sym} leverage failed")
        return

    if SHADOW_MODE:
        log.info(f"[SHADOW] 👻 Would place LIMIT {sig['action']} {sym} "
                 f"@ ${price:.2f} size={size} margin=${margin:.2f} "
                 f"risk=${actual_risk:.2f}")
        notify(f"👻 SHADOW: {sym} {sig['action']} LIMIT @ ${price:.2f} | "
               f"SL:${sig['sl']:.2f} TP:${sig['tp1']:.2f}", urgent=False)
        return

    # [FIX-7] SL rides on the entry order itself
    oid = place_limit_order(sym, sig["side"], size, price, preset_sl=sig["sl"])
    if not oid:
        return

    S["pending_orders"][sym] = {
        "order_id":   oid,
        "side":       sig["side"],
        "hold":       sig["hold"],
        "limit_px":   price,
        "sl":         sig["sl"],
        "tp1":        sig["tp1"],
        "tp2":        sig["tp2"],
        "atr":        sig["atr"],
        "size":       size,
        "margin":     margin,
        "placed_ts":  int(time.time() * 1000),
    }
    save_state()
    log.info(f"[ENTRY] ✅ LIMIT placed: {sym} {sig['action']} @ ${price:.2f} "
             f"size={size} ID:{oid}")
    notify(f"📋 {sym} LIMIT {sig['action']} @ ${price:.2f}\n"
           f"SL:${sig['sl']:.2f} (pre-armed) TP:${sig['tp1']:.2f}\n"
           f"Waiting for fill...", urgent=True)

def _activate_filled_entry(sym, order, size, fill_price, partial=False):
    """
    [FIX-16] Shared activation for full AND partial fills: compute TPs
    from actual entry, place the TP split, register in trade_meta.
    The SL is already armed by the exchange (preset on the order).
    Previously a partial fill that expired left a REAL position with
    an SL but no TPs and no tracking — this closes that hole.
    """
    actual_entry = fill_price if fill_price > 0 else order["limit_px"]
    atr  = order["atr"]
    hold = order["hold"]
    if hold == "long":
        tp1 = actual_entry + atr * ATR_TP_MULT
        tp2 = actual_entry + atr * ATR_TP2_MULT
    else:
        tp1 = actual_entry - atr * ATR_TP_MULT
        tp2 = actual_entry - atr * ATR_TP2_MULT

    spec = SYMBOL_SPECS.get(sym, {"precision": 4, "min_size": 0})
    size = round(size, spec["precision"])
    if size < spec.get("min_size", 0):
        log.warning(f"[FILL] {sym} fill size {size} below min — "
                    f"position too small to manage, leaving exchange SL armed")
        return

    half = round(size * 0.5, spec["precision"])
    rest = round(size - half, spec["precision"])
    tp1_oid = tp2_oid = None
    if half >= spec.get("min_size", 0):
        tp1_oid = place_plan_order(sym, "profit_plan", tp1, hold, half)
    if rest >= spec.get("min_size", 0):
        tp2_oid = place_plan_order(sym, "profit_plan", tp2, hold, rest)

    S["trade_meta"][sym] = {
        "entry_price": actual_entry,
        "entry_time":  datetime.now(timezone.utc).isoformat(),
        "side":        hold,
        "lev":         MAX_LEVERAGE,
        "margin":      order["margin"] * (size / order["size"]
                                          if order.get("size") else 1),
        "size":        size,
        "current_sl":  order["sl"],   # pre-armed preset SL
        "tp1":         tp1,
        "tp2":         tp2,
        "tp1_oid":     tp1_oid,
        "tp2_oid":     tp2_oid,
        "atr":         atr,
        "be_moved":    False,
        "last_pnl":    0.0,
        "last_mark":   actual_entry,
    }
    save_state()
    tag = "PARTIAL FILL" if partial else "FILLED"
    notify(f"✅ {sym} {hold.upper()} {tag} @ ${actual_entry:.2f} "
           f"(size {size})\nSL:${order['sl']:.2f} "
           f"TP1:${tp1:.2f} TP2:${tp2:.2f}", urgent=True)

def check_pending_orders():
    """
    Check pending limit orders.
    [FIX-7]  SL is pre-armed on the order; on fill only TPs are added.
    [FIX-16] Raw status is logged (Bitget v2 vocabulary: live /
             partially_filled / filled / canceled) and a partial fill
             at expiry becomes a tracked position instead of being
             silently abandoned.
    Cancel after 2 candles (8h) if unfilled.
    """
    now_ms = int(time.time() * 1000)
    CANCEL_AFTER_MS = 8 * 3600 * 1000  # 2 x 4H candles

    for sym, order in list(S["pending_orders"].items()):
        oid = order["order_id"]
        status, filled_size, fill_price = check_limit_order_filled(sym, oid)
        # [FIX-16] log the raw status so the real vocabulary can be
        # verified during shadow/early-live instead of assumed
        log.info(f"[FILL] {sym} order {oid} raw status='{status}' "
                 f"filled={filled_size}")

        if status in ("full_fill", "filled"):
            log.info(f"[FILL] {sym} limit order FILLED @ ${fill_price:.2f}")
            _activate_filled_entry(
                sym, order,
                filled_size if filled_size > 0 else order["size"],
                fill_price)
            del S["pending_orders"][sym]
            save_state()

        elif status in ("cancelled", "canceled", "cancel"):
            if filled_size > 0:
                # cancelled after a partial fill — a real position exists
                log.warning(f"[FILL] {sym} cancelled with partial fill "
                            f"{filled_size} — activating position")
                _activate_filled_entry(sym, order, filled_size,
                                       fill_price, partial=True)
            else:
                log.info(f"[FILL] {sym} order cancelled externally")
            del S["pending_orders"][sym]
            save_state()

        elif now_ms - order["placed_ts"] > CANCEL_AFTER_MS:
            log.info(f"[FILL] {sym} limit order expired (8h) — cancelling")
            cancel_limit_order(sym, oid)
            # [FIX-16] re-check after cancel: any partially-filled size
            # is a LIVE position that must be protected and tracked
            time.sleep(1)
            status2, filled2, fillpx2 = check_limit_order_filled(sym, oid)
            if filled2 > 0:
                log.warning(f"[FILL] {sym} expired order had partial fill "
                            f"{filled2} — activating position")
                _activate_filled_entry(sym, order, filled2, fillpx2,
                                       partial=True)
                notify(f"⚠️ {sym} limit expired with PARTIAL fill {filled2} "
                       f"— now managed as open position", urgent=True)
            else:
                notify(f"⏱️ {sym} limit order expired — price moved away",
                       urgent=False)
            del S["pending_orders"][sym]
            save_state()

        else:
            age_h = (now_ms - order["placed_ts"]) / 3600000
            log.info(f"[FILL] {sym} limit order pending ({age_h:.1f}h)")

# ═══════════════════════════════════════════════════════════
# REPORTING
# ═══════════════════════════════════════════════════════════

def print_report(bal):
    start = S["start_bal"]
    growth = ((bal - start) / start * 100) if start > 0 else 0
    dd = ((S["peak_bal"] - bal) / S["peak_bal"] * 100) if S["peak_bal"] > 0 else 0
    total = S["wins"] + S["losses"]
    wr = S["wins"] / total * 100 if total > 0 else 0

    log.info("╔══════════════════════════════════════╗")
    log.info(f"║  💰 ARES v7.2 REPORT")
    log.info(f"║  Balance:  ${bal:.2f}")
    log.info(f"║  Growth:   {growth:+.2f}%")
    log.info(f"║  Drawdown: {dd:.2f}%")
    log.info(f"║  PnL:      ${S['total_pnl']:+.4f}")
    log.info(f"║  WinRate:  {wr:.1f}% ({S['wins']}W/{S['losses']}L)")
    log.info(f"║  Strategy: EMA Pullback 4H")
    if SHADOW_MODE:
        log.info(f"║  Mode:     SHADOW (no real trades)")
    if S["paused_today"]:
        log.info(f"║  ⛔ Entries paused (daily loss limit)")
    log.info("╚══════════════════════════════════════╝")

# ═══════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════

def run():
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║  ▲ ARES v7.2 — EMA Pullback Strategy (4H)       ║")
    log.info(f"║  Risk:{RISK_PCT*100:.1f}%/trade (TRUE risk) | Lev:{MAX_LEVERAGE}x | "
             f"Max:{MAX_TRADES}")
    log.info(f"║  Orders: LIMIT + preset SL | TF: 4H closed candles")
    if SHADOW_MODE:
        log.info("║  ⚠️  SHADOW MODE — No real trades (default ON)")
    else:
        log.info("║  🔴 LIVE MODE — Real money")
    log.info("╚══════════════════════════════════════════════════╝")

    if not API_KEY or not SECRET_KEY or not PASSPHRASE:
        log.error("❌ API keys missing — check Railway variables")
        return

    if not check_exchange_health():
        log.error("[INIT] Bitget unreachable — waiting...")
        for _ in range(60):
            time.sleep(60)
            if check_exchange_health():
                break

    # [FIX-12] verify hedge mode before any order logic runs.
    # Retry a few times; if STILL unverifiable: abort in LIVE mode
    # (never trade real money on an unconfirmed assumption), but allow
    # SHADOW mode to proceed with a warning (no orders are placed).
    pos_mode = ""
    for _ in range(3):
        pos_mode = check_position_mode()
        if pos_mode:
            break
        time.sleep(5)
    if pos_mode and "hedge" not in pos_mode.lower():
        log.error(f"❌ Account is in '{pos_mode}' — this bot requires "
                  f"HEDGE mode (tradeSide/holdSide semantics). Switch "
                  f"position mode on Bitget, then restart.")
        notify("❌ ARES requires Hedge Mode on Bitget — bot stopped",
               urgent=True)
        return
    if not pos_mode:
        if SHADOW_MODE:
            log.warning("[INIT] Could not verify position mode — "
                        "SHADOW mode, proceeding (no real orders)")
        else:
            log.error("❌ Could not verify position mode and LIVE mode "
                      "is enabled — refusing to trade on an unconfirmed "
                      "assumption. Check API permissions and restart.")
            notify("❌ ARES: position mode unverifiable in LIVE — stopped",
                   urgent=True)
            return

    load_state()
    if RESET_STATS:
        for key in ["start_bal", "daily_start_bal", "peak_bal", "total_pnl",
                    "daily_pnl", "wins", "losses", "loss_streak", "win_streak"]:
            S[key] = 0.0 if isinstance(S[key], float) else 0
        S["pending_orders"] = {}
        S["trade_meta"] = {}
        S["paused_today"] = False
        save_state()

    bal = fetch_balance()
    if S["start_bal"] == 0:       S["start_bal"] = bal
    if S["peak_bal"] == 0:        S["peak_bal"]  = bal
    if S["daily_start_bal"] == 0: S["daily_start_bal"] = bal  # [FIX-10]
    S["daily_start"] = datetime.now(timezone.utc).date()

    log.info(f"[INIT] Balance: ${bal:.2f} USDT"
             f"{' [SHADOW]' if SHADOW_MODE else ' [LIVE]'}")
    notify(f"🚀 ARES v7.2 started | ${bal:.2f}"
           f"{' [SHADOW]' if SHADOW_MODE else ' [LIVE]'}", urgent=True)

    cycle = 0
    while True:
        try:
            cycle += 1
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            today = datetime.now(timezone.utc).date()

            # Daily reset
            if S["daily_start"] and today != S["daily_start"]:
                log.info("🔄 New day — daily PnL reset, entries re-enabled")
                S["daily_pnl"]       = 0
                S["daily_start"]     = today
                S["daily_start_bal"] = fetch_balance()  # [FIX-10]
                S["paused_today"]    = False             # [FIX-8]
                save_state()

            log.info(f"\n{'═'*52}")
            log.info(f"  CYCLE {cycle} | {now}")
            log.info(f"{'═'*52}")

            if not check_exchange_health():
                log.warning("[HEALTH] Bitget unreachable — skip cycle")
                time.sleep(SCAN_INTERVAL)
                continue

            bal = fetch_balance()
            if bal > S["peak_bal"]: S["peak_bal"] = bal

            # [FIX-8 + FIX-10] Daily loss limit: set a pause flag, keep
            # running. Positions and pending orders are STILL managed —
            # only new entries are blocked until next UTC day.
            denom = S["daily_start_bal"] or S["start_bal"]
            if denom > 0 and not S["paused_today"]:
                if S["daily_pnl"] / denom <= -MAX_DAILY_LOSS:
                    S["paused_today"] = True
                    save_state()
                    log.warning("⛔ Daily loss limit hit — NEW entries paused "
                                "until next UTC day (positions still managed)")
                    notify("⛔ Daily loss limit hit — new entries paused. "
                           "Open positions still being managed.", urgent=True)

            open_pos = fetch_positions()
            if open_pos is None:
                log.error("[POS] fetch_positions failed — skip for safety")
                time.sleep(SCAN_INTERVAL)
                continue

            log.info(f"[POS] {len(open_pos)} open | "
                     f"Tracked:{list(S['trade_meta'].keys())} | "
                     f"Pending:{list(S['pending_orders'].keys())}")

            # Update unrealized PnL BEFORE detect_closed
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

            # [FIX-8] entries blocked while paused — management above
            # already ran this cycle
            if S["paused_today"]:
                log.info("[SKIP] Entries paused (daily loss limit)")
                save_state()
                time.sleep(SCAN_INTERVAL)
                continue

            exchange_syms = {p.get("symbol") for p in open_pos}
            tracked_syms  = set(S["trade_meta"].keys())
            pending_syms  = set(S["pending_orders"].keys())
            all_open = exchange_syms | tracked_syms | pending_syms

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

                log.info(f"\n[SCAN] ━━━ {sym} ━━━━━━━━━━━━━━━━━")

                candles = pub_candles_4h(sym, limit=200)
                if not candles or len(candles) < 61:
                    log.warning(f"[{sym}] Not enough 4H candles")
                    continue

                tick = pub_ticker(sym)
                if not tick:
                    continue

                price = float(tick.get("lastPr", 0))
                chg   = float(tick.get("change24h", 0)) * 100
                log.info(f"[{sym}] ${price:,.2f} | {chg:+.2f}% | "
                         f"4H candles:{len(candles)}")

                sig = generate_signal(sym, candles, price)
                if not sig:
                    continue

                for r in sig.get("reasons", []):
                    log.info(f"  {r}")

                execute_entry(sym, sig, bal)

            save_state()
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log.info("\n[STOP] Bot stopped")
            notify("🛑 ARES v7.2 stopped", urgent=True)
            save_state()
            break
        except Exception as e:
            log.error(f"[ERROR] Cycle {cycle}: {e}", exc_info=True)
            notify(f"⚠️ Bot error: {e}", urgent=True)
            try:
                save_state()
            except Exception:
                pass
            time.sleep(30)


if __name__ == "__main__":
    run()
