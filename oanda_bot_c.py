"""
OANDA PRACTICE ACCOUNT BOT - Strategy C (GitHub Actions, single run per cron tick)

Strategy C (frozen, do NOT tune):
  - XAU/USD on 1-hour candles
  - BUY  when the last closed H1 candle closes ABOVE the highest high of the previous 55 candles
         AND EMA50 > EMA200 (trend filter)
  - SELL when it closes BELOW the lowest low of the previous 55 candles
         AND EMA50 < EMA200
  - Stop = 2.0 x ATR(14), target = 3.0 x ATR(14)
  - Close any trade still open after 24 hours
  - One trade at a time
  - Position size = RISK_CAD / (stop distance in CAD), so every loss is about RISK_CAD

Required GitHub Secrets (use a SEPARATE practice sub-account so C is tracked on its own):
  OANDA_TOKEN       = your Personal Access Token
  OANDA_ACCOUNT_ID  = the practice account ID for this bot

Optional env vars:
  RISK_CAD   = money risked per trade in account currency (default 250)
  MAX_UNITS  = hard cap on position size (default 50)
"""

import os
import requests
import datetime
import traceback

OANDA_TOKEN = os.environ.get("OANDA_TOKEN")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID")
OANDA_API_URL = "https://api-fxpractice.oanda.com"  # practice environment, NOT live

SYMBOL = "XAU_USD"
GRANULARITY = "H1"
DONCHIAN_PERIOD = 55
TREND_FAST = 50
TREND_SLOW = 200
ATR_PERIOD = 14
ATR_SL_MULT = 2.0
ATR_TP_MULT = 3.0
EXPIRY_HOURS = 24
CANDLES_TO_FETCH = 500          # enough history for EMA200 to settle
MAX_SIGNAL_AGE_MIN = 55         # act on a breakout candle that closed in the last 55 min (GitHub cron can run late)
REQUEST_TIMEOUT = 15

RISK_CAD = float(os.environ.get("RISK_CAD", "250"))
MAX_UNITS = int(os.environ.get("MAX_UNITS", "50"))

HEADERS = {
    "Authorization": f"Bearer {OANDA_TOKEN}",
    "Content-Type": "application/json",
}


# ===== INDICATORS =====

def ema(prices, period):
    if len(prices) < period:
        return None
    ema_val = sum(prices[:period]) / period
    k = 2 / (period + 1)
    for p in prices[period:]:
        ema_val = (p - ema_val) * k + ema_val
    return ema_val


def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, period + 1):
        tr = max(highs[-i] - lows[-i],
                 abs(highs[-i] - closes[-i - 1]),
                 abs(lows[-i] - closes[-i - 1]))
        trs.append(tr)
    return sum(trs) / period


# ===== NETWORK CALLS (wrapped so a transient failure can't kill the run) =====

def get_prices(instruments):
    """Returns {instrument: dict(bid, ask, tradeable)} or None on failure."""
    try:
        url = f"{OANDA_API_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
        r = requests.get(url, headers=HEADERS, params={"instruments": ",".join(instruments)},
                         timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        out = {}
        for p in r.json().get("prices", []):
            out[p["instrument"]] = {
                "bid": float(p["bids"][0]["price"]),
                "ask": float(p["asks"][0]["price"]),
                "tradeable": p.get("tradeable", False),
            }
        return out
    except Exception as e:
        print(f"  [get_prices] error: {e}")
        return None


def get_account_currency():
    try:
        url = f"{OANDA_API_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()["account"]["currency"]
    except Exception as e:
        print(f"  [get_account_currency] error: {e}")
        return None


def fetch_candles(count=CANDLES_TO_FETCH):
    """Returns (times, closes, highs, lows) of COMPLETED candles only."""
    try:
        url = f"{OANDA_API_URL}/v3/instruments/{SYMBOL}/candles"
        params = {"count": count, "granularity": GRANULARITY, "price": "M"}
        r = requests.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        candles = [c for c in r.json()["candles"] if c["complete"]]
        times = [c["time"] for c in candles]
        closes = [float(c["mid"]["c"]) for c in candles]
        highs = [float(c["mid"]["h"]) for c in candles]
        lows = [float(c["mid"]["l"]) for c in candles]
        return times, closes, highs, lows
    except Exception as e:
        print(f"  [fetch_candles] error: {e}")
        return None, None, None, None


def get_open_trades():
    try:
        url = f"{OANDA_API_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/openTrades"
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json().get("trades", [])
    except Exception as e:
        print(f"  [get_open_trades] error: {e}")
        return None  # None = unknown, distinct from [] = confirmed none open


def place_order(side, units, sl, tp):
    try:
        order_units = units if side == "BUY" else -units
        order = {
            "order": {
                "type": "MARKET",
                "instrument": SYMBOL,
                "units": str(order_units),
                "timeInForce": "FOK",
                "positionFill": "DEFAULT",
                "stopLossOnFill": {"price": f"{sl:.2f}"},
                "takeProfitOnFill": {"price": f"{tp:.2f}"},
            }
        }
        url = f"{OANDA_API_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
        r = requests.post(url, headers=HEADERS, json=order, timeout=REQUEST_TIMEOUT)
        if r.status_code not in (200, 201):
            print(f"  [place_order] FAILED: {r.status_code} {r.text}")
            return None
        return r.json()
    except Exception as e:
        print(f"  [place_order] error: {e}")
        return None


def traded_since(cutoff):
    """True if any trade (open or closed) was opened at/after cutoff. None = unknown."""
    try:
        url = f"{OANDA_API_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/trades"
        r = requests.get(url, headers=HEADERS, params={"state": "ALL", "instrument": SYMBOL, "count": 5},
                         timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return any(parse_time(t["openTime"]) >= cutoff for t in r.json().get("trades", []))
    except Exception as e:
        print(f"  [traded_since] error: {e}")
        return None


def close_trade(trade_id):
    try:
        url = f"{OANDA_API_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/close"
        r = requests.put(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code not in (200, 201):
            print(f"  [close_trade] FAILED: {r.status_code} {r.text}")
            return None
        return r.json()
    except Exception as e:
        print(f"  [close_trade] error: {e}")
        return None


# ===== SIGNAL LOGIC (Strategy C) =====

def parse_time(s):
    # OANDA times look like 2026-11-10T14:00:00.000000000Z (nanoseconds) - trim to microseconds
    s = s.replace("Z", "")
    if "." in s:
        head, frac = s.split(".")
        s = f"{head}.{frac[:6]}"
    return datetime.datetime.fromisoformat(s).replace(tzinfo=datetime.timezone.utc)


def generate_signal(closes, highs, lows):
    """Looks at the LAST completed candle. Returns 'BUY', 'SELL' or None, plus ATR."""
    need = max(TREND_SLOW, DONCHIAN_PERIOD + 1, ATR_PERIOD + 1)
    if len(closes) < need:
        return None, None

    a = atr(highs, lows, closes, ATR_PERIOD)
    fast = ema(closes, TREND_FAST)
    slow = ema(closes, TREND_SLOW)
    if None in (a, fast, slow):
        return None, None

    prev_high = max(highs[-DONCHIAN_PERIOD - 1:-1])   # previous 55 candles, excluding the signal candle
    prev_low = min(lows[-DONCHIAN_PERIOD - 1:-1])
    last_close = closes[-1]

    if last_close > prev_high and fast > slow:
        return "BUY", a
    if last_close < prev_low and fast < slow:
        return "SELL", a
    return None, a


# ===== ONE CHECK CYCLE (never raises) =====

def run_once():
    try:
        now = datetime.datetime.now(datetime.timezone.utc)

        prices = get_prices([SYMBOL])
        if prices is None or SYMBOL not in prices or not prices[SYMBOL]["tradeable"]:
            print(f"{now} - market closed (or status unknown), skipping.")
            return

        open_trades = get_open_trades()
        if open_trades is None:
            print(f"{now} - couldn't confirm open trades, skipping this cycle to be safe.")
            return

        # 24h expiry
        for t in open_trades:
            opened_at = parse_time(t["openTime"])
            age_hours = (now - opened_at).total_seconds() / 3600
            if age_hours >= EXPIRY_HOURS:
                print(f"  Closing trade {t['id']} - open {age_hours:.1f}h, exceeded {EXPIRY_HOURS}h expiry")
                close_trade(t["id"])

        open_trades = get_open_trades()
        if open_trades is None:
            print(f"{now} - couldn't re-confirm open trades after expiry check, skipping.")
            return
        if open_trades:
            print(f"{now} - trade already open, skipping new signal check.")
            return

        times, closes, highs, lows = fetch_candles()
        if closes is None:
            print(f"{now} - couldn't fetch candles this cycle, skipping.")
            return

        # only act right after a candle closes, so one breakout = at most one trade
        last_close_time = parse_time(times[-1]) + datetime.timedelta(hours=1)
        age_min = (now - last_close_time).total_seconds() / 60
        if age_min > MAX_SIGNAL_AGE_MIN:
            print(f"{now} - latest candle closed {age_min:.0f} min ago, outside signal window, skipping.")
            return

        side, a = generate_signal(closes, highs, lows)
        if side is None:
            print(f"{now} - no signal (last candle closed {age_min:.0f} min ago).")
            return

        # one breakout = one trade: skip if we already traded after this candle closed
        already = traded_since(last_close_time)
        if already is None or already:
            print(f"{now} - signal {side}, but already traded on this candle (or unknown), skipping.")
            return

        # entry price = live price on the correct side of the spread
        px = prices[SYMBOL]
        entry = px["ask"] if side == "BUY" else px["bid"]
        sl_dist = ATR_SL_MULT * a
        tp_dist = ATR_TP_MULT * a
        if side == "BUY":
            sl, tp = entry - sl_dist, entry + tp_dist
        else:
            sl, tp = entry + sl_dist, entry - tp_dist

        # size so a stop-out loses about RISK_CAD (converted from USD via the account currency)
        acct_ccy = get_account_currency()
        if acct_ccy is None:
            print(f"{now} - couldn't read account currency, skipping.")
            return
        if acct_ccy == "USD":
            usd_to_acct = 1.0
        else:
            fx = get_prices([f"USD_{acct_ccy}"])
            if not fx or f"USD_{acct_ccy}" not in fx:
                print(f"{now} - couldn't get USD_{acct_ccy} rate, skipping.")
                return
            q = fx[f"USD_{acct_ccy}"]
            usd_to_acct = (q["bid"] + q["ask"]) / 2

        units = int(RISK_CAD / (sl_dist * usd_to_acct))
        units = max(1, min(units, MAX_UNITS))

        print(f"{now} - Signal: {side} entry~{entry:.2f} sl={sl:.2f} tp={tp:.2f} "
              f"ATR={a:.2f} units={units} (~{units * sl_dist * usd_to_acct:.0f} {acct_ccy} risk)")
        result = place_order(side, units, sl, tp)
        print(f"  Order result: {result}")

    except Exception:
        print(f"{datetime.datetime.now()} - UNEXPECTED ERROR this cycle:")
        traceback.print_exc()


if __name__ == "__main__":
    if not OANDA_TOKEN or not OANDA_ACCOUNT_ID:
        raise SystemExit("Missing OANDA_TOKEN or OANDA_ACCOUNT_ID environment variables.")
    run_once()
