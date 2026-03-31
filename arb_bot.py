"""
Kalshi / Polymarket Binary Arbitrage Bot
=========================================
LIVE_MODE = False  →  mock data, no real orders (safe to run anytime)
LIVE_MODE = True   →  hits real APIs, places real orders (needs .env credentials)
"""

import os
import re
import time
import logging
import random
from dataclasses import dataclass
from typing import Optional

import requests

# Try to load .env if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG  —  edit these
# ══════════════════════════════════════════════════════════════════════════════
LIVE_MODE       = False   # ← flip to True when you have real accounts + .env set up
MIN_PROFIT_PCT  = 0.02    # minimum 2% profit after fees before firing a trade
MAX_TRADE_USD   = 100     # max dollars per arb leg
POLL_INTERVAL_S = 15      # seconds between scans
NUM_MOCK_SCANS  = 5       # only used in mock mode

KALSHI_FEE_PCT  = 0.07    # 7% of winnings
POLY_FEE_PCT    = 0.02    # ~2% taker fee

KALSHI_BASE     = "https://trading-api.kalshi.com/trade-api/v2"
POLY_BASE       = "https://clob.polymarket.com"

# ══════════════════════════════════════════════════════════════════════════════
#  DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class MarketPair:
    description:       str
    kalshi_ticker:     str
    poly_yes_token_id: str
    poly_no_token_id:  str

@dataclass
class ArbOpportunity:
    pair:             MarketPair
    direction:        str
    leg_a_price:      float
    leg_b_price:      float
    raw_cost:         float
    cost_after_fees:  float
    ev:               float
    profit_pct:       float

# ══════════════════════════════════════════════════════════════════════════════
#  KALSHI CLIENT
# ══════════════════════════════════════════════════════════════════════════════
class KalshiClient:
    def __init__(self):
        self.session = requests.Session()

    def login(self, email: str, password: str) -> None:
        resp = self.session.post(
            f"{KALSHI_BASE}/login",
            json={"email": email, "password": password},
        )
        resp.raise_for_status()
        token = resp.json()["token"]
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        log.info("Kalshi: logged in as %s", email)

    def get_markets(self, limit: int = 200, cursor: str = "") -> dict:
        params = {"limit": limit, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        resp = self.session.get(f"{KALSHI_BASE}/markets", params=params)
        resp.raise_for_status()
        return resp.json()

    def best_ask(self, ticker: str, side: str) -> Optional[float]:
        try:
            resp = self.session.get(f"{KALSHI_BASE}/markets/{ticker}/orderbook")
            resp.raise_for_status()
            ob   = resp.json().get("orderbook", {})
            asks = ob.get(f"{side}_ask", [])
            return asks[0][0] / 100 if asks else None
        except Exception as e:
            log.warning("Kalshi orderbook error %s: %s", ticker, e)
            return None

    def place_order(self, ticker: str, side: str, count: int, limit_price: int) -> dict:
        payload = {
            "ticker": ticker,
            "side":   side,
            "count":  count,
            "type":   "limit",
            ("yes_price" if side == "yes" else "no_price"): limit_price,
            "action": "buy",
        }
        resp = self.session.post(f"{KALSHI_BASE}/portfolio/orders", json=payload)
        resp.raise_for_status()
        return resp.json()

# ══════════════════════════════════════════════════════════════════════════════
#  POLYMARKET CLIENT
# ══════════════════════════════════════════════════════════════════════════════
class PolymarketClient:
    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        self.session = requests.Session()
        self.session.headers.update({
            "POLY-API-KEY":        api_key,
            "POLY-API-SECRET":     api_secret,
            "POLY-API-PASSPHRASE": passphrase,
        })

    def get_markets(self, next_cursor: str = "") -> dict:
        params = {"active": "true", "closed": "false"}
        if next_cursor:
            params["next_cursor"] = next_cursor
        resp = self.session.get(f"{POLY_BASE}/markets", params=params)
        resp.raise_for_status()
        return resp.json()

    def best_ask(self, token_id: str) -> Optional[float]:
        try:
            resp = self.session.get(f"{POLY_BASE}/book", params={"token_id": token_id})
            resp.raise_for_status()
            asks = resp.json().get("asks", [])
            return float(asks[0]["price"]) if asks else None
        except Exception as e:
            log.warning("Poly orderbook error %s: %s", token_id, e)
            return None

    def place_order(self, token_id: str, side: str, price: float, size: float) -> dict:
        payload = {
            "token_id": token_id,
            "side":     side,
            "price":    str(price),
            "size":     str(size),
            "type":     "GTC",
        }
        resp = self.session.post(f"{POLY_BASE}/order", json=payload)
        resp.raise_for_status()
        return resp.json()

# ══════════════════════════════════════════════════════════════════════════════
#  MOCK CLIENTS  (used when LIVE_MODE = False)
# ══════════════════════════════════════════════════════════════════════════════
MOCK_MARKETS = [
    {"description": "Will the Fed cut rates in May 2025?",
     "kalshi_ticker": "FED-25MAY-CUT",
     "kalshi_yes": 0.41, "kalshi_no": 0.62, "poly_yes": 0.55, "poly_no": 0.48},
    {"description": "Will BTC close above $100k on Dec 31 2025?",
     "kalshi_ticker": "BTC-100K-DEC25",
     "kalshi_yes": 0.52, "kalshi_no": 0.50, "poly_yes": 0.53, "poly_no": 0.49},
    {"description": "Will the US enter a recession in 2025?",
     "kalshi_ticker": "US-RECESSION-25",
     "kalshi_yes": 0.47, "kalshi_no": 0.55, "poly_yes": 0.38, "poly_no": 0.64},
    {"description": "Will Apple release an AR headset in 2025?",
     "kalshi_ticker": "AAPL-AR-2025",
     "kalshi_yes": 0.30, "kalshi_no": 0.72, "poly_yes": 0.29, "poly_no": 0.73},
    {"description": "Will US unemployment exceed 5% in Q3 2025?",
     "kalshi_ticker": "UNEMP-5PCT-Q3",
     "kalshi_yes": 0.33, "kalshi_no": 0.69, "poly_yes": 0.44, "poly_no": 0.58},
]

class MockKalshiClient:
    def best_ask(self, ticker: str, side: str) -> Optional[float]:
        mkt = next((m for m in MOCK_MARKETS if m["kalshi_ticker"] == ticker), None)
        if not mkt:
            return None
        base = mkt["kalshi_yes"] if side == "yes" else mkt["kalshi_no"]
        return round(base + random.uniform(-0.005, 0.005), 4)

    def place_order(self, ticker: str, side: str, count: int, limit_price: int) -> dict:
        log.info("  [MOCK] Kalshi  %-25s  %-3s  %3d contracts @ %d¢  ($%.2f)",
                 ticker, side.upper(), count, limit_price, count * limit_price / 100)
        return {"status": "mock_filled"}

class MockPolymarketClient:
    def best_ask(self, token_id: str) -> Optional[float]:
        ticker, outcome = token_id.rsplit("_", 1)
        mkt = next((m for m in MOCK_MARKETS if m["kalshi_ticker"] == ticker), None)
        if not mkt:
            return None
        base = mkt["poly_yes"] if outcome == "YES" else mkt["poly_no"]
        return round(base + random.uniform(-0.005, 0.005), 4)

    def place_order(self, token_id: str, side: str, price: float, size: float) -> dict:
        log.info("  [MOCK] Poly    %-30s  %-3s  %.2f units @ %.4f  ($%.2f)",
                 token_id, side, size, price, size * price)
        return {"status": "mock_filled"}

# ══════════════════════════════════════════════════════════════════════════════
#  MARKET MATCHING
# ══════════════════════════════════════════════════════════════════════════════
def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def build_mock_pairs() -> list[MarketPair]:
    return [
        MarketPair(
            description       = m["description"],
            kalshi_ticker     = m["kalshi_ticker"],
            poly_yes_token_id = f"{m['kalshi_ticker']}_YES",
            poly_no_token_id  = f"{m['kalshi_ticker']}_NO",
        )
        for m in MOCK_MARKETS
    ]

def match_live_markets(kalshi: KalshiClient, poly: PolymarketClient) -> list[MarketPair]:
    log.info("Fetching Kalshi markets...")
    kalshi_markets = {}
    cursor = ""
    while True:
        data = kalshi.get_markets(limit=200, cursor=cursor)
        for m in data.get("markets", []):
            kalshi_markets[normalize(m.get("title", ""))] = m
        cursor = data.get("cursor", "")
        if not cursor:
            break
    log.info("Kalshi: %d open markets", len(kalshi_markets))

    log.info("Fetching Polymarket markets...")
    poly_markets = {}
    next_cursor = ""
    while True:
        data = poly.get_markets(next_cursor=next_cursor)
        for m in data.get("data", []):
            if m.get("active"):
                poly_markets[normalize(m.get("question", ""))] = m
        next_cursor = data.get("next_cursor", "")
        if not next_cursor or next_cursor == "LTE=":
            break
    log.info("Polymarket: %d open markets", len(poly_markets))

    pairs = []
    for k_key, k_mkt in kalshi_markets.items():
        k_words = set(k_key.split())
        for p_key, p_mkt in poly_markets.items():
            if len(k_words & set(p_key.split())) >= 4:
                tokens    = p_mkt.get("tokens", [])
                yes_token = next((t["token_id"] for t in tokens if t["outcome"] == "Yes"), None)
                no_token  = next((t["token_id"] for t in tokens if t["outcome"] == "No"),  None)
                if yes_token and no_token:
                    pairs.append(MarketPair(
                        description       = k_mkt.get("title", k_key),
                        kalshi_ticker     = k_mkt["ticker"],
                        poly_yes_token_id = yes_token,
                        poly_no_token_id  = no_token,
                    ))
    log.info("Matched %d market pairs", len(pairs))
    return pairs

# ══════════════════════════════════════════════════════════════════════════════
#  ARB LOGIC
# ══════════════════════════════════════════════════════════════════════════════
def cost_with_fees(price: float, platform: str) -> float:
    if platform == "kalshi":
        return price + KALSHI_FEE_PCT * (1 - price)
    return price * (1 + POLY_FEE_PCT)

def evaluate_arb(pair: MarketPair, kalshi, poly) -> Optional[ArbOpportunity]:
    k_yes = kalshi.best_ask(pair.kalshi_ticker, "yes")
    k_no  = kalshi.best_ask(pair.kalshi_ticker, "no")
    p_yes = poly.best_ask(pair.poly_yes_token_id)
    p_no  = poly.best_ask(pair.poly_no_token_id)

    if None in (k_yes, k_no, p_yes, p_no):
        return None

    candidates = []

    # Direction A: YES on Poly + NO on Kalshi
    cost_a = cost_with_fees(p_yes, "poly") + cost_with_fees(k_no, "kalshi")
    ev_a   = 1.0 - cost_a
    if ev_a > 0:
        candidates.append(ArbOpportunity(
            pair=pair, direction="YES_POLY + NO_KALSHI",
            leg_a_price=p_yes, leg_b_price=k_no,
            raw_cost=p_yes + k_no, cost_after_fees=cost_a,
            ev=ev_a, profit_pct=ev_a / cost_a,
        ))

    # Direction B: YES on Kalshi + NO on Poly
    cost_b = cost_with_fees(k_yes, "kalshi") + cost_with_fees(p_no, "poly")
    ev_b   = 1.0 - cost_b
    if ev_b > 0:
        candidates.append(ArbOpportunity(
            pair=pair, direction="YES_KALSHI + NO_POLY",
            leg_a_price=k_yes, leg_b_price=p_no,
            raw_cost=k_yes + p_no, cost_after_fees=cost_b,
            ev=ev_b, profit_pct=ev_b / cost_b,
        ))

    if not candidates:
        return None
    best = max(candidates, key=lambda x: x.profit_pct)
    return best if best.profit_pct >= MIN_PROFIT_PCT else None

def execute_arb(opp: ArbOpportunity, kalshi, poly) -> float:
    contracts   = int(MAX_TRADE_USD / max(opp.leg_a_price, opp.leg_b_price))
    gross_cost  = contracts * opp.cost_after_fees
    profit      = contracts * opp.ev

    print(f"\n  {'─'*58}")
    print(f"  ARB OPPORTUNITY")
    print(f"  Market    : {opp.pair.description[:65]}")
    print(f"  Direction : {opp.direction}")
    print(f"  Leg A     : {opp.leg_a_price:.4f}")
    print(f"  Leg B     : {opp.leg_b_price:.4f}")
    print(f"  Raw cost  : {opp.raw_cost:.4f}  →  after fees: {opp.cost_after_fees:.4f}")
    print(f"  Profit    : {opp.profit_pct*100:.2f}%  |  ${profit:.2f} on {contracts} contracts")
    print(f"  {'─'*58}")

    if "POLY" in opp.direction.split("+")[0]:
        poly.place_order(opp.pair.poly_yes_token_id, "BUY",
                         opp.leg_a_price, round(contracts * opp.leg_a_price, 2))
        kalshi.place_order(opp.pair.kalshi_ticker, "no",
                           contracts, int(opp.leg_b_price * 100))
    else:
        kalshi.place_order(opp.pair.kalshi_ticker, "yes",
                           contracts, int(opp.leg_a_price * 100))
        poly.place_order(opp.pair.poly_no_token_id, "BUY",
                         opp.leg_b_price, round(contracts * opp.leg_b_price, 2))

    return profit

# ══════════════════════════════════════════════════════════════════════════════
#  SCANNER
# ══════════════════════════════════════════════════════════════════════════════
def run_scanner(kalshi, poly, pairs: list[MarketPair], max_scans: Optional[int] = None) -> None:
    total_profit = 0.0
    scan = 0
    while True:
        scan += 1
        print(f"\n{'═'*60}")
        print(f"  SCAN #{scan}  —  {len(pairs)} markets  ({'LIVE' if LIVE_MODE else 'MOCK'})")
        print(f"{'═'*60}")

        scan_profit = 0.0
        for pair in pairs:
            opp = evaluate_arb(pair, kalshi, poly)
            if opp:
                scan_profit += execute_arb(opp, kalshi, poly)
            else:
                log.info("  No arb  : %s", pair.description[:55])

        total_profit += scan_profit
        print(f"\n  Scan #{scan} P&L: ${scan_profit:.2f}  |  Session total: ${total_profit:.2f}")

        if max_scans and scan >= max_scans:
            break
        time.sleep(POLL_INTERVAL_S)

    print(f"\n{'═'*60}")
    print(f"  DONE — {scan} scans  |  Total simulated profit: ${total_profit:.2f}")
    print(f"{'═'*60}\n")

# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    random.seed(42)
    print(f"\n  Kalshi/Polymarket Arb Bot  —  {'LIVE MODE' if LIVE_MODE else 'MOCK MODE'}")
    print(f"  MIN_PROFIT={MIN_PROFIT_PCT*100:.0f}%  MAX_TRADE=${MAX_TRADE_USD}\n")

    if LIVE_MODE:
        # ── Real clients ──────────────────────────────────────────────────────
        kalshi = KalshiClient()
        kalshi.login(
            email    = os.environ["KALSHI_EMAIL"],
            password = os.environ["KALSHI_PASSWORD"],
        )
        poly = PolymarketClient(
            api_key    = os.environ["POLY_API_KEY"],
            api_secret = os.environ["POLY_API_SECRET"],
            passphrase = os.environ["POLY_PASSPHRASE"],
        )
        pairs = match_live_markets(kalshi, poly)
        run_scanner(kalshi, poly, pairs)          # runs forever until Ctrl+C

    else:
        # ── Mock clients ──────────────────────────────────────────────────────
        kalshi = MockKalshiClient()
        poly   = MockPolymarketClient()
        pairs  = build_mock_pairs()
        run_scanner(kalshi, poly, pairs, max_scans=NUM_MOCK_SCANS)
