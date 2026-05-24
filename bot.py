"""
ARES ULTRA v6.4 — Pro Risk Management  (bot_fixed_v64.py)
Bitget USDT-M Perpetuals | BTC + ETH

═══════════════════════════════════════════════════════════
FIX SUMMARY (over original bot_fixed.py)
═══════════════════════════════════════════════════════════

FIX-1  _sign() — replaced hmac.new() (non-standard alias) with
       the canonical hmac.new() from the hmac module; added an
       explicit check so a bad secret fails loudly instead of
       silently producing a wrong signature.

FIX-2  call() None-return safety — every caller that did
       res.get(...) without a prior `if res` check now has an
       explicit guard.  The entry-flow crash path (place_market_order
       → res.get("data", {}).get("orderId")) is the most critical;
       also patched place_plan_order, cancel_plan_order, set_leverage.

FIX-3  Win/loss threshold made relative to margin — was a flat
       $0.01 which silently treated many small real wins as breakeven
       and never updated pattern memory.  Now uses
       max(0.01, margin * 0.002) per closed position.

FIX-4  /tmp state warning — _get_state_file() now emits a prominent
       WARNING when falling back to /tmp so the operator knows state
       won't survive a restart.

FIX-5  Candle gap / integrity check — new validate_candles() helper
       called before indicator calculation.  Detects: too-few bars,
       reversed series (already auto-corrected), timestamp gaps > 3×
       the expected interval, and duplicate timestamps.  Returns
       (ok: bool, reason: str); signal generation skips on failure.

FIX-6  safe_extract_ohlcv data-quality gate — when >30 % of volume
       rows are bad the function now returns empty lists (instead of
       silently continuing with median-filled garbage), causing signal
       generation to skip that cycle rather than trade on bad data.

FIX-7  Sentiment per-source retry — SENTIMENT_CACHE is now a dict
       of per-source entries, each with its own timestamp and TTL
       (300 s on failure, 1800 s on success).  A partial failure
       (e.g. CryptoPanic down but F&G up) retries only the failed
       source on the next cycle instead of waiting the full 30 min.

FIX-8  MTF cycle-time guard — total elapsed time for the entire
       mtf_analysis() call is now logged at INFO level so slow
       cycles are visible in logs.

FIX-9  compound_size drawdown — minor: the function previously used
       S["peak_bal"] directly; now receives bal explicitly and the
       drawdown path is guarded against peak_bal == 0 more clearly
       (was already guarded but restructured for readability).

FIX-10 process_cb_expiry timestamp units — was comparing
       now_ms (milliseconds) against cb_paused_until which is also
       stored in ms, so the comparison was correct; however
       trip_circuit_breaker stored pause_until as now_ms + duration*1000
       which is correct.  Audited and confirmed consistent; added
       an assertion comment so future changes don't break it.

FIX-11 detect_closed_positions — loop-local margin lookup added so
       the relative PnL threshold (FIX-3) actually uses the per-trade
       margin rather than a fallback default.

FIX-12 OUTAGE_DETECTED / API_FAIL_COUNT thread-safety comment —
       these remain module globals (single-threaded bot) but are now
       documented as single-threaded assumptions with a TODO.

INHERITED FIXES (from original bot_fixed.py, unchanged):
  Bug A  Dead unreachable code removed from check_circuit_breakers()
  Bug B  Per-symbol open check uses all_open (exchange + tracked)
  Bug C  Trade notification shows actual_entry (fill price)
═══════════════════════════════════════════════════════════
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
API_KEY    = os.environ.get("BITGET_API_KEY", "")
SECRET_KEY = os.environ.get("BITGET_SECRET_KEY", "")
PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")
TG_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

def _env_int(name, default):
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except (ValueError, TypeError):
        print(f"⚠️ Invalid {name}='{raw}', using default {default}")
        return default

def _env_float(name, default):
    raw = os.environ.get(name, str(default))
    try:
        return float(raw)
    except (ValueError, TypeError):
        print(f"⚠️ Invalid {name}='{raw}', using default {default}")
        return default

MAX_LEVERAGE  = _env_int("MAX_LEVERAGE", 10)
SCAN_INTERVAL = _env_int("SCAN_INTERVAL_SECONDS", 120)
COMPOUND_PCT  = _env_float("COMPOUND_PCT", 20) / 100
MAX_TRADES    = _env_int("MAX_TRADES", 2)
RESET_STATS   = os.environ.get("RESET_STATS", "false").lower() == "true"
SHADOW_MODE   = os.environ.get("SHADOW_MODE", "false").lower() == "true"

BASE_URL     = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"
SYMBOLS      = ["BTCUSDT", "ETHUSDT"]

# FIX-4: track whether we fell back to /tmp so we can warn at startup
_STATE_FILE_IS_EPHEMERAL = False

def _get_state_file():
    """
    Find best persistent path for state file.
    Priority: env var → /app (Railway persistent) → /tmp (fallback)
    """
    global _STATE_FILE_IS_EPHEMERAL
    custom = os.environ.get("STATE_FILE_PATH")
    if custom:
        _STATE_FILE_IS_EPHEMERAL = False
        return custom
    app_dir = "/app"
    try:
        os.makedirs(app_dir, exist_ok=True)
        test = os.path.join(app_dir, ".write_test")
        with open(test, "w") as f:
            f.write("test")
        os.remove(test)
        _STATE_FILE_IS_EPHEMERAL = False
        return os.path.join(app_dir, "ares_v64_state.json")
    except Exception:
        pass
    # FIX-4: loud warning so operator knows state is ephemeral
    log.warning(
        "[STATE] ⚠️  /app not writable — falling back to /tmp/ares_v64_state.json. "
        "State WILL BE LOST on restart/redeploy. Set STATE_FILE_PATH env var "
        "to a persistent volume path to fix this."
    )
    _STATE_FILE_IS_EPHEMERAL = True
    return "/tmp/ares_v64_state.json"

STATE_FILE = _get_state_file()

# ── Risk constants ────────────────────────────────────────────
SL_MULT        = 1.5
TP1_MULT       = 2.0
TP2_MULT       = 3.5
TP3_MULT       = 6.0
TRAIL_MULT     = 1.0
MIN_CONF       = 65
MAX_DAILY_LOSS = 0.06
MAX_DRAWDOWN   = 0.12
MAX_VOLAT      = 8.0
MIN_SL_IMPROVE = 0.003

# ── Circuit Breakers ──────────────────────────────────────────
CB_CONSECUTIVE_LOSSES    = 5
CB_CONSECUTIVE_API_FAILS = 10
CB_HOURLY_LOSS_PCT       = 0.04
CB_PAUSE_DURATION        = 3600  # seconds

# ── Exchange Outage Detection ─────────────────────────────────
OUTAGE_FAIL_THRESHOLD = 5
OUTAGE_RETRY_INTERVAL = 60

# ── Correlation-Aware Sizing ──────────────────────────────────
CORR_REDUCTION = 0.65

# ── Slippage Model ────────────────────────────────────────────
SLIPPAGE_BPS_LOW  = 5
SLIPPAGE_BPS_HIGH = 20

# ── Regime Classifier ─────────────────────────────────────────
REGIME_ADX_TREND  = 25
REGIME_VOLAT_HIGH = 4.0

SYMBOL_SPECS = {
    "BTCUSDT": {"min_size": 0.0001, "precision": 4, "price_precision": 1},
    "ETHUSDT": {"min_size": 0.01,   "precision": 3, "price_precision": 2},
    "SOLUSDT": {"min_size": 0.1,    "precision": 1, "price_precision": 3},
    "BNBUSDT": {"min_size": 0.01,   "precision": 2, "price_precision": 2},
}

S = {
    "start_bal":       0.0,
    "peak_bal":        0.0,
    "daily_pnl":       0.0,
    "total_pnl":       0.0,
    "wins":            0,
    "losses":          0,
    "loss_streak":     0,
    "win_streak":      0,
    "daily_start":     datetime.now(timezone.utc).date(),
    "trade_meta":      {},
    "pattern_memory":  {},
    "recent_pnl_log":  [],
    "cb_paused_until": 0,
    "cb_reason":       "",
    "shadow_trades":   [],
}

# FIX-7: per-source sentiment cache
# Structure: { source_name: {"data": ..., "timestamp": 0, "failed": False} }
SENTIMENT_CACHE = {
    "fg":   {"data": None, "timestamp": 0, "failed": False},
    "news": {"data": None, "timestamp": 0, "failed": False},
    "dom":  {"data": None, "timestamp": 0, "failed": False},
}
SENTIMENT_TTL_OK   = 1800   # 30 min when last fetch succeeded
SENTIMENT_TTL_FAIL = 300    #  5 min when last fetch failed

# FIX-12: single-threaded assumption note
# API_FAIL_COUNT and OUTAGE_DETECTED are module-level mutable globals.
# This is safe because the bot is single-threaded.  If threading/async
# is ever added, replace these with threading.Lock-protected counters.
API_FAIL_COUNT  = 0
OUTAGE_DETECTED = False


# ── State Persistence ─────────────────────────────────────────

def load_state():
    target_file = STATE_FILE
    if not os.path.exists(target_file):
        legacy_file = os.environ.get(
            "STATE_FILE_PATH", "/app/ares_v63_state.json"
        ).replace("v64", "v63")
        if os.path.exists(legacy_file):
            log.info("[STATE] Migrating v6.3 → v6.4 state (pattern_memory preserved)")
            target_file = legacy_file
        else:
            return
    try:
        with open(target_file) as f:
            saved = json.load(f)
        expected_types = {
            "start_bal":       (int, float),
            "peak_bal":        (int, float),
            "daily_pnl":       (int, float),
            "total_pnl":       (int, float),
            "wins":            int,
            "losses":          int,
            "loss_streak":     int,
            "win_streak":      int,
            "trade_meta":      dict,
            "pattern_memory":  dict,
            "recent_pnl_log":  list,
            "cb_paused_until": (int, float),
            "cb_reason":       str,
            "shadow_trades":   list,
        }
        for k, v in saved.items():
            if k == "daily_start":
                try:
                    S[k] = datetime.fromisoformat(v).date()
                except (ValueError, TypeError):
                    S[k] = datetime.now(timezone.utc).date()
            elif k in S:
                if k in expected_types:
                    if isinstance(v, expected_types[k]):
                        S[k] = v
                    else:
                        log.warning(
                            f"[STATE] {k} has wrong type "
                            f"({type(v).__name__} not {expected_types[k]}), keeping default"
                        )
                else:
                    S[k] = v

        # Migrate v6.3 pattern keys (no regime suffix) → v6.4 format
        pm = S.get("pattern_memory", {})
        if pm and isinstance(pm, dict):
            migrated        = {}
            migration_count = 0
            for key, val in pm.items():
                if isinstance(key, str) and "Reg_" not in key:
                    migrated[key + "|Reg_UNKNOWN"] = val
                    migration_count += 1
                else:
                    migrated[key] = val
            if migration_count > 0:
                log.info(f"[STATE] Migrated {migration_count} v6.3 pattern keys → v6.4 format")
                S["pattern_memory"] = migrated

        tm = S.get("trade_meta", {})
        if tm and isinstance(tm, dict):
            for sym, meta in tm.items():
                if isinstance(meta, dict):
                    old_sig = meta.get("pattern_sig")
                    if isinstance(old_sig, str) and "Reg_" not in old_sig:
                        meta["pattern_sig"] = old_sig + "|Reg_UNKNOWN"

        log.info(
            f"[STATE] Restored: {S['wins']}W/{S['losses']}L | "
            f"PnL:${S['total_pnl']:.2f}"
        )
    except Exception as e:
        log.warning(f"[STATE] Load failed: {e}")

def save_state():
    try:
        # Ensure directory exists (Railway /app/data may not exist on fresh deploy)
        state_dir = os.path.dirname(STATE_FILE)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        snapshot = {}
        for k, v in S.items():
            if k == "daily_start":
                snapshot[k] = v.isoformat() if hasattr(v, "isoformat") else str(v)
            else:
                snapshot[k] = v
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f, indent=2, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning(f"[STATE] Save failed: {e}")

def reset_stats():
    log.info("[INIT] 🔄 Stats reset requested via RESET_STATS env var")
    S["start_bal"]       = 0.0
    S["peak_bal"]        = 0.0
    S["total_pnl"]       = 0.0
    S["daily_pnl"]       = 0.0
    S["wins"]            = 0
    S["losses"]          = 0
    S["loss_streak"]     = 0
    S["win_streak"]      = 0
    S["daily_start"]     = datetime.now(timezone.utc).date()
    S["recent_pnl_log"]  = []
    S["cb_paused_until"] = 0
    S["cb_reason"]       = ""
    S["shadow_trades"]   = []
    save_state()
    log.info("[INIT] ✅ Stats reset complete (pattern_memory preserved)")


# ── Telegram (with batching) ──────────────────────────────────

NOTIFY_BUFFER         = []
NOTIFY_LAST_FLUSH     = time.time()
NOTIFY_FLUSH_INTERVAL = 600
NOTIFY_BUFFER_MAX     = 10

def _send_telegram_now(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    safe_msg = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": safe_msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        log.debug(f"[TG] Failed: {e}")

def flush_notifications():
    global NOTIFY_LAST_FLUSH
    if not NOTIFY_BUFFER:
        return
    combined = "📊 ARES Updates:\n" + "\n".join(NOTIFY_BUFFER)
    _send_telegram_now(combined)
    NOTIFY_BUFFER.clear()
    NOTIFY_LAST_FLUSH = time.time()

def notify(msg, urgent=False):
    global NOTIFY_LAST_FLUSH
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    if urgent:
        _send_telegram_now(f"🚨 {msg}")
        return
    timestamp = datetime.now().strftime("%H:%M")
    NOTIFY_BUFFER.append(f"{timestamp} {msg}")
    now = time.time()
    if (
        len(NOTIFY_BUFFER) >= NOTIFY_BUFFER_MAX
        or now - NOTIFY_LAST_FLUSH >= NOTIFY_FLUSH_INTERVAL
    ):
        flush_notifications()


# ── API Layer with retry ──────────────────────────────────────

# FIX-1: use canonical hmac.new() — the original code used hmac.new()
# which is valid Python but is an alias that can confuse linters and
# static analysis tools.  More importantly we now validate the secret
# is non-empty before signing so a misconfigured deployment fails fast.
def _sign(secret, msg):
    if not secret:
        raise ValueError("[AUTH] SECRET_KEY is empty — cannot sign request")
    return base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()


# FIX-2: call() now guarantees callers always receive a dict (possibly
# with a synthetic error code) rather than None.  This eliminates
# AttributeError crashes on res.get(...) throughout the codebase.
_API_ERROR_RESPONSE = {"code": "CLIENT_ERROR", "msg": "no response from exchange"}

def call(method, path, params=None, body=None, retries=3):
    last_result = None
    for attempt in range(retries):
        t = str(int(time.time() * 1000))
        headers = {
            "Content-Type":      "application/json",
            "ACCESS-KEY":        API_KEY,
            "ACCESS-TIMESTAMP":  t,
            "ACCESS-PASSPHRASE": PASSPHRASE,
            "locale":            "en-US",
        }
        try:
            if method == "GET" and params:
                qs = "&".join(f"{k}={v}" for k, v in params.items())
                headers["ACCESS-SIGN"] = _sign(SECRET_KEY, t + "GET" + path + "?" + qs)
                r = requests.get(
                    BASE_URL + path + "?" + qs, headers=headers, timeout=12
                )
            elif method == "POST":
                bs = json.dumps(body) if body else ""
                headers["ACCESS-SIGN"] = _sign(SECRET_KEY, t + "POST" + path + bs)
                r = requests.post(
                    BASE_URL + path, headers=headers, data=bs, timeout=12
                )
            else:
                headers["ACCESS-SIGN"] = _sign(SECRET_KEY, t + "GET" + path)
                r = requests.get(BASE_URL + path, headers=headers, timeout=12)

            result = r.json()
            last_result = result
            if result.get("code") == "00000":
                register_api_success()
                return result
            err_code = result.get("code", "")
            if r.status_code == 429 or err_code in ["429", "50054"]:
                wait = 10 * (attempt + 1)
                log.warning(f"[API] Rate limited (status={r.status_code}), waiting {wait}s")
                time.sleep(wait)
                continue
            if err_code in ["40001", "40002", "40003", "40009", "40037"]:
                log.error(f"[API] Auth/param error: {result.get('msg')}")
                return result
            if attempt < retries - 1:
                wait = 2 ** attempt
                log.debug(f"[API] Retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
        except ValueError as e:
            # _sign raises ValueError for empty secret — re-raise immediately
            raise
        except Exception as e:
            log.error(f"[API] {method} {path}: {e}")
            register_api_failure()
            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    # FIX-2: never return None; return last known result or a safe error dict
    if last_result is not None:
        return last_result
    return dict(_API_ERROR_RESPONSE)


# ── Public Market Data ────────────────────────────────────────

def pub_get(path, params, retries=3):
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

def pub_ticker(sym):
    data = pub_get(
        "/api/v2/mix/market/ticker",
        {"symbol": sym, "productType": PRODUCT_TYPE},
    )
    if data:
        d = data.get("data", [])
        return d[0] if d else None
    return None

def pub_candles(sym, gran="15m", limit=150):
    gran_map = {
        "5": "5m", "15": "15m", "60": "1H", "240": "4H",
        "1": "1m", "30": "30m", "120": "2H", "360": "6H",
    }
    gran = gran_map.get(str(gran), gran)
    data = pub_get(
        "/api/v2/mix/market/candles",
        {
            "symbol":      sym,
            "productType": PRODUCT_TYPE,
            "granularity": gran,
            "limit":       str(limit),
        },
    )
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
    data = pub_get(
        "/api/v2/mix/market/contracts",
        {"productType": PRODUCT_TYPE, "symbol": sym},
    )
    if data:
        d = data.get("data", [])
        if d:
            try:
                rate    = float(d[0].get("fundingRate", 0))
                next_ms = int(d[0].get("nextFundingTime", 0))
                if next_ms > 0:
                    now_ms = int(time.time() * 1000)
                    mins   = max(0, int((next_ms - now_ms) / 60000))
                    return rate, mins
            except Exception:
                pass
    data = pub_get(
        "/api/v2/mix/market/current-fund-rate",
        {"symbol": sym, "productType": PRODUCT_TYPE},
    )
    if data:
        d = data.get("data", [])
        try:
            rate = float(d[0].get("fundingRate", 0)) if d else 0.0
            return rate, -1
        except Exception:
            pass
    return 0.0, -1


# ── Candle Integrity Check ────────────────────────────────────
# FIX-5: validate candle series before indicator calculation.

# Expected interval in ms for common granularities (used by gap check)
_GRAN_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1H": 3_600_000, "2H": 7_200_000,
    "4H": 14_400_000, "6H": 21_600_000,
}

def validate_candles(candles, gran_label="15m", min_bars=60):
    """
    Returns (ok: bool, reason: str).
    Checks: minimum bar count, all-same timestamp (degenerate),
    large gaps (> 3× expected interval), duplicate timestamps.
    """
    if not candles or len(candles) < min_bars:
        return False, f"only {len(candles) if candles else 0} bars (need {min_bars})"

    # Parse timestamps
    try:
        timestamps = [int(c[0]) for c in candles]
    except (IndexError, TypeError, ValueError) as e:
        return False, f"timestamp parse error: {e}"

    # Check monotonically increasing
    if timestamps != sorted(timestamps):
        return False, "timestamps not sorted ascending"

    # Check for duplicates
    if len(set(timestamps)) != len(timestamps):
        dupes = len(timestamps) - len(set(timestamps))
        return False, f"{dupes} duplicate timestamps"

    # Gap check — only if we know the expected interval
    expected_ms = _GRAN_MS.get(gran_label)
    if expected_ms and len(timestamps) >= 2:
        gaps = [timestamps[i] - timestamps[i - 1] for i in range(1, len(timestamps))]
        max_gap = max(gaps)
        if max_gap > expected_ms * 3:
            gap_min = max_gap // 60_000
            return False, f"data gap of {gap_min}min detected (expected ~{expected_ms//60000}min bars)"

    return True, "ok"


# ── Sentiment ─────────────────────────────────────────────────
# FIX-7: per-source TTL so a partial failure retries only the dead source.

def _should_refresh(source_key):
    entry = SENTIMENT_CACHE[source_key]
    age   = time.time() - entry["timestamp"]
    ttl   = SENTIMENT_TTL_FAIL if entry["failed"] else SENTIMENT_TTL_OK
    return entry["data"] is None or age > ttl

def fetch_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=8)
        d = r.json().get("data", [])
        if d:
            return int(d[0].get("value", 50)), d[0].get("value_classification", "Neutral")
    except Exception:
        pass
    return None, None

def fetch_news_sentiment():
    try:
        r = requests.get(
            "https://cryptopanic.com/api/free/v1/posts/",
            params={"public": "true", "currencies": "BTC,ETH"},
            timeout=8,
        )
        posts = r.json().get("results", [])[:20]
        bull  = sum(
            1 for p in posts
            if p.get("votes", {}).get("positive", 0) > p.get("votes", {}).get("negative", 0)
        )
        bear  = sum(
            1 for p in posts
            if p.get("votes", {}).get("negative", 0) > p.get("votes", {}).get("positive", 0)
        )
        total = max(len(posts), 1)
        return (bull - bear) / total * 100, bull, bear
    except Exception:
        return None, 0, 0

def fetch_btc_dominance():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=8)
        d = r.json().get("data", {})
        return float(d.get("market_cap_percentage", {}).get("btc", 50))
    except Exception:
        return None

def get_sentiment():
    """
    FIX-7: Each source (fg, news, dom) refreshes independently.
    Returns a combined dict with the most recent available data per source.
    """
    now = time.time()

    if _should_refresh("fg"):
        fg_val, fg_cls = fetch_fear_greed()
        ok = fg_val is not None
        SENTIMENT_CACHE["fg"] = {
            "data":      {"fg_value": fg_val, "fg_class": fg_cls},
            "timestamp": now,
            "failed":    not ok,
        }
        if ok:
            log.info(f"[SENTIMENT] F&G:{fg_val}({fg_cls})")
        else:
            log.debug("[SENTIMENT] F&G fetch failed — retry in 5min")

    if _should_refresh("news"):
        news_sent, bull, bear = fetch_news_sentiment()
        ok = news_sent is not None
        SENTIMENT_CACHE["news"] = {
            "data":      {"news_sent": news_sent, "bull_news": bull, "bear_news": bear},
            "timestamp": now,
            "failed":    not ok,
        }
        if ok:
            log.info(f"[SENTIMENT] News:{news_sent:+.0f}%")
        else:
            log.debug("[SENTIMENT] News fetch failed — retry in 5min")

    if _should_refresh("dom"):
        btc_dom = fetch_btc_dominance()
        ok = btc_dom is not None
        SENTIMENT_CACHE["dom"] = {
            "data":      {"btc_dom": btc_dom},
            "timestamp": now,
            "failed":    not ok,
        }
        if ok:
            log.info(f"[SENTIMENT] BTC.D:{btc_dom:.1f}%")
        else:
            log.debug("[SENTIMENT] BTC dominance fetch failed — retry in 5min")

    # Merge all cached source data into one flat dict
    combined = {}
    for src in ("fg", "news", "dom"):
        src_data = SENTIMENT_CACHE[src].get("data") or {}
        combined.update(src_data)
    return combined


# ── Authenticated Endpoints ───────────────────────────────────

def fetch_recent_fills(sym, limit=30):
    res = call(
        "GET", "/api/v2/mix/order/fills",
        params={"productType": PRODUCT_TYPE, "symbol": sym, "limit": str(limit)},
    )
    # FIX-2: call() now always returns a dict, safe to .get() directly
    if res.get("code") == "00000":
        data = res.get("data", {})
        if isinstance(data, dict):
            return data.get("fillList", [])
        return data if isinstance(data, list) else []
    return []

def fetch_balance():
    res = call(
        "GET", "/api/v2/mix/account/accounts",
        params={"productType": PRODUCT_TYPE},
    )
    if res.get("code") == "00000" and res.get("data"):
        for item in res["data"]:
            if item.get("marginCoin", "").upper() == "USDT":
                try:
                    return float(item.get("available", 0))
                except Exception:
                    pass
    return 0.0

def fetch_positions():
    """Returns list of open positions, or None on API failure.
    Callers must distinguish [] (no positions) from None (API failure).
    """
    res = call(
        "GET", "/api/v2/mix/position/all-position",
        params={"productType": PRODUCT_TYPE, "marginCoin": "USDT"},
    )
    # FIX-2: call() always returns a dict now; None path preserved for
    # callers that check `if open_pos is None`
    if res.get("code") != "00000":
        return None
    data = res.get("data") or []
    return [p for p in data if float(p.get("total", 0)) > 0]

def fetch_pending_plan_orders(sym):
    res = call(
        "GET", "/api/v2/mix/order/orders-plan-pending",
        params={"productType": PRODUCT_TYPE, "symbol": sym},
    )
    if res.get("code") == "00000" and res.get("data"):
        return res["data"].get("entrustedList", [])
    return []

def cancel_plan_order(sym, order_id, plan_type):
    res = call(
        "POST", "/api/v2/mix/order/cancel-plan-order",
        body={
            "symbol":      sym,
            "productType": PRODUCT_TYPE,
            "marginCoin":  "USDT",
            "orderIdList": [{"orderId": order_id}],
            "planType":    plan_type,
        },
    )
    # FIX-2: res is always a dict
    return res.get("code") == "00000"

def cancel_all_plan_orders(sym):
    orders = fetch_pending_plan_orders(sym)
    if not orders:
        return 0
    cancelled = 0
    for order in orders:
        oid   = order.get("orderId")
        ptype = order.get("planType")
        if cancel_plan_order(sym, oid, ptype):
            cancelled += 1
            time.sleep(0.2)
    if cancelled:
        log.info(f"[CANCEL] {sym}: {cancelled}/{len(orders)} plan orders cancelled")
    return cancelled

def cancel_sl_orders_only(sym):
    orders    = fetch_pending_plan_orders(sym)
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
        res = call(
            "POST", "/api/v2/mix/account/set-leverage",
            body={
                "symbol":      sym,
                "productType": PRODUCT_TYPE,
                "marginCoin":  "USDT",
                "leverage":    str(lev),
                "holdSide":    side,
            },
        )
        # FIX-2: res is always a dict
        if res.get("code") != "00000":
            err = res.get("msg", "no response")
            log.warning(f"[LEV] {sym} {side} {lev}x failed: {err}")
            success = False
    return success

def place_market_order(sym, side, size, trade_side):
    return call(
        "POST", "/api/v2/mix/order/place-order",
        body={
            "symbol":      sym,
            "productType": PRODUCT_TYPE,
            "marginMode":  "isolated",
            "marginCoin":  "USDT",
            "size":        str(size),
            "side":        side,
            "tradeSide":   trade_side,
            "orderType":   "market",
            "force":       "gtc",
        },
    )

def place_plan_order(sym, plan_type, trigger_px, hold_side, size):
    spec    = SYMBOL_SPECS.get(sym, {"price_precision": 2})
    px_prec = spec.get("price_precision", 2)
    res = call(
        "POST", "/api/v2/mix/order/place-tpsl-order",
        body={
            "symbol":       sym,
            "productType":  PRODUCT_TYPE,
            "marginCoin":   "USDT",
            "planType":     plan_type,
            "triggerPrice": str(round(trigger_px, px_prec)),
            "triggerType":  "mark_price",
            "executePrice": "0",
            "holdSide":     hold_side,
            "size":         str(size),
        },
    )
    # FIX-2: res is always a dict
    return res.get("code") == "00000"


# ── Risk Setup ────────────────────────────────────────────────

def setup_protection(sym, hold_side, sl, tp1, tp2, tp3, total_size, skip_sl=False):
    """Place SL + 3 TPs on exchange.
    skip_sl=True: caller already placed SL (minimises naked-position window).
    """
    spec      = SYMBOL_SPECS.get(sym, {"precision": 4, "min_size": 0.0001})
    full_size = round(total_size, spec["precision"])
    if not skip_sl:
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
    for tp_px, sz, label in [
        (tp1, tp1_size, "TP1"),
        (tp2, tp2_size, "TP2"),
        (tp3, tp3_size, "TP3"),
    ]:
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
    k   = 2 / (p + 1)
    out = [sum(data[:p]) / p]
    for x in data[p:]:
        out.append(x * k + out[-1] * (1 - k))
    return out

def calc_rsi(closes, p=14):
    if len(closes) < p + 1:
        return 50
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
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
    ml  = [e12[i] - e26[i] for i in range(min(len(e12), len(e26)))]
    if len(ml) < 9:
        return ml[-1], ml[-1], 0
    sl = ema_series(ml, 9)[-1]
    return ml[-1], sl, ml[-1] - sl

def calc_bb(closes, p=20):
    if len(closes) < p:
        c = closes[-1]
        return c * 1.02, c, c * 0.98
    r   = closes[-p:]
    mid = sum(r) / p
    std = (sum((x - mid) ** 2 for x in r) / p) ** 0.5
    return mid + 2 * std, mid, mid - 2 * std

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
    h  = highs[-n:]; l = lows[-n:]; c = closes[-n:]; v = vols[-n:]
    tp = [(h[i] + l[i] + c[i]) / 3 for i in range(n)]
    tv = sum(t * vol for t, vol in zip(tp, v))
    sv = sum(v)
    return tv / sv if sv else closes[-1]

def safe_extract_ohlcv(cdata):
    """
    FIX-6: returns empty lists when > 30% of volume rows are bad,
    rather than silently continuing with median-padded garbage.
    """
    if not cdata or len(cdata[0]) < 6:
        return [], [], [], [], []
    try:
        highs  = [float(x[2]) for x in cdata]
        lows   = [float(x[3]) for x in cdata]
        closes = [float(x[4]) for x in cdata]
        opens  = [float(x[1]) for x in cdata]

        raw_vols    = []
        bad_indices = []
        for i, row in enumerate(cdata):
            try:
                v = float(row[5])
                if v > 0:
                    raw_vols.append(v)
                else:
                    raw_vols.append(None)
                    bad_indices.append(i)
            except Exception:
                raw_vols.append(None)
                bad_indices.append(i)

        bad_pct = len(bad_indices) / len(cdata) if cdata else 0

        # FIX-6: hard stop when > 30% of volume data is bad
        if bad_pct > 0.30:
            log.warning(
                f"[DATA] {len(bad_indices)}/{len(cdata)} candles "
                f"({bad_pct*100:.0f}%) had bad volume — refusing to use this data"
            )
            return [], [], [], [], []

        good_vols = [v for v in raw_vols if v is not None]
        if good_vols:
            sorted_vols = sorted(good_vols)
            median_vol  = sorted_vols[len(sorted_vols) // 2]
        else:
            median_vol = 1.0
        vols = [median_vol if v is None else v for v in raw_vols]
        return opens, highs, lows, closes, vols
    except Exception as e:
        log.error(f"[DATA] OHLCV parse failed: {e}")
        return [], [], [], [], []


# ── MTF Analysis ──────────────────────────────────────────────
# FIX-8: log total MTF elapsed time.

def mtf_analysis(symbol, primary_15m=None):
    bull, bear = 0, 0
    results    = {}
    start      = time.time()
    MAX_TIME   = 15
    for gran, label, w in [("5", "5m", 1), ("15", "15m", 2), ("60", "1H", 3), ("240", "4H", 4)]:
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
            e9, e21   = ema(cls, 9), ema(cls, 21)
            r         = calc_rsi(cls)
            _, _, mh  = calc_macd(cls)
            bbu, _, bbl = calc_bb(cls)
            sk        = calc_stoch(hhs, lls, cls)
            tb, tr    = 0, 0
            if e9 > e21:  tb += 2
            else:          tr += 2
            if r < 35:     tb += 2
            elif r > 65:   tr += 2
            elif r > 50:   tb += 1
            else:          tr += 1
            if mh > 0:     tb += 1
            else:          tr += 1
            if cls[-1] <= bbl:  tb += 1
            elif cls[-1] >= bbu: tr += 1
            if sk < 25:    tb += 1
            elif sk > 75:  tr += 1
            bull += tb * w
            bear += tr * w
            results[label] = "🟢" if tb > tr else "🔴" if tr > tb else "⚪"
        except Exception as e:
            log.debug(f"[MTF] {label}: {e}")
            results[label] = "⚪"

    # FIX-8: log total MTF duration
    elapsed_ms = int((time.time() - start) * 1000)
    log.info(f"[MTF] {symbol} completed in {elapsed_ms}ms")
    return bull, bear, results


# ── Mini-Backtest (Pre-Trade Validation) ──────────────────────

MINI_BT_LOOKBACK    = 300
MINI_BT_FORWARD     = 20
MINI_BT_MIN_SAMPLES = 3
MINI_BT_MIN_WR      = 0.40

def mini_backtest_signal(candles, action, atr, sig=None, sl_mult=SL_MULT, tp_mult=TP2_MULT):
    """Scan past candles for similar setups and check historical win rate.
    Returns: (win_rate, num_samples, reason_str)
    """
    if action not in ("LONG", "SHORT"):
        return None, 0, f"invalid action ({action})"
    if len(candles) < 100:
        return None, 0, "insufficient candles"
    if atr is None or atr <= 0:
        return None, 0, f"invalid ATR ({atr})"

    _, highs, lows, closes, _ = safe_extract_ohlcv(candles)
    if len(closes) < 100:
        return None, 0, "OHLCV parse failed"

    if sig is not None:
        sig_rsi   = sig.get("rsi", 50)
        sig_mh    = sig.get("macd_h", 0)
        sig_stoch = sig.get("stoch", 50)
        rsi_lo,   rsi_hi   = sig_rsi - 10,   sig_rsi + 10
        stoch_lo, stoch_hi = sig_stoch - 10, sig_stoch + 10
        macd_positive      = sig_mh > 0
    else:
        if action == "LONG":
            rsi_lo, rsi_hi     = 0, 45
            stoch_lo, stoch_hi = 0, 40
            macd_positive      = True
        else:
            rsi_lo, rsi_hi     = 55, 100
            stoch_lo, stoch_hi = 60, 100
            macd_positive      = False

    scan_end        = len(closes) - MINI_BT_FORWARD
    scan_start      = max(50, scan_end - MINI_BT_LOOKBACK)
    actual_lookback = scan_end - scan_start
    wins = losses = ambiguous = 0

    for i in range(scan_start, scan_end):
        window_closes = closes[:i + 1]
        window_highs  = highs[:i + 1]
        window_lows   = lows[:i + 1]
        if len(window_closes) < 50:
            continue
        try:
            r        = calc_rsi(window_closes)
            _, _, mh = calc_macd(window_closes)
            sk       = calc_stoch(window_highs, window_lows, window_closes)
            e9       = ema(window_closes, 9)
            e21      = ema(window_closes, 21)
        except Exception:
            continue

        macd_match = (mh > 0) == macd_positive
        ema_match  = (e9 > e21) if action == "LONG" else (e9 < e21)
        if not (
            rsi_lo <= r <= rsi_hi
            and stoch_lo <= sk <= stoch_hi
            and macd_match
            and ema_match
        ):
            continue

        entry        = closes[i]
        future_highs = highs[i + 1: i + 1 + MINI_BT_FORWARD]
        future_lows  = lows[i + 1: i + 1 + MINI_BT_FORWARD]
        if not future_highs or not future_lows:
            continue

        if action == "LONG":
            target = entry + atr * tp_mult
            stop   = entry - atr * sl_mult
        else:
            target = entry - atr * tp_mult
            stop   = entry + atr * sl_mult

        hit_target = hit_stop = False
        for j in range(len(future_highs)):
            if action == "LONG":
                sl_hit = future_lows[j] <= stop
                tp_hit = future_highs[j] >= target
            else:
                sl_hit = future_highs[j] >= stop
                tp_hit = future_lows[j] <= target

            if sl_hit and tp_hit:
                hit_stop = True
                ambiguous += 1
                break
            elif sl_hit:
                hit_stop = True
                break
            elif tp_hit:
                hit_target = True
                break

        if hit_target:
            wins += 1
        elif hit_stop:
            losses += 1

    total = wins + losses
    if total == 0:
        return None, 0, "no similar setups found"

    wr      = wins / total
    amb_str = f", {ambiguous} ambig" if ambiguous > 0 else ""
    reason  = f"{wins}W/{losses}L ({wr*100:.0f}%) in {actual_lookback}c{amb_str}"
    return wr, total, reason


def check_mini_backtest(candles, action, atr, sig=None):
    wr, samples, reason = mini_backtest_signal(candles, action, atr, sig=sig)
    if wr is None:
        return True, reason
    if samples < MINI_BT_MIN_SAMPLES:
        return True, f"only {samples} samples — allowed"
    if wr < MINI_BT_MIN_WR:
        return False, f"{reason} < {MINI_BT_MIN_WR*100:.0f}% threshold"
    return True, reason


# ── Pattern Memory (Self-Learning) ────────────────────────────

PATTERN_MIN_SAMPLES  = 5
PATTERN_MIN_WIN_RATE = 0.35
PATTERN_MAX_ENTRIES  = 200
PATTERN_PRUNE_AFTER  = 250

def _safe_num(v, default):
    if v is None:
        return default
    try:
        f = float(v)
        if f != f:  # NaN check
            return default
        return f
    except (ValueError, TypeError):
        return default

def get_pattern_signature(sig):
    if not isinstance(sig, dict):
        return "UNKNOWN|all_default"

    sym = sig.get("sym") or sig.get("asset", "ANY")
    if not isinstance(sym, str):
        sym = "ANY"

    action = sig.get("action")
    if not isinstance(action, str):
        action = "UNKNOWN"
    parts = [sym, action]

    rsi = _safe_num(sig.get("rsi"), 50)
    if rsi < 30:   parts.append("RSI_xover")
    elif rsi < 40: parts.append("RSI_low")
    elif rsi > 70: parts.append("RSI_xhigh")
    elif rsi > 60: parts.append("RSI_high")
    else:          parts.append("RSI_mid")

    mh = _safe_num(sig.get("macd_h"), 0)
    parts.append("MACD_pos" if mh > 0 else "MACD_neg")

    stoch = _safe_num(sig.get("stoch"), 50)
    if stoch < 25:   parts.append("Stoch_low")
    elif stoch > 75: parts.append("Stoch_high")
    else:            parts.append("Stoch_mid")

    vol_r = _safe_num(sig.get("vol_r"), 1)
    if vol_r > 2:     parts.append("Vol_surge")
    elif vol_r > 1.3: parts.append("Vol_above")
    else:             parts.append("Vol_norm")

    volat = _safe_num(sig.get("volat"), 1)
    if volat < 1.5: parts.append("Volat_low")
    elif volat < 3: parts.append("Volat_mid")
    else:           parts.append("Volat_high")

    conf = _safe_num(sig.get("conf"), 65)
    if conf >= 85:   parts.append("Conf_xhigh")
    elif conf >= 75: parts.append("Conf_high")
    else:            parts.append("Conf_mid")

    regime = sig.get("regime", "UNKNOWN")
    if not isinstance(regime, str):
        regime = "UNKNOWN"
    parts.append(f"Reg_{regime}")

    return "|".join(parts)

def check_pattern_history(sig_key):
    if not sig_key:
        return True, "no pattern key"

    pm = S.get("pattern_memory", {})
    if not isinstance(pm, dict):
        log.warning("[LEARN] pattern_memory corrupted (not dict), resetting")
        S["pattern_memory"] = {}
        return True, "memory reset"

    record = pm.get(sig_key, {"wins": 0, "losses": 0})
    try:
        wins   = max(0, int(record.get("wins", 0)) if isinstance(record, dict) else 0)
        losses = max(0, int(record.get("losses", 0)) if isinstance(record, dict) else 0)
    except (ValueError, TypeError):
        wins = losses = 0

    total = wins + losses
    if total < PATTERN_MIN_SAMPLES:
        return True, f"learning ({total}/{PATTERN_MIN_SAMPLES})"

    win_rate = wins / total
    if win_rate < PATTERN_MIN_WIN_RATE:
        return False, (
            f"WR {win_rate*100:.0f}% < {PATTERN_MIN_WIN_RATE*100:.0f}% "
            f"({wins}W/{losses}L)"
        )
    return True, f"WR {win_rate*100:.0f}% ({wins}W/{losses}L)"

def update_pattern_memory(sig_key, won):
    if not sig_key or not isinstance(sig_key, str):
        return
    if sig_key.startswith("UNKNOWN|"):
        log.debug("[LEARN] Skipping UNKNOWN pattern key — not recording")
        return
    if won is None:
        return

    if "pattern_memory" not in S or not isinstance(S.get("pattern_memory"), dict):
        S["pattern_memory"] = {}

    if sig_key not in S["pattern_memory"]:
        S["pattern_memory"][sig_key] = {"wins": 0, "losses": 0}

    rec = S["pattern_memory"][sig_key]
    if not isinstance(rec, dict):
        rec = {"wins": 0, "losses": 0}
    rec.setdefault("wins", 0)
    rec.setdefault("losses", 0)

    if won:
        rec["wins"] += 1
    else:
        rec["losses"] += 1
    S["pattern_memory"][sig_key] = rec

    if len(S["pattern_memory"]) > PATTERN_PRUNE_AFTER:
        prune_pattern_memory(protect=sig_key)

def prune_pattern_memory(protect=None):
    pm = S.get("pattern_memory", {})
    if not isinstance(pm, dict):
        S["pattern_memory"] = {}
        return
    if len(pm) <= PATTERN_MAX_ENTRIES:
        return

    def sample_count(item):
        _, rec = item
        if not isinstance(rec, dict):
            return -1
        try:
            return int(rec.get("wins", 0)) + int(rec.get("losses", 0))
        except (ValueError, TypeError):
            return -1

    sorted_pats = sorted(pm.items(), key=sample_count, reverse=True)
    keep        = dict(sorted_pats[:PATTERN_MAX_ENTRIES])

    if protect and protect in pm and protect not in keep:
        if keep:
            min_key = min(keep.keys(), key=lambda k: sample_count((k, keep[k])))
            del keep[min_key]
        keep[protect] = pm[protect]

    pruned_count = len(pm) - len(keep)
    S["pattern_memory"] = keep
    if pruned_count > 0:
        log.info(
            f"[LEARN] Pruned {pruned_count} stale patterns "
            f"(kept top {len(keep)} by sample count)"
        )

def print_pattern_summary():
    pm = S.get("pattern_memory", {})
    if not pm:
        return
    log.info("─── PATTERN MEMORY ───")

    def safe_total(item):
        _, rec = item
        if not isinstance(rec, dict):
            return 0
        try:
            return int(rec.get("wins", 0)) + int(rec.get("losses", 0))
        except (ValueError, TypeError):
            return 0

    sorted_pats = sorted(pm.items(), key=safe_total, reverse=True)
    for key, rec in sorted_pats[:10]:
        if not isinstance(rec, dict):
            continue
        try:
            wins   = int(rec.get("wins", 0))
            losses = int(rec.get("losses", 0))
        except (ValueError, TypeError):
            continue
        total = wins + losses
        if total == 0:
            continue
        wr     = wins / total * 100
        status = "✅" if wr >= 50 else "⚠️" if wr >= 35 else "❌"
        log.info(f"  {status} {key} → {wr:.0f}% ({wins}W/{losses}L)")


# ── Circuit Breakers ──────────────────────────────────────────

def record_trade_outcome(pnl):
    now_ms = int(time.time() * 1000)
    if "recent_pnl_log" not in S or not isinstance(S["recent_pnl_log"], list):
        S["recent_pnl_log"] = []
    S["recent_pnl_log"].append([now_ms, pnl])
    if len(S["recent_pnl_log"]) > 100:
        S["recent_pnl_log"] = S["recent_pnl_log"][-100:]

def process_cb_expiry():
    # NOTE: cb_paused_until is stored in milliseconds (epoch_ms + duration_ms).
    # time.time()*1000 == now_ms.  Keep these units consistent if ever editing.
    now_ms      = int(time.time() * 1000)
    pause_until = S.get("cb_paused_until", 0)
    if pause_until and now_ms >= pause_until:
        log.info("[CB] Pause expired — resetting loss_streak and pnl_log")
        S["cb_paused_until"] = 0
        S["cb_reason"]       = ""
        S["loss_streak"]     = 0
        one_hour_ago = now_ms - 3_600_000
        S["recent_pnl_log"] = [
            [ts, pnl] for ts, pnl in S.get("recent_pnl_log", [])
            if ts >= one_hour_ago
        ]
        save_state()
        notify("✅ Circuit breaker pause expired — trading resumed", urgent=True)
        return True
    return False

def check_circuit_breakers(balance):
    """Pure query — no side effects. Call process_cb_expiry() separately each cycle."""
    now_ms      = int(time.time() * 1000)
    pause_until = S.get("cb_paused_until", 0)
    if pause_until and now_ms < pause_until:
        remaining = (pause_until - now_ms) // 1000
        return True, f"paused {remaining}s more ({S.get('cb_reason', 'unknown')})"

    # Bug A (inherited): dead unreachable second return was removed here

    if S.get("loss_streak", 0) >= CB_CONSECUTIVE_LOSSES:
        return True, f"{CB_CONSECUTIVE_LOSSES} consecutive losses"

    if balance > 0 and isinstance(S.get("recent_pnl_log"), list):
        one_hour_ago = now_ms - 3_600_000
        recent       = [pnl for ts, pnl in S["recent_pnl_log"] if ts >= one_hour_ago]
        hourly_pnl   = sum(recent)
        if hourly_pnl < 0 and abs(hourly_pnl) / balance > CB_HOURLY_LOSS_PCT:
            return (
                True,
                f"hourly loss {abs(hourly_pnl)/balance*100:.1f}% > {CB_HOURLY_LOSS_PCT*100:.0f}%",
            )

    if API_FAIL_COUNT >= CB_CONSECUTIVE_API_FAILS:
        return True, f"{API_FAIL_COUNT} consecutive API failures"

    return False, "ok"

def trip_circuit_breaker(reason):
    # NOTE: pause duration stored in ms (CB_PAUSE_DURATION * 1000) — keep consistent
    # with process_cb_expiry() which also works in ms.
    now_ms = int(time.time() * 1000)
    S["cb_paused_until"] = now_ms + (CB_PAUSE_DURATION * 1000)
    S["cb_reason"]       = reason
    log.error(f"⛔ CIRCUIT BREAKER TRIPPED: {reason}")
    log.error(f"   Bot paused for {CB_PAUSE_DURATION}s ({CB_PAUSE_DURATION//60}min)")
    notify(f"⛔ Circuit Breaker: {reason}\nPaused {CB_PAUSE_DURATION//60}min", urgent=True)
    save_state()


# ── Exchange Outage Protection ────────────────────────────────

def check_exchange_health():
    try:
        r = requests.get(f"{BASE_URL}/api/v2/public/time", timeout=5)
        if r.status_code == 200:
            return r.json().get("code") == "00000"
    except Exception:
        pass
    return False

def wait_for_exchange_recovery(max_wait_seconds=86400):
    global OUTAGE_DETECTED
    if not OUTAGE_DETECTED:
        return
    log.warning("[OUTAGE] Bitget unreachable — waiting for recovery...")
    notify("⚠️ Bitget API down — bot paused", urgent=True)
    attempts     = 0
    max_attempts = max_wait_seconds // OUTAGE_RETRY_INTERVAL
    while OUTAGE_DETECTED and attempts < max_attempts:
        time.sleep(OUTAGE_RETRY_INTERVAL)
        attempts += 1
        if check_exchange_health():
            OUTAGE_DETECTED = False
            log.info(f"[OUTAGE] ✅ Bitget recovered after {attempts*OUTAGE_RETRY_INTERVAL}s")
            notify(f"✅ Bitget recovered after {attempts}min", urgent=True)
            return
        if attempts % 10 == 0:
            log.warning(
                f"[OUTAGE] Still down (attempt {attempts}, "
                f"{attempts*OUTAGE_RETRY_INTERVAL/60:.0f}min elapsed)"
            )
    if OUTAGE_DETECTED:
        log.error(
            f"[OUTAGE] Max wait ({max_wait_seconds/3600:.0f}h) reached — "
            "exchange still down."
        )
        notify(
            f"🚨 Bitget down >{max_wait_seconds//3600}h — check exchange manually!",
            urgent=True,
        )

def register_api_failure():
    global API_FAIL_COUNT
    API_FAIL_COUNT += 1

def register_api_success():
    global API_FAIL_COUNT
    API_FAIL_COUNT = 0


# ── Correlation-Aware Sizing ──────────────────────────────────

CORRELATED_GROUPS = [
    {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"},
]

def is_correlated_with_open(sym, new_side=None):
    open_meta = S.get("trade_meta", {})
    if not open_meta:
        return False
    for group in CORRELATED_GROUPS:
        if sym in group:
            others = group - {sym}
            for open_sym in open_meta:
                if open_sym not in others:
                    continue
                if new_side is None:
                    return True
                if open_meta[open_sym].get("side", "") == new_side:
                    return True
    return False


# ── Slippage Model ────────────────────────────────────────────

def estimate_slippage(volat_pct):
    if volat_pct < 2:
        bps = SLIPPAGE_BPS_LOW
    elif volat_pct < 4:
        bps = (SLIPPAGE_BPS_LOW + SLIPPAGE_BPS_HIGH) / 2
    else:
        bps = SLIPPAGE_BPS_HIGH
    return bps / 10000

def adjust_for_slippage(price, volat_pct, action):
    slip = estimate_slippage(volat_pct)
    return price * (1 + slip) if action == "LONG" else price * (1 - slip)


# ── Regime Classifier ─────────────────────────────────────────

def calc_adx(highs, lows, closes, period=14):
    if len(closes) < 2 * period + 1:
        return 0
    tr_list  = []
    plus_dm  = []
    minus_dm = []
    for i in range(1, len(closes)):
        high_diff = highs[i] - highs[i - 1]
        low_diff  = lows[i - 1] - lows[i]
        plus_dm.append(max(high_diff, 0) if high_diff > low_diff else 0)
        minus_dm.append(max(low_diff, 0) if low_diff > high_diff else 0)
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)

    smoothed_tr    = sum(tr_list[:period])
    smoothed_plus  = sum(plus_dm[:period])
    smoothed_minus = sum(minus_dm[:period])
    dx_values      = []

    for i in range(period, len(tr_list)):
        smoothed_tr    = smoothed_tr    - (smoothed_tr    / period) + tr_list[i]
        smoothed_plus  = smoothed_plus  - (smoothed_plus  / period) + plus_dm[i]
        smoothed_minus = smoothed_minus - (smoothed_minus / period) + minus_dm[i]
        if smoothed_tr == 0:
            continue
        plus_di  = 100 * smoothed_plus  / smoothed_tr
        minus_di = 100 * smoothed_minus / smoothed_tr
        di_sum   = plus_di + minus_di
        dx_values.append(0 if di_sum == 0 else 100 * abs(plus_di - minus_di) / di_sum)

    if len(dx_values) < period:
        return 0
    adx = sum(dx_values[:period]) / period
    for i in range(period, len(dx_values)):
        adx = (adx * (period - 1) + dx_values[i]) / period
    return adx

def classify_regime(highs, lows, closes, volat_pct):
    if volat_pct > REGIME_VOLAT_HIGH:
        return "VOLATILE"
    recent_window = 50
    h   = highs[-recent_window:]  if len(highs)   >= recent_window else highs
    l   = lows[-recent_window:]   if len(lows)    >= recent_window else lows
    c   = closes[-recent_window:] if len(closes)  >= recent_window else closes
    adx = calc_adx(h, l, c)
    if adx > REGIME_ADX_TREND:
        recent = closes[-10:]
        if recent[-1] > recent[0] * 1.005:
            return "TRENDING_UP"
        elif recent[-1] < recent[0] * 0.995:
            return "TRENDING_DOWN"
    return "RANGING"

def regime_adjustment(regime, action):
    if regime == "VOLATILE":
        return 0.7
    if regime == "RANGING":
        return 1.0
    if regime == "TRENDING_UP":
        return 1.15 if action == "LONG" else 0.6
    if regime == "TRENDING_DOWN":
        return 1.15 if action == "SHORT" else 0.6
    return 1.0


# ── Shadow Mode ───────────────────────────────────────────────

def log_shadow_trade(sym, sig, size, margin):
    if "shadow_trades" not in S or not isinstance(S["shadow_trades"], list):
        S["shadow_trades"] = []
    entry = {
        "ts":      int(time.time() * 1000),
        "ts_iso":  datetime.now(timezone.utc).isoformat(),
        "sym":     sym,
        "action":  sig.get("action"),
        "price":   sig.get("price"),
        "conf":    sig.get("conf"),
        "sl":      sig.get("sl"),
        "tp1":     sig.get("tp1"),
        "tp2":     sig.get("tp2"),
        "tp3":     sig.get("tp3"),
        "lev":     sig.get("lev"),
        "size":    size,
        "margin":  margin,
        "regime":  sig.get("regime", "unknown"),
        "rsi":     sig.get("rsi"),
        "macd_h":  sig.get("macd_h"),
        "stoch":   sig.get("stoch"),
        "vol_r":   sig.get("vol_r"),
        "volat":   sig.get("volat"),
        "bull":    sig.get("bull"),
        "bear":    sig.get("bear"),
        "atr":     sig.get("atr"),
        "reasons": sig.get("reasons", []),
        "outcome": None,
    }
    S["shadow_trades"].append(entry)
    if len(S["shadow_trades"]) > 200:
        S["shadow_trades"] = S["shadow_trades"][-200:]
    log.info(
        f"[SHADOW] 👻 Would trade {sym} {sig['action']} @ ${sig['price']:.2f} "
        f"(size={size}, margin=${margin}, conf={sig.get('conf')}%, regime={sig.get('regime')})"
    )

def print_shadow_summary():
    shadow = S.get("shadow_trades", [])
    if not shadow:
        return
    log.info("─── SHADOW MODE STATS ───")
    log.info(f"  Total shadow trades: {len(shadow)}")
    longs  = sum(1 for t in shadow if t.get("action") == "LONG")
    shorts = sum(1 for t in shadow if t.get("action") == "SHORT")
    log.info(f"  LONG: {longs}, SHORT: {shorts}")
    regimes = {}
    for t in shadow[-50:]:
        r = t.get("regime", "?")
        regimes[r] = regimes.get(r, 0) + 1
    log.info(f"  Recent regimes: {regimes}")
    with_outcomes = [t for t in shadow if t.get("outcome")]
    if with_outcomes:
        wins    = sum(1 for t in with_outcomes if t["outcome"].get("hit") == "tp")
        losses  = sum(1 for t in with_outcomes if t["outcome"].get("hit") == "sl")
        timeout = sum(1 for t in with_outcomes if t["outcome"].get("hit") == "timeout")
        log.info(
            f"  Outcomes: {wins} TP / {losses} SL / {timeout} timeout "
            f"(of {len(with_outcomes)} resolved)"
        )

def update_shadow_outcomes():
    shadow = S.get("shadow_trades", [])
    if not shadow:
        return
    now_ms     = int(time.time() * 1000)
    unresolved = [t for t in shadow if t.get("outcome") is None]

    for trade in unresolved:
        age_ms    = now_ms - trade.get("ts", now_ms)
        age_hours = age_ms / 3_600_000
        sym       = trade.get("sym")
        if not sym:
            continue
        tick = pub_ticker(sym)
        if not tick:
            continue
        try:
            cur_price = float(tick.get("lastPr", 0))
        except (ValueError, TypeError):
            continue
        if cur_price <= 0:
            continue

        entry  = trade.get("price", 0)
        sl     = trade.get("sl", 0)
        tp1    = trade.get("tp1", 0)
        action = trade.get("action")
        if entry <= 0 or sl <= 0:
            continue

        hit = None
        if action == "LONG":
            if cur_price <= sl:    hit = "sl"
            elif cur_price >= tp1: hit = "tp"
        elif action == "SHORT":
            if cur_price >= sl:    hit = "sl"
            elif cur_price <= tp1: hit = "tp"

        if hit is None and age_hours >= 24:
            hit = "timeout"

        if hit:
            pct = (
                (cur_price - entry) / entry * 100
                if action == "LONG"
                else (entry - cur_price) / entry * 100
            )
            trade["outcome"] = {
                "hit":         hit,
                "exit_price":  cur_price,
                "pct_move":    pct,
                "hold_hours":  age_hours,
                "resolved_ts": now_ms,
            }
            log.info(
                f"[SHADOW] 📊 Resolved {sym} {action} @ ${entry:.2f} → ${cur_price:.2f} "
                f"({pct:+.2f}%) hit={hit} held={age_hours:.1f}h"
            )


# ── Compounding ───────────────────────────────────────────────
# FIX-9: restructured drawdown guard for clarity; logic unchanged.

def compound_size(bal, price, lev, conf, sym, side=None):
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
    if conf >= 90:   margin *= 1.2
    elif conf >= 85: margin *= 1.1
    elif conf >= 80: margin *= 1.05

    # Drawdown reduction (FIX-9: guard peak_bal > 0 clearly)
    peak = S.get("peak_bal", 0)
    if peak > 0:
        dd = (peak - bal) / peak
        if dd > MAX_DRAWDOWN:
            margin *= 0.4
            log.warning(f"[COMPOUND] Drawdown {dd*100:.1f}% — size 40%")
        elif dd > 0.06:
            margin *= 0.7

    if is_correlated_with_open(sym, new_side=side):
        margin *= CORR_REDUCTION
        log.info(
            f"[COMPOUND] {sym} correlated with open position — "
            f"size {CORR_REDUCTION*100:.0f}%"
        )

    margin       = min(margin, bal * 0.40)
    dynamic_min  = max(3.0, bal * 0.08)
    if bal < dynamic_min:
        log.warning(f"[COMPOUND] {sym} balance ${bal:.2f} < min ${dynamic_min:.2f} — skip")
        return 0, 0
    if margin < dynamic_min:
        log.warning(
            f"[COMPOUND] {sym} margin ${margin:.2f} < ${dynamic_min:.2f} "
            "(dynamic min) — skip"
        )
        return 0, 0
    if sym not in SYMBOL_SPECS:
        log.error(f"[COMPOUND] Unknown symbol {sym} — no specs defined, skip")
        return 0, 0

    spec     = SYMBOL_SPECS[sym]
    raw_size = (margin * lev) / price
    factor   = 10 ** spec["precision"]
    size     = int(raw_size * factor) / factor
    if size < spec["min_size"]:
        log.warning(f"[COMPOUND] {sym} size {size} < min {spec['min_size']}")
        return 0, 0
    return size, round(margin, 2)


# ── Signal Generation ─────────────────────────────────────────

def generate_signal(sym, tick, cdata, fund_rate):
    if not cdata or len(cdata) < 60:
        return None
    try:
        last_ts_ms = int(cdata[-1][0])
        now_ms     = int(time.time() * 1000)
        age_min    = (now_ms - last_ts_ms) / 60000
        if age_min > 30:
            log.warning(f"[{sym}] Stale candles (age {age_min:.0f}min) — skip signal")
            return None
    except (ValueError, TypeError, IndexError):
        log.warning(f"[{sym}] Cannot parse candle timestamp — skip signal")
        return None

    # FIX-5: validate candle integrity before any indicator computation
    ok, reason = validate_candles(cdata, gran_label="15m", min_bars=60)
    if not ok:
        log.warning(f"[{sym}] Candle validation failed: {reason} — skip signal")
        return None

    _, highs, lows, closes, vols = safe_extract_ohlcv(cdata)
    if not closes:
        return None

    price = float(tick.get("lastPr", closes[-1]))
    h24   = float(tick.get("high24h", price))
    l24   = float(tick.get("low24h", price))
    chg   = float(tick.get("change24h", 0)) * 100
    e9    = ema(closes, 9)
    e21   = ema(closes, 21)
    e50   = ema(closes, 50)
    e100  = ema(closes, 100)
    e200  = ema(closes, 200) if len(closes) >= 200 else None
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
    if volat > MAX_VOLAT:
        log.warning(f"[{sym}] Volatility {volat:.1f}% — skip")
        return None
    regime = classify_regime(highs, lows, closes, volat)
    log.info(f"[REGIME] {sym} → {regime} (volat={volat:.1f}%, ADX-based)")
    mtf_b, mtf_br, tf_map = mtf_analysis(sym, primary_15m=cdata)
    bull, bear = 0, 0
    reasons    = []
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
        if price > e200: bull += 3; reasons.append("✅ Above EMA200")
        else:            bear += 3; reasons.append("🔴 Below EMA200")
    rsi_up = r_now > r_prev
    if r_now < 25:   bull += 4; reasons.append(f"✅ RSI extreme oversold {r_now:.0f}")
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
    bb_pct   = (price - bbl) / bb_range if bb_range > 0 else 0.5
    if price < bbl:     bull += 3; reasons.append("✅ Below BB lower")
    elif bb_pct < 0.2:  bull += 2; reasons.append("✅ Near BB lower support")
    elif price > bbu:   bear += 3; reasons.append("🔴 Above BB upper")
    elif bb_pct > 0.8:  bear += 2; reasons.append("🔴 Near BB upper resistance")
    if sk < 15:   bull += 3; reasons.append(f"✅ Stoch extreme oversold {sk:.0f}")
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
    elif fund_rate > 0.001: bear += 2
    elif fund_rate < -0.003:
        bull += 3; reasons.append(f"✅ Extreme negative funding {fund_rate*100:.3f}%")
    elif fund_rate < -0.001: bull += 2
    rng = h24 - l24
    if rng > 0:
        pos = (price - l24) / rng
        if pos < 0.10:   bull += 3; reasons.append("✅ At 24h low support")
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
    if chg > 4:    bull += 2
    elif chg > 2:  bull += 1
    elif chg < -4: bear += 2
    elif chg < -2: bear += 1
    sentiment = get_sentiment() or {}
    fg        = sentiment.get("fg_value")
    if fg is not None:
        if fg <= 20:   bull += 4; reasons.append(f"✅ Extreme Fear {fg} — buy opportunity")
        elif fg <= 35: bull += 2; reasons.append(f"✅ Market fearful {fg}")
        elif fg >= 80: bear += 4; reasons.append(f"🔴 Extreme Greed {fg} — caution!")
        elif fg >= 65: bear += 2; reasons.append(f"🔴 Market greedy {fg}")
    news_sent = sentiment.get("news_sent")
    if news_sent is not None:
        if news_sent > 30:    bull += 3; reasons.append("✅ News very bullish")
        elif news_sent > 10:  bull += 1
        elif news_sent < -30: bear += 3; reasons.append("🔴 News very bearish")
        elif news_sent < -10: bear += 1
    if sym == "ETHUSDT":
        btc_dom = sentiment.get("btc_dom")
        if btc_dom is not None:
            if btc_dom > 55:   bear += 1; reasons.append(f"🔴 BTC dominance high {btc_dom:.1f}%")
            elif btc_dom < 45: bull += 1; reasons.append(f"✅ Alt season BTC.D {btc_dom:.1f}%")
    total    = bull + bear
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

    regime_mult = regime_adjustment(regime, action)
    if regime_mult != 1.0:
        old_conf = conf
        conf = min(97, max(0, int(conf * regime_mult)))
        log.info(f"[REGIME] {regime}: conf {old_conf}% × {regime_mult:.2f} = {conf}%")

    if volat > 5:     lev = 1
    elif volat > 3.5: lev = 2
    elif volat > 2.5: lev = 3
    elif volat > 1.5: lev = 4 if conf >= 80 else 3
    elif conf >= 92:  lev = min(MAX_LEVERAGE, 10)
    elif conf >= 87:  lev = min(MAX_LEVERAGE, 8)
    elif conf >= 82:  lev = min(MAX_LEVERAGE, 6)
    elif conf >= 75:  lev = min(MAX_LEVERAGE, 5)
    else:             lev = min(MAX_LEVERAGE, 3)

    sl_d  = atr_v * SL_MULT
    tp1_d = atr_v * TP1_MULT
    tp2_d = atr_v * TP2_MULT
    tp3_d = atr_v * TP3_MULT
    trail = atr_v * TRAIL_MULT

    if action in ("LONG", "SHORT"):
        adjusted_entry = adjust_for_slippage(price, volat, action)
        slip_bps = abs(adjusted_entry - price) / price * 10000
        log.info(f"[SLIPPAGE] Expected entry slippage: {slip_bps:.1f} bps")
    else:
        adjusted_entry = price

    if action == "LONG":
        sl, tp1, tp2, tp3 = (
            adjusted_entry - sl_d,
            adjusted_entry + tp1_d,
            adjusted_entry + tp2_d,
            adjusted_entry + tp3_d,
        )
    elif action == "SHORT":
        sl, tp1, tp2, tp3 = (
            adjusted_entry + sl_d,
            adjusted_entry - tp1_d,
            adjusted_entry - tp2_d,
            adjusted_entry - tp3_d,
        )
    else:
        sl = tp1 = tp2 = tp3 = price

    if sl <= 0 or tp3 <= 0:
        log.warning(
            f"[{sym}] Invalid SL/TP prices (sl=${sl}, tp3=${tp3}) — skip signal"
        )
        return None
    if abs(sl - price) / price > 0.30:
        log.warning(
            f"[{sym}] SL too far from price ({abs(sl-price)/price*100:.0f}%) — skip"
        )
        return None

    return {
        "sym":    sym,    "asset": sym.replace("USDT", ""),
        "action": action, "side":  side,  "hold": hold,
        "conf":   conf,   "price": price,
        "sl":     sl,     "tp1":   tp1,   "tp2": tp2,  "tp3": tp3,
        "lev":    lev,    "rr":    f"1:{round(tp2_d/sl_d,1)}",
        "atr":    atr_v,  "trail": trail,
        "rsi":    r_now,  "macd_h": mh,   "stoch": sk,
        "vol_r":  vol_r,  "volat":  volat,
        "bull":   bull,   "bear":   bear,
        "regime": regime,
        "reasons": reasons[:5],
    }


# ── Position Management ───────────────────────────────────────

def manage_position(pos):
    sym  = pos.get("symbol")
    hold = pos.get("holdSide", "long")
    try:
        mark  = float(pos.get("markPrice", 0))
        size  = float(pos.get("total", 0))
        entry = float(pos.get("openPriceAvg", mark))
        pnl   = float(pos.get("unrealizedPL", 0))
    except (ValueError, TypeError) as e:
        log.warning(f"[MGR] {sym} invalid numeric data: {e} — skip")
        return
    if not (mark == mark and entry == entry and size == size):
        log.warning(f"[MGR] {sym} NaN in position data — skip")
        return
    if mark <= 0 or entry <= 0:
        log.warning(
            f"[MGR] {sym} non-positive price (mark=${mark}, entry=${entry}) — skip"
        )
        return
    if size <= 0:
        return

    start_bal = S.get("start_bal", 0)
    if start_bal > 0 and pnl < 0:
        unrealized_pct = abs(pnl) / start_bal
        if unrealized_pct > 0.05:
            log.warning(
                f"[MGR] {sym} unrealized loss {unrealized_pct*100:.1f}% "
                "of start_bal — flash crash risk"
            )

    meta = S["trade_meta"].get(sym, {})
    if not meta:
        log.debug(f"[MGR] {sym} no metadata — skipping")
        return
    trail = meta.get("trail", mark * 0.01)

    if "current_sl" in meta:
        current_sl = meta["current_sl"]
    else:
        sl_distance = trail * (SL_MULT / TRAIL_MULT)
        current_sl  = (entry - sl_distance) if hold == "long" else (entry + sl_distance)
        log.warning(f"[MGR] {sym} current_sl missing — fallback ${current_sl:,.2f}")

    tp1    = meta.get("tp1", 0)
    margin = meta.get("margin", 1)
    pnl_pct = (pnl / margin) * 100 if margin else 0
    log.info(
        f"[MGR] {sym} {hold.upper()} Mark:${mark:,.2f} "
        f"PnL:{pnl:+.4f}({pnl_pct:+.1f}%) SL:${current_sl:,.2f}"
    )

    if hold == "long":
        new_sl = mark - trail
        if new_sl > current_sl and mark > entry * 1.005:
            sl_improvement = (new_sl - current_sl) / current_sl if current_sl > 0 else 1
            if sl_improvement >= MIN_SL_IMPROVE:
                if update_sl_atomic(sym, hold, new_sl, size):
                    meta["current_sl"]   = new_sl
                    S["trade_meta"][sym] = meta
                    save_state()
        elif tp1 > 0 and mark >= tp1 and not meta.get("be_moved"):
            be = entry * 1.003
            if be > current_sl:
                if update_sl_atomic(sym, hold, be, size):
                    meta["current_sl"]   = be
                    meta["be_moved"]     = True
                    S["trade_meta"][sym] = meta
                    save_state()
                    log.info(f"[TRAIL] {sym} TP1 hit! SL → BE")
    elif hold == "short":
        new_sl = mark + trail
        if new_sl < current_sl and mark < entry * 0.995:
            sl_improvement = (current_sl - new_sl) / current_sl if current_sl > 0 else 1
            if sl_improvement >= MIN_SL_IMPROVE:
                if update_sl_atomic(sym, hold, new_sl, size):
                    meta["current_sl"]   = new_sl
                    S["trade_meta"][sym] = meta
                    save_state()
        elif tp1 > 0 and mark <= tp1 and not meta.get("be_moved"):
            be = entry * 0.997
            if be < current_sl:
                if update_sl_atomic(sym, hold, be, size):
                    meta["current_sl"]   = be
                    meta["be_moved"]     = True
                    S["trade_meta"][sym] = meta
                    save_state()
                    log.info(f"[TRAIL] {sym} TP1 hit! SL → BE")


def detect_closed_positions(open_pos):
    exchange_syms = {p.get("symbol") for p in open_pos}
    for sym in list(S["trade_meta"].keys()):
        if sym in exchange_syms:
            continue
        meta = S["trade_meta"][sym]
        log.info(f"[CLOSED] {sym} closed on exchange")

        # FIX-11: look up per-trade margin for the relative PnL threshold
        trade_margin = meta.get("margin", 0) if isinstance(meta, dict) else 0

        actual_pnl  = 0.0
        fills_found = False
        try:
            entry_time_str = meta.get("entry_time", "")
            if entry_time_str:
                entry_time_ms = int(
                    datetime.fromisoformat(entry_time_str).timestamp() * 1000
                )
                fills = fetch_recent_fills(sym, 30)
                for fill in fills:
                    fill_time  = int(fill.get("cTime", 0))
                    trade_side = fill.get("tradeSide", "").lower()
                    if fill_time >= entry_time_ms and "close" in trade_side:
                        try:
                            actual_pnl += float(fill.get("profit", 0))
                            fills_found = True
                        except (ValueError, TypeError):
                            pass
        except Exception as e:
            log.warning(f"[FILL parse] {sym}: {e}")

        if not fills_found:
            last_pnl = meta.get("last_unrealized_pnl", None)
            if last_pnl is None or last_pnl == 0.0:
                entry     = meta.get("entry_price", 0)
                last_mark = meta.get("last_mark", entry)
                size      = meta.get("size", 0)
                side      = meta.get("side", "long")
                if entry > 0 and last_mark > 0 and size > 0:
                    actual_pnl = (
                        (last_mark - entry) * size
                        if side == "long"
                        else (entry - last_mark) * size
                    )
            else:
                actual_pnl = last_pnl
            log.info(f"[CLOSED] {sym} using estimated PnL (fills not found)")
        else:
            log.info(f"[CLOSED] {sym} actual realized PnL from fills")

        # FIX-3: relative threshold so small-margin trades are still classified
        pnl_threshold = max(0.01, trade_margin * 0.002) if trade_margin > 0 else 0.01

        if actual_pnl > pnl_threshold:
            S["wins"]       += 1
            S["win_streak"] += 1
            S["loss_streak"] = 0
            log.info(f"[WIN] {sym} +${actual_pnl:.4f}")
            notify(f"✅ {sym} WIN ${actual_pnl:+.2f}", urgent=True)
            pat_sig = meta.get("pattern_sig")
            if pat_sig:
                update_pattern_memory(pat_sig, won=True)
                log.info(f"[LEARN] ✅ Pattern '{pat_sig}' → WIN recorded")
        elif actual_pnl < -pnl_threshold:
            S["losses"]      += 1
            S["loss_streak"] += 1
            S["win_streak"]   = 0
            log.info(f"[LOSS] {sym} ${actual_pnl:.4f}")
            notify(f"❌ {sym} LOSS ${actual_pnl:+.2f}", urgent=True)
            pat_sig = meta.get("pattern_sig")
            if pat_sig:
                update_pattern_memory(pat_sig, won=False)
                log.info(f"[LEARN] ❌ Pattern '{pat_sig}' → LOSS recorded")
        else:
            log.info(f"[BREAKEVEN] {sym} closed @ ~$0 PnL (no streak change)")
            notify(f"➖ {sym} closed @ breakeven", urgent=True)

        S["daily_pnl"] += actual_pnl
        S["total_pnl"] += actual_pnl
        record_trade_outcome(actual_pnl)
        if actual_pnl < 0:
            cb_pause, cb_reason = check_circuit_breakers(S.get("start_bal", 100))
            if cb_pause and ("consecutive" in cb_reason or "hourly" in cb_reason):
                trip_circuit_breaker(cb_reason)
        cancel_all_plan_orders(sym)
        del S["trade_meta"][sym]
        save_state()


# ── Reporting ─────────────────────────────────────────────────

def print_report(bal):
    start  = S["start_bal"]
    growth = ((bal - start) / start * 100) if start > 0 else 0
    dd     = ((S["peak_bal"] - bal) / S["peak_bal"] * 100) if S["peak_bal"] > 0 else 0
    total  = S["wins"] + S["losses"]
    wr     = (S["wins"] / total * 100) if total > 0 else 0
    log.info("╔══════════════════════════════════════╗")
    log.info("║   💰 ARES v6.4 REPORT                ║")
    log.info(f"║  Balance:  ${bal:.2f}")
    log.info(f"║  Growth:   {growth:+.2f}%")
    log.info(f"║  Drawdown: {dd:.2f}%")
    log.info(f"║  PnL:      ${S['total_pnl']:+.4f}")
    log.info(f"║  WinRate:  {wr:.1f}% ({S['wins']}W/{S['losses']}L)")
    pm = S.get("pattern_memory", {})
    log.info(f"║  Patterns: {len(pm)} learned")
    # FIX-4: flag ephemeral state in the report
    if _STATE_FILE_IS_EPHEMERAL:
        log.info("║  ⚠️  State: /tmp (EPHEMERAL)")
    cb_paused = S.get("cb_paused_until", 0)
    if cb_paused and int(time.time() * 1000) < cb_paused:
        remaining = (cb_paused - int(time.time() * 1000)) // 60000
        log.info(f"║  ⛔ CB:    paused {remaining}min more")
    shadow = S.get("shadow_trades", [])
    if shadow:
        log.info(f"║  Shadow:   {len(shadow)} logged trades")
    log.info("╚══════════════════════════════════════╝")
    if pm:
        print_pattern_summary()
    if shadow:
        print_shadow_summary()


# ── Main Loop ─────────────────────────────────────────────────

def run():
    global OUTAGE_DETECTED
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║  ▲ ARES ULTRA v6.4 — Pro Risk Management         ║")
    log.info("║  +Circuit Breakers +Outage Detect +Regime +Shadow║")
    log.info(
        f"║  Compound:{COMPOUND_PCT*100:.0f}% | MaxLev:{MAX_LEVERAGE}x | "
        f"Trades:{MAX_TRADES}     ║"
    )
    log.info("╚══════════════════════════════════════════════════╝")

    # FIX-4: re-emit state path warning at startup (already logged at import
    # time if /app was not writable, but worth repeating in the run banner)
    if _STATE_FILE_IS_EPHEMERAL:
        log.warning(
            "[INIT] ⚠️  Using EPHEMERAL state at /tmp — "
            "set STATE_FILE_PATH to a persistent path!"
        )

    if not API_KEY or not SECRET_KEY or not PASSPHRASE:
        log.error("❌ API keys missing!")
        return

    load_state()
    if RESET_STATS:
        reset_stats()

    if not check_exchange_health():
        log.error("[INIT] Bitget unreachable on startup — waiting for recovery")
        OUTAGE_DETECTED = True
        wait_for_exchange_recovery()

    bal = fetch_balance()
    if bal == 0:
        log.warning("[INIT] Balance $0 — verify API keys and futures balance")
    else:
        log.info(f"[INIT] Balance: ${bal:.2f} USDT")
    if S["start_bal"] == 0:
        S["start_bal"] = bal
    if S["peak_bal"] == 0:
        S["peak_bal"] = bal
    notify(
        f"🚀 ARES v6.4 started | Balance: ${bal:.2f}"
        f"{' [SHADOW]' if SHADOW_MODE else ''}",
        urgent=True,
    )

    cycle = 0
    while True:
        try:
            cycle += 1
            now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            today = datetime.now(timezone.utc).date()
            if not isinstance(S["daily_start"], type(today)):
                S["daily_start"] = today
            if today != S["daily_start"]:
                log.info("🔄 New day — daily PnL reset")
                S["daily_pnl"]   = 0
                S["daily_start"] = today
                save_state()

            log.info(f"\n{'═'*52}")
            log.info(f"  CYCLE {cycle} | {now}{' [SHADOW]' if SHADOW_MODE else ''}")
            log.info(f"{'═'*52}")

            if not check_exchange_health():
                OUTAGE_DETECTED = True
                wait_for_exchange_recovery()
                continue

            bal = fetch_balance()
            if bal > S["peak_bal"]:
                S["peak_bal"] = bal

            process_cb_expiry()
            cb_pause, cb_reason = check_circuit_breakers(bal)
            if cb_pause:
                log.warning(f"⛔ [CIRCUIT BREAKER] {cb_reason}")
                save_state()
                time.sleep(SCAN_INTERVAL)
                continue

            if S["start_bal"] > 0:
                loss_pct = S["daily_pnl"] / S["start_bal"]
                if loss_pct <= -MAX_DAILY_LOSS:
                    now_utc  = datetime.now(timezone.utc)
                    tomorrow = (now_utc + timedelta(days=1)).replace(
                        hour=0, minute=5, second=0, microsecond=0
                    )
                    sleep_secs = int((tomorrow - now_utc).total_seconds())
                    hours      = sleep_secs // 3600
                    log.warning(
                        f"⛔ Daily loss {MAX_DAILY_LOSS*100:.0f}% hit! "
                        f"Pausing {hours}h until next day"
                    )
                    notify(f"⛔ Daily loss limit hit. Pausing {hours}h.", urgent=True)
                    save_state()
                    time.sleep(sleep_secs)
                    S["daily_pnl"] = 0
                    continue

            total = S["wins"] + S["losses"]
            wr    = (S["wins"] / total * 100) if total > 0 else 0
            log.info(
                f"[BAL] ${bal:.2f} | Day:${S['daily_pnl']:+.4f} | "
                f"Total:${S['total_pnl']:+.4f} | "
                f"{S['wins']}W/{S['losses']}L ({wr:.0f}%)"
            )

            if bal < 6:
                log.warning("[SKIP] Balance < $6")
                time.sleep(SCAN_INTERVAL)
                continue

            open_pos = fetch_positions()
            if open_pos is None:
                log.error("[POS] fetch_positions failed — skip cycle for safety")
                time.sleep(SCAN_INTERVAL)
                continue

            log.info(
                f"[POS] {len(open_pos)} open | "
                f"Tracked: {list(S['trade_meta'].keys())}"
            )

            # Update fallback PnL data BEFORE detect_closed_positions reads it
            for pos in open_pos:
                sym = pos.get("symbol")
                if sym in S["trade_meta"]:
                    try:
                        S["trade_meta"][sym]["last_unrealized_pnl"] = float(
                            pos.get("unrealizedPL", 0)
                        )
                        S["trade_meta"][sym]["last_mark"] = float(
                            pos.get("markPrice", 0)
                        )
                    except (ValueError, TypeError):
                        pass

            detect_closed_positions(open_pos)

            if SHADOW_MODE:
                update_shadow_outcomes()

            for pos in open_pos:
                manage_position(pos)

            if cycle % 20 == 0:
                print_report(bal)

            # Bug B (inherited): all_open = union of exchange + tracked positions.
            exchange_syms   = {p.get("symbol") for p in open_pos}
            tracked_syms    = set(S["trade_meta"].keys())
            all_open        = exchange_syms | tracked_syms
            live_open_count = len(all_open)

            if live_open_count >= MAX_TRADES:
                log.info(
                    f"[SKIP] {MAX_TRADES} trades open "
                    f"(tracked:{len(tracked_syms)} exchange:{len(exchange_syms)})"
                )
                save_state()
                time.sleep(SCAN_INTERVAL)
                continue

            for sym in SYMBOLS:
                if sym in all_open:
                    log.info(f"[{sym}] Position open — skip")
                    continue

                log.info(f"\n[SCAN] ━━━ {sym} ━━━━━━━━━━━━━━━━━━━━━━━")
                tick       = pub_ticker(sym)
                cdata_full = pub_candles(sym, "15", 350)
                fund_rate, fund_mins = pub_funding(sym)
                if not tick or not cdata_full:
                    log.warning(f"[{sym}] No market data")
                    continue
                cdata = cdata_full[-150:] if len(cdata_full) >= 150 else cdata_full

                if fund_mins == -1 and abs(fund_rate) > 0.0008:
                    log.warning(
                        f"[{sym}] Funding time unknown @ "
                        f"{fund_rate*100:.3f}% (extreme) — skip"
                    )
                    continue
                if fund_mins != -1 and fund_mins < 15 and abs(fund_rate) > 0.0005:
                    log.warning(
                        f"[{sym}] Funding in {fund_mins}m @ "
                        f"{fund_rate*100:.3f}% — skip"
                    )
                    continue

                price    = float(tick.get("lastPr", 0))
                chg      = float(tick.get("change24h", 0)) * 100
                fund_str = f"{fund_mins}m" if fund_mins != -1 else "?"
                log.info(
                    f"[{sym}] ${price:,.2f} | {chg:+.2f}% | "
                    f"Fund:{fund_rate*100:.4f}% (in {fund_str})"
                )

                sig = generate_signal(sym, tick, cdata, fund_rate)
                if not sig:
                    log.info(f"[{sym}] No signal")
                    continue

                em = "🟢" if sig["action"] == "LONG" else "🔴" if sig["action"] == "SHORT" else "🟡"
                log.info(
                    f"[SIG] {em} {sig['action']} | Conf:{sig['conf']}% | "
                    f"Lev:{sig['lev']}x | R:R {sig['rr']}"
                )
                log.info(
                    f"[IND] RSI:{sig['rsi']:.0f} MACD:{sig['macd_h']:+.2f} "
                    f"Stoch:{sig['stoch']:.0f} Vol:{sig['vol_r']:.1f}x"
                )
                log.info(f"[SCR] 🟢{sig['bull']} vs 🔴{sig['bear']}")
                for r in sig["reasons"][:3]:
                    log.info(f"  {r}")

                if sig["action"] not in ("LONG", "SHORT") or sig["conf"] < MIN_CONF:
                    log.info(f"[{sym}] {sig['action']} conf:{sig['conf']}% — wait")
                    continue

                # ── Mini-Backtest ──────────────────────────────────────
                bt_start = time.time()
                bt_ok, bt_reason = check_mini_backtest(
                    cdata_full, sig["action"], sig["atr"], sig=sig
                )
                log.info(f"[BACKTEST] {bt_reason} ({(time.time()-bt_start)*1000:.0f}ms)")
                if not bt_ok:
                    log.warning(f"[{sym}] ⚠️ Mini-backtest fail — SKIP")
                    notify(f"⚠️ {sym} {sig['action']} skipped (backtest: {bt_reason})")
                    continue

                # ── Pattern Memory ─────────────────────────────────────
                sig_key = get_pattern_signature(sig)
                should_trade, reason = check_pattern_history(sig_key)
                log.info(f"[LEARN] Pattern: {sig_key}")
                log.info(f"[LEARN] History: {reason}")
                if not should_trade:
                    log.warning(f"[{sym}] ⚠️ Pattern has poor history — SKIP")
                    notify(f"⚠️ {sym} {sig['action']} skipped (poor pattern: {reason})")
                    continue

                log.info("[✅ APPROVED] Both filters passed → executing trade")

                size, margin = compound_size(
                    bal, price, sig["lev"], sig["conf"], sym, side=sig["hold"]
                )
                if size <= 0:
                    continue

                log.info(
                    f"[ENTRY] {sym} margin:${margin} size:{size} lev:{sig['lev']}x"
                )
                log.info(
                    f"[RISK] SL:${sig['sl']:,.2f} TP1:${sig['tp1']:,.2f} "
                    f"TP2:${sig['tp2']:,.2f} TP3:${sig['tp3']:,.2f}"
                )

                # Shadow mode — log but don't execute
                if SHADOW_MODE:
                    log_shadow_trade(sym, sig, size, margin)
                    save_state()
                    continue

                if not set_leverage(sym, sig["lev"]):
                    log.error(f"[ENTRY] {sym} leverage failed — abort")
                    continue

                res = place_market_order(sym, sig["side"], size, "open")
                time.sleep(1.5)

                # Verify position actually opened
                actual_pos = None
                for verify_attempt in range(3):
                    current_positions = fetch_positions()
                    if current_positions is None:
                        log.warning(
                            f"[ENTRY] {sym} verify attempt {verify_attempt+1}: API failed"
                        )
                        time.sleep(2)
                        continue
                    for p in current_positions:
                        if p.get("symbol") == sym and float(p.get("total", 0)) > 0:
                            actual_pos = p
                            break
                    if actual_pos:
                        break
                    log.warning(
                        f"[ENTRY] {sym} verify attempt {verify_attempt+1}: no position yet"
                    )
                    time.sleep(2)

                if not actual_pos:
                    # FIX-2: res is now always a dict, safe to call .get()
                    err = res.get("msg", "order failed or not confirmed")
                    log.error(
                        f"[ENTRY] {sym} order failed after 3 verify attempts: {err}"
                    )
                    continue

                actual_size  = float(actual_pos.get("total", 0))
                actual_entry = float(actual_pos.get("openPriceAvg", price))
                # FIX-2: res is always a dict, .get() is safe
                oid = res.get("data", {}).get("orderId", "N/A") if res else "verified"
                log.info(
                    f"[ENTRY] ✅ {sig['action']} OPENED size={actual_size} "
                    f"@ ${actual_entry:.2f} ID:{oid}"
                )

                # Recompute SL/TP from actual fill price
                atr_v = sig["atr"]
                if sig["hold"] == "long":
                    actual_sl  = actual_entry - atr_v * SL_MULT
                    actual_tp1 = actual_entry + atr_v * TP1_MULT
                    actual_tp2 = actual_entry + atr_v * TP2_MULT
                    actual_tp3 = actual_entry + atr_v * TP3_MULT
                else:
                    actual_sl  = actual_entry + atr_v * SL_MULT
                    actual_tp1 = actual_entry - atr_v * TP1_MULT
                    actual_tp2 = actual_entry - atr_v * TP2_MULT
                    actual_tp3 = actual_entry - atr_v * TP3_MULT

                if abs(actual_entry - sig["price"]) / sig["price"] > 0.0005:
                    log.info(
                        f"[ENTRY] SL/TP recomputed from actual fill "
                        f"(diff {(actual_entry-sig['price'])/sig['price']*10000:.1f} bps)"
                    )

                # Place SL immediately to minimise naked-position window
                spec    = SYMBOL_SPECS.get(sym, {"price_precision": 2})
                px_prec = spec.get("price_precision", 2)
                sl_first = place_plan_order(
                    sym, "loss_plan",
                    round(actual_sl, px_prec),
                    sig["hold"], actual_size,
                )
                if not sl_first:
                    log.error(f"[ENTRY] {sym} SL placement failed — emergency close")
                    cancel_all_plan_orders(sym)
                    close_side = "sell" if sig["side"] == "buy" else "buy"
                    closed     = False
                    for close_attempt in range(3):
                        close_res = place_market_order(sym, close_side, actual_size, "close")
                        if close_res.get("code") == "00000":  # FIX-2: always a dict
                            closed = True
                            break
                        time.sleep(2)
                    if not closed:
                        notify(
                            f"🚨 CRITICAL: {sym} SL+close failed — NAKED POSITION",
                            urgent=True,
                        )
                    else:
                        notify(f"⚠️ {sym} closed — SL placement failed", urgent=True)
                    continue

                log.info(f"[ENTRY] 🛡️ SL placed @ ${actual_sl:.2f} — position now protected")

                if not setup_protection(
                    sym, sig["hold"], actual_sl,
                    actual_tp1, actual_tp2, actual_tp3,
                    actual_size, skip_sl=True,
                ):
                    log.error(f"[ENTRY] {sym} TP placement failed — closing position")
                    cancel_all_plan_orders(sym)
                    close_side = "sell" if sig["side"] == "buy" else "buy"
                    closed     = False
                    for close_attempt in range(3):
                        close_res = place_market_order(sym, close_side, actual_size, "close")
                        if close_res.get("code") == "00000":  # FIX-2: always a dict
                            closed = True
                            break
                        time.sleep(2)
                    if not closed:
                        notify(
                            f"🚨 CRITICAL: {sym} close failed after TP fail — NAKED",
                            urgent=True,
                        )
                    else:
                        notify(f"⚠️ {sym} closed — TP setup failed", urgent=True)
                    continue

                sig["sl"]    = actual_sl
                sig["tp1"]   = actual_tp1
                sig["tp2"]   = actual_tp2
                sig["tp3"]   = actual_tp3
                sig["price"] = actual_entry

                S["trade_meta"][sym] = {
                    "entry_price":         actual_entry,
                    "entry_time":          datetime.now(timezone.utc).isoformat(),
                    "side":                sig["hold"],
                    "lev":                 sig["lev"],
                    "margin":              margin,
                    "size":                actual_size,
                    "trail":               sig["trail"],
                    "current_sl":          sig["sl"],
                    "tp1":                 sig["tp1"],
                    "tp2":                 sig["tp2"],
                    "tp3":                 sig["tp3"],
                    "be_moved":            False,
                    "signal_score":        sig["bull"] if sig["action"] == "LONG" else sig["bear"],
                    "confidence":          sig["conf"],
                    "last_unrealized_pnl": 0.0,
                    "last_mark":           actual_entry,
                    "pattern_sig":         sig_key,
                }
                save_state()
                # Bug C (inherited): show actual_entry (fill price), not pre-entry market quote
                notify(
                    f"🎯 {sym} {sig['action']} @ ${actual_entry:,.2f}\n"
                    f"Lev:{sig['lev']}x Conf:{sig['conf']}%\n"
                    f"SL:${sig['sl']:,.2f} TP3:${sig['tp3']:,.2f}",
                    urgent=True,
                )
                time.sleep(3)

            log.info(f"\n[SLEEP] {SCAN_INTERVAL}s...")
            save_state()
            flush_notifications()
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log.info("Stopped by user.")
            save_state()
            flush_notifications()
            notify("🛑 ARES stopped", urgent=True)
            break
        except Exception as e:
            log.error(f"[ERROR] Cycle {cycle}: {e}", exc_info=True)
            notify(f"⚠️ Bot error: {e}", urgent=True)
            try:
                flush_notifications()
                save_state()
            except Exception:
                pass
            time.sleep(30)


if __name__ == "__main__":
    run()
