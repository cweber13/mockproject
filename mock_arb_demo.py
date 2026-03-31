"""
Kalshi / Polymarket Arbitrage — MOCK DEMO
==========================================
Simulates both APIs with fake markets and prices.
No credentials needed. Shows the full scan → detect → execute flow.
"""

import random
import time
import logging
from dataclasses import dataclass
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
MIN_PROFIT_PCT  = 0.02   # 2% minimum spread after fees
MAX_TRADE_USD   = 100
KALSHI_FEE_PCT  = 0.07
POLY_FEE_PCT    = 0.02
POLL_INTERVAL_S = 3      # fast for demo
NUM_SCANS       = 5

random.seed(42)

# ── Fake market universe ───────────────────────────────────────────────────────
MOCK_MARKETS = [
    {
        "description": "Will the Fed cut rates in May 2025?",
        "kalshi_ticker": "FED-25MAY-CUT",
        # Kalshi YES ask deliberately lower than Poly → arb on direction B
        "kalshi_yes": 0.41,
        "kalshi_no":  0.62,   # note: yes+no > 1 is normal (bid-ask spread)
        "poly_yes":   0.55,
        "poly_no":    0.48,
    },
    {
        "description": "Will BTC close above $100k on Dec 31 2025?",
        "kalshi_ticker": "BTC-100K-DEC25",
        # No arb — prices consistent
        "kalshi_yes": 0.52,
        "kalshi_no":  0.50,
        "poly_yes":   0.53,
        "poly_no":    0.49,
    },
    {
        "description": "Will the US enter a recession in 2025?",
        "kalshi_ticker": "US-RECESSION-25",
        # Arb direction A: YES_POLY(0.38) + NO_KALSHI(0.55) = 0.93 → profit after fees
        "kalshi_yes": 0.47,
        "kalshi_no":  0.55,
        "poly_yes":   0.38,
        "poly_no":    0.64,
    },
    {
        "description": "Will Apple release an AR headset in 2025?",
        "kalshi_ticker": "AAPL-AR-2025",
        # Borderline — just below threshold after fees
        "kalshi_yes": 0.30,
        "kalshi_no":  0.72,
        "poly_yes":   0.29,
        "poly_no":    0.73,
    },
    {
        "description": "Will US unemployment exceed 5% in Q3 2025?",
        "kalshi_ticker": "UNEMP-5PCT-Q3",
        # Clear arb direction B: YES_KALSHI(0.33) + NO_POLY(0.58) = 0.91
        "kalshi_yes": 0.33,
        "kalshi_no":  0.69,
        "poly_yes":   0.44,
        "poly_no":    0.58,
    },
]

# ── Data classes ───────────────────────────────────────────────────────────────
@dataclass
class MarketPair:
    description:       str
    kalshi_ticker:     str
    poly_yes_token_id: str
    poly_no_token_id:  str

@dataclass
class ArbOpportunity:
    pair:           MarketPair
    direction:      str
    leg_a_price:    float
    leg_b_price:    float
    raw_cost:       float
    cost_after_fees: float
    ev:             float
    profit_pct:     float

# ── Mock clients ───────────────────────────────────────────────────────────────
class MockKalshiClient:
    def best_ask(self, ticker: str, side: str) -> Optional[float]:
        mkt = next((m for m in MOCK_MARKETS if m["kalshi_ticker"] == ticker), None)
        if not mkt:
            return None
        # Add tiny random noise each call to simulate live orderbook
        base = mkt["kalshi_yes"] if side == "yes" else mkt["kalshi_no"]
        return round(base + random.uniform(-0.005, 0.005), 4)

    def place_order(self, ticker: str, side: str, count: int, limit_price: int) -> dict:
        log.info(
            "  [MOCK ORDER] Kalshi  %-25s  %-3s  %3d contracts @ %d¢  ($%.2f)",
            ticker, side.upper(), count, limit_price, count * limit_price / 100
        )
        return {"status": "mock_filled", "ticker": ticker, "side": side}

class MockPolymarketClient:
    def best_ask(self, token_id: str) -> Optional[float]:
        # token_id format: "<ticker>_YES" or "<ticker>_NO"
        ticker, outcome = token_id.rsplit("_", 1)
        mkt = next((m for m in MOCK_MARKETS if m["kalshi_ticker"] == ticker), None)
        if not mkt:
            return None
        base = mkt["poly_yes"] if outcome == "YES" else mkt["poly_no"]
        return round(base + random.uniform(-0.005, 0.005), 4)

    def place_order(self, token_id: str, side: str, price: float, size: float) -> dict:
        log.info(
            "  [MOCK ORDER] Poly    %-30s  %-3s  %.2f units @ %.4f  ($%.2f)",
            token_id, side, size, price, size * price
        )
        return {"status": "mock_filled", "token_id": token_id}

# ── Market matching (mock) ─────────────────────────────────────────────────────
def build_mock_pairs() -> list[MarketPair]:
    pairs = []
    for m in MOCK_MARKETS:
        pairs.append(MarketPair(
            description       = m["description"],
            kalshi_ticker     = m["kalshi_ticker"],
            poly_yes_token_id = f"{m['kalshi_ticker']}_YES",
            poly_no_token_id  = f"{m['kalshi_ticker']}_NO",
        ))
    return pairs

# ── Fee model ──────────────────────────────────────────────────────────────────
def cost_with_fees(price: float, platform: str) -> float:
    if platform == "kalshi":
        return price + KALSHI_FEE_PCT * (1 - price)
    return price * (1 + POLY_FEE_PCT)

# ── Arb evaluator ──────────────────────────────────────────────────────────────
def evaluate_arb(
    pair: MarketPair,
    kalshi: MockKalshiClient,
    poly: MockPolymarketClient,
) -> Optional[ArbOpportunity]:

    k_yes = kalshi.best_ask(pair.kalshi_ticker, "yes")
    k_no  = kalshi.best_ask(pair.kalshi_ticker, "no")
    p_yes = poly.best_ask(pair.poly_yes_token_id)
    p_no  = poly.best_ask(pair.poly_no_token_id)

    if None in (k_yes, k_no, p_yes, p_no):
        return None

    candidates = []

    # Direction A: buy YES on Poly + buy NO on Kalshi
    cost_a = cost_with_fees(p_yes, "poly") + cost_with_fees(k_no, "kalshi")
    ev_a   = 1.0 - cost_a
    if ev_a > 0:
        candidates.append(ArbOpportunity(
            pair            = pair,
            direction       = "YES_POLY + NO_KALSHI",
            leg_a_price     = p_yes,
            leg_b_price     = k_no,
            raw_cost        = p_yes + k_no,
            cost_after_fees = cost_a,
            ev              = ev_a,
            profit_pct      = ev_a / cost_a,
        ))

    # Direction B: buy YES on Kalshi + buy NO on Poly
    cost_b = cost_with_fees(k_yes, "kalshi") + cost_with_fees(p_no, "poly")
    ev_b   = 1.0 - cost_b
    if ev_b > 0:
        candidates.append(ArbOpportunity(
            pair            = pair,
            direction       = "YES_KALSHI + NO_POLY",
            leg_a_price     = k_yes,
            leg_b_price     = p_no,
            raw_cost        = k_yes + p_no,
            cost_after_fees = cost_b,
            ev              = ev_b,
            profit_pct      = ev_b / cost_b,
        ))

    if not candidates:
        return None

    best = max(candidates, key=lambda x: x.profit_pct)
    return best if best.profit_pct >= MIN_PROFIT_PCT else None

# ── Order executor ─────────────────────────────────────────────────────────────
def execute_arb(
    opp: ArbOpportunity,
    kalshi: MockKalshiClient,
    poly: MockPolymarketClient,
) -> None:
    contracts = int(MAX_TRADE_USD / max(opp.leg_a_price, opp.leg_b_price))
    gross_cost = contracts * opp.cost_after_fees
    gross_payout = contracts * 1.0
    gross_profit = gross_payout - gross_cost

    print(f"\n  {'─'*58}")
    print(f"  ARB OPPORTUNITY DETECTED")
    print(f"  Market    : {opp.pair.description}")
    print(f"  Direction : {opp.direction}")
    print(f"  Leg A ask : {opp.leg_a_price:.4f}")
    print(f"  Leg B ask : {opp.leg_b_price:.4f}")
    print(f"  Raw cost  : {opp.raw_cost:.4f}  (before fees)")
    print(f"  Net cost  : {opp.cost_after_fees:.4f}  (after fees)")
    print(f"  EV/dollar : ${opp.ev:.4f}  ({opp.profit_pct*100:.2f}% ROI)")
    print(f"  Trade size: {contracts} contracts")
    print(f"  Expected  : spend ${gross_cost:.2f}  →  payout ${gross_payout:.2f}  →  profit ${gross_profit:.2f}")
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

# ── Scanner ────────────────────────────────────────────────────────────────────
def run_scanner(
    kalshi: MockKalshiClient,
    poly: MockPolymarketClient,
    pairs: list[MarketPair],
    num_scans: int,
) -> None:
    total_profit = 0.0

    for scan in range(1, num_scans + 1):
        print(f"\n{'═'*60}")
        print(f"  SCAN #{scan}  —  checking {len(pairs)} markets")
        print(f"{'═'*60}")

        scan_profit = 0.0
        for pair in pairs:
            opp = evaluate_arb(pair, kalshi, poly)
            if opp:
                contracts = int(MAX_TRADE_USD / max(opp.leg_a_price, opp.leg_b_price))
                profit = contracts * opp.ev
                scan_profit += profit
                execute_arb(opp, kalshi, poly)
            else:
                log.info("  No arb  : %s", pair.description[:55])

        total_profit += scan_profit
        print(f"\n  Scan #{scan} P&L: ${scan_profit:.2f}  |  Session total: ${total_profit:.2f}")

        if scan < num_scans:
            time.sleep(POLL_INTERVAL_S)

    print(f"\n{'═'*60}")
    print(f"  DEMO COMPLETE — {num_scans} scans run")
    print(f"  Total simulated profit: ${total_profit:.2f}")
    print(f"{'═'*60}\n")

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n  Kalshi / Polymarket Arb — MOCK DEMO")
    print(f"  MIN_PROFIT={MIN_PROFIT_PCT*100:.0f}%  MAX_TRADE=${MAX_TRADE_USD}  SCANS={NUM_SCANS}\n")

    kalshi = MockKalshiClient()
    poly   = MockPolymarketClient()
    pairs  = build_mock_pairs()

    run_scanner(kalshi, poly, pairs, num_scans=NUM_SCANS)
