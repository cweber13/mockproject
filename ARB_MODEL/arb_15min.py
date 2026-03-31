"""
BTC / ETH 15-Minute Up/Down Binary Arb
========================================
Platforms:  Nadex (CFTC-regulated)  vs  Derive/Lyra (DeFi)
Contract:   "Will BTC/ETH be HIGHER in 15 minutes than it is now?"
Payout:     $100 if correct, $0 if wrong  (Nadex uses $100 contracts)

Arb logic:
  - Buy YES on whichever platform prices it cheaper
  - Buy NO  on the other platform
  - Total cost < $100 = locked profit regardless of price move

LIVE_MODE = False  →  runs entirely on simulated prices, no accounts needed
LIVE_MODE = True   →  hits real APIs (requires .env credentials)
"""

import os
import time
import random
import logging
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════
LIVE_MODE        = False
MIN_PROFIT_PCT   = 0.02    # 2% minimum after fees
MAX_TRADE_USD    = 500     # Nadex contracts are $100 each, so multiples of 100
CONTRACT_SIZE    = 100     # $100 per contract (Nadex standard)
POLL_INTERVAL_S  = 5       # scan every 5 seconds in mock
NUM_MOCK_SCANS   = 6

# Fee estimates
NADEX_FEE_USD    = 0.90    # $0.90 per contract per side (buy + sell = $1.80 round trip)
DERIVE_FEE_PCT   = 0.003   # 0.3% of notional

# ══════════════════════════════════════════════════════════════════════════════
#  DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class BinaryContract:
    """A 15-minute up/down contract on one asset."""
    asset:        str       # 'BTC' or 'ETH'
    direction:    str       # 'UP' (will price be higher?) — NO side = price goes DOWN
    strike:       float     # current price at contract open
    expiry:       datetime  # when it resolves

@dataclass
class ArbOpportunity:
    contract:         BinaryContract
    buy_yes_platform: str     # 'nadex' or 'derive'
    buy_no_platform:  str
    yes_price:        float   # cost of YES leg (0-100 scale)
    no_price:         float   # cost of NO leg  (0-100 scale)
    raw_cost:         float   # yes + no before fees
    cost_after_fees:  float
    profit:           float   # dollars per pair of contracts
    profit_pct:       float

# ══════════════════════════════════════════════════════════════════════════════
#  SIMULATED MARKET STATE
# ══════════════════════════════════════════════════════════════════════════════
# Simulates a live market where prices drift around as BTC/ETH move
class MarketSimulator:
    def __init__(self):
        self.btc_price = 83_500.0
        self.eth_price =  1_820.0
        self._t = 0

    def tick(self):
        """Simulate small price moves each scan."""
        self.btc_price += random.gauss(0, 150)
        self.eth_price += random.gauss(0, 12)
        self._t += 1

    def nadex_price(self, asset: str) -> tuple[float, float]:
        """
        Nadex binary YES ask / NO ask for a 15-min UP contract.
        Returns (yes_ask, no_ask) in dollars (0-100 scale).
        Nadex bid-ask spread is typically $1-3 wide.
        """
        # Base probability that price goes up in 15 min (~50/50 with slight trend)
        base_prob = 0.50 + random.gauss(0, 0.06)
        base_prob = max(0.05, min(0.95, base_prob))

        # Nadex prices in whole dollars, spread ~2
        yes_ask = round(base_prob * 100 + random.uniform(0.5, 2.0), 1)
        no_ask  = round((1 - base_prob) * 100 + random.uniform(0.5, 2.0), 1)
        return yes_ask, no_ask

    def derive_price(self, asset: str) -> tuple[float, float]:
        """
        Derive (Lyra) binary YES ask / NO ask.
        DeFi AMM pricing — sometimes lags Nadex, creating arb.
        Spread ~1-4 depending on liquidity.
        """
        base_prob = 0.50 + random.gauss(0, 0.07)
        base_prob = max(0.05, min(0.95, base_prob))

        yes_ask = round(base_prob * 100 + random.uniform(0.3, 3.5), 1)
        no_ask  = round((1 - base_prob) * 100 + random.uniform(0.3, 3.5), 1)
        return yes_ask, no_ask

sim = MarketSimulator()

# ══════════════════════════════════════════════════════════════════════════════
#  MOCK CLIENTS
# ══════════════════════════════════════════════════════════════════════════════
class MockNadexClient:
    def get_yes_ask(self, asset: str) -> float:
        return sim.nadex_price(asset)[0]

    def get_no_ask(self, asset: str) -> float:
        return sim.nadex_price(asset)[1]

    def place_order(self, asset: str, side: str, contracts: int, price: float) -> dict:
        log.info("  [NADEX ] %-4s  %-3s  %d contract(s) @ $%.1f  (cost $%.2f)",
                 asset, side, contracts, price, contracts * price)
        return {"status": "mock_filled", "platform": "nadex"}

class MockDeriveClient:
    def get_yes_ask(self, asset: str) -> float:
        return sim.derive_price(asset)[0]

    def get_no_ask(self, asset: str) -> float:
        return sim.derive_price(asset)[1]

    def place_order(self, asset: str, side: str, contracts: int, price: float) -> dict:
        log.info("  [DERIVE] %-4s  %-3s  %d contract(s) @ $%.1f  (cost $%.2f)",
                 asset, side, contracts, price, contracts * price)
        return {"status": "mock_filled", "platform": "derive"}

# ══════════════════════════════════════════════════════════════════════════════
#  LIVE CLIENTS (stubs — fill in when you have API keys)
# ══════════════════════════════════════════════════════════════════════════════
class NadexClient:
    """
    Real Nadex REST client.

    Setup:
      1. Create account at nadex.com (ID verification + bank funding required)
      2. Add to .env:
            NADEX_USERNAME=your@email.com
            NADEX_PASSWORD=yourpassword
      3. pip install requests

    Nadex contract naming for BTC/ETH 20-min binaries:
      BTC/USD  >  [strike]  @  [expiry_time]   e.g. "Bitcoin 83500 (2pm)"
    Expiries run every 20 minutes during market hours.
    """

    BASE = "https://api.nadex.com"

    def __init__(self):
        import requests
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self._token     = None
        self._contracts = {}   # cache: asset → {yes_id, no_id, yes_ask, no_ask}

    def login(self, username: str, password: str) -> None:
        resp = self.session.post(f"{self.BASE}/authentication", json={
            "username": username,
            "password": password,
        })
        resp.raise_for_status()
        self._token = resp.json()["token"]
        self.session.headers.update({"Authorization": f"Bearer {self._token}"})
        log.info("Nadex: logged in as %s", username)

    def _fetch_binary_contracts(self, asset: str) -> dict:
        """
        Fetch the nearest upcoming 20-min binary contract for BTC or ETH.
        Returns {yes_ask, no_ask, yes_contract_id, no_contract_id, expiry}
        """
        # Nadex market names
        market_name = "Bitcoin Binary" if asset == "BTC" else "Ether Binary"
        resp = self.session.get(f"{self.BASE}/marketdata/contracts", params={
            "market": market_name,
            "type":   "Binary",
            "status": "Open",
        })
        resp.raise_for_status()
        contracts = resp.json().get("contracts", [])

        # Pick the contract expiring soonest (nearest 20-min window)
        contracts.sort(key=lambda c: c["expiry"])
        nearest = contracts[0] if contracts else None
        if not nearest:
            return {}

        # Each binary has a YES (call) and NO (put) side
        return {
            "yes_contract_id": nearest["callId"],
            "no_contract_id":  nearest["putId"],
            "yes_ask":         float(nearest["callAsk"]),
            "no_ask":          float(nearest["putAsk"]),
            "expiry":          nearest["expiry"],
        }

    def get_yes_ask(self, asset: str) -> float:
        self._contracts[asset] = self._fetch_binary_contracts(asset)
        return self._contracts[asset].get("yes_ask", 50.0)

    def get_no_ask(self, asset: str) -> float:
        if asset not in self._contracts:
            self._fetch_binary_contracts(asset)
        return self._contracts[asset].get("no_ask", 50.0)

    def place_order(self, asset: str, side: str, contracts: int, price: float) -> dict:
        contract_id = (
            self._contracts[asset]["yes_contract_id"] if side == "YES"
            else self._contracts[asset]["no_contract_id"]
        )
        resp = self.session.post(f"{self.BASE}/trading/orders", json={
            "contractId": contract_id,
            "side":       "BUY",
            "quantity":   contracts,
            "price":      price,
            "orderType":  "LIMIT",
        })
        resp.raise_for_status()
        log.info("  [NADEX ] %-4s  %-3s  %d contract(s) @ $%.1f", asset, side, contracts, price)
        return resp.json()


class DeriveClient:
    """
    Real Derive (Lyra) client.

    Setup:
      1. Install MetaMask, create a wallet
      2. Go to derive.xyz, connect wallet
      3. Fund with ETH on Base network (for gas) + USDC (collateral)
      4. Add to .env:
            DERIVE_PRIVATE_KEY=0xYourWalletPrivateKey
      5. pip install derive-client eth-account

    Derive binary options:
      - Listed as "BTC-YYYYMMDD-STRIKE-C" (call = UP) / "BTC-YYYYMMDD-STRIKE-P" (put = DOWN)
      - Price is quoted 0-1 (multiply by 100 to match Nadex scale)
      - Nearest expiry changes dynamically; we pick the one closest to +15-20 min
    """

    BASE = "https://api.derive.xyz"

    def __init__(self):
        import requests
        from eth_account import Account
        self.session = requests.Session()
        self._wallet  = Account.from_key(os.environ["DERIVE_PRIVATE_KEY"])
        self._address = self._wallet.address
        self._cache   = {}
        log.info("Derive: wallet loaded %s", self._address[:10] + "...")

    def _get_auth_headers(self) -> dict:
        """Derive uses wallet-signed auth headers."""
        import time
        from eth_account.messages import encode_defunct
        ts  = str(int(time.time()))
        msg = encode_defunct(text=f"derive_auth_{ts}")
        sig = self._wallet.sign_message(msg).signature.hex()
        return {"X-Wallet-Address": self._address, "X-Signature": sig, "X-Timestamp": ts}

    def _fetch_binary_contract(self, asset: str) -> dict:
        """
        Fetch the nearest binary option on BTC or ETH expiring within ~20 minutes.
        Returns {yes_ask, no_ask, yes_instrument, no_instrument}
        """
        resp = self.session.get(
            f"{self.BASE}/public/get_instruments",
            params={"currency": asset, "kind": "option", "expired": False},
            headers=self._get_auth_headers(),
        )
        resp.raise_for_status()
        instruments = resp.json().get("result", [])

        now = datetime.utcnow()
        target = now + timedelta(minutes=20)

        # Filter to binary (digital) options expiring near our target window
        candidates = [
            i for i in instruments
            if i.get("is_binary")
            and abs((datetime.utcfromtimestamp(i["expiration_timestamp"]) - target).total_seconds()) < 600
        ]
        if not candidates:
            return {}

        # Split into call (UP/YES) and put (DOWN/NO)
        calls = [i for i in candidates if i["option_type"] == "call"]
        puts  = [i for i in candidates if i["option_type"] == "put"]
        if not calls or not puts:
            return {}

        call_name = calls[0]["instrument_name"]
        put_name  = puts[0]["instrument_name"]

        # Fetch orderbook for each
        def best_ask(name: str) -> float:
            r = self.session.get(f"{self.BASE}/public/get_order_book",
                                 params={"instrument_name": name})
            asks = r.json().get("result", {}).get("asks", [])
            return float(asks[0][0]) * 100 if asks else 50.0  # convert 0-1 → 0-100

        return {
            "yes_instrument": call_name,
            "no_instrument":  put_name,
            "yes_ask":        best_ask(call_name),
            "no_ask":         best_ask(put_name),
        }

    def get_yes_ask(self, asset: str) -> float:
        self._cache[asset] = self._fetch_binary_contract(asset)
        return self._cache[asset].get("yes_ask", 50.0)

    def get_no_ask(self, asset: str) -> float:
        if asset not in self._cache:
            self._fetch_binary_contract(asset)
        return self._cache[asset].get("no_ask", 50.0)

    def place_order(self, asset: str, side: str, contracts: int, price: float) -> dict:
        instrument = (
            self._cache[asset]["yes_instrument"] if side == "YES"
            else self._cache[asset]["no_instrument"]
        )
        payload = {
            "instrument_name": instrument,
            "direction":       "buy",
            "order_type":      "limit",
            "amount":          contracts,
            "price":           price / 100,   # Derive uses 0-1 scale
        }
        resp = self.session.post(
            f"{self.BASE}/private/order",
            json=payload,
            headers=self._get_auth_headers(),
        )
        resp.raise_for_status()
        log.info("  [DERIVE] %-4s  %-3s  %d contract(s) @ $%.1f", asset, side, contracts, price)
        return resp.json()

# ══════════════════════════════════════════════════════════════════════════════
#  FEE MODEL
# ══════════════════════════════════════════════════════════════════════════════
def cost_after_fees(yes_price: float, no_price: float) -> float:
    """
    Total cost for one pair (YES on one side, NO on the other) after fees.
    Nadex: $0.90 per contract per trade
    Derive: 0.3% of notional
    """
    nadex_fee  = NADEX_FEE_USD                        # flat per contract
    derive_fee = (yes_price + no_price) * DERIVE_FEE_PCT
    return yes_price + no_price + nadex_fee + derive_fee

# ══════════════════════════════════════════════════════════════════════════════
#  ARB EVALUATOR
# ══════════════════════════════════════════════════════════════════════════════
def evaluate_arb(asset: str, nadex, derive) -> Optional[ArbOpportunity]:
    n_yes = nadex.get_yes_ask(asset)
    n_no  = nadex.get_no_ask(asset)
    d_yes = derive.get_yes_ask(asset)
    d_no  = derive.get_no_ask(asset)

    expiry  = datetime.now() + timedelta(minutes=15)
    strike  = sim.btc_price if asset == "BTC" else sim.eth_price
    contract = BinaryContract(asset=asset, direction="UP", strike=strike, expiry=expiry)

    candidates = []

    # Direction A: YES on Nadex + NO on Derive
    raw_a  = n_yes + d_no
    cost_a = cost_after_fees(n_yes, d_no)
    ev_a   = CONTRACT_SIZE - cost_a
    if ev_a > 0:
        candidates.append(ArbOpportunity(
            contract          = contract,
            buy_yes_platform  = "nadex",
            buy_no_platform   = "derive",
            yes_price         = n_yes,
            no_price          = d_no,
            raw_cost          = raw_a,
            cost_after_fees   = cost_a,
            profit            = ev_a,
            profit_pct        = ev_a / cost_a,
        ))

    # Direction B: YES on Derive + NO on Nadex
    raw_b  = d_yes + n_no
    cost_b = cost_after_fees(d_yes, n_no)
    ev_b   = CONTRACT_SIZE - cost_b
    if ev_b > 0:
        candidates.append(ArbOpportunity(
            contract          = contract,
            buy_yes_platform  = "derive",
            buy_no_platform   = "nadex",
            yes_price         = d_yes,
            no_price          = n_no,
            raw_cost          = raw_b,
            cost_after_fees   = cost_b,
            profit            = ev_b,
            profit_pct        = ev_b / cost_b,
        ))

    if not candidates:
        return None

    best = max(candidates, key=lambda x: x.profit_pct)
    return best if best.profit_pct >= MIN_PROFIT_PCT else None

# ══════════════════════════════════════════════════════════════════════════════
#  ORDER EXECUTOR
# ══════════════════════════════════════════════════════════════════════════════
def execute_arb(opp: ArbOpportunity, nadex, derive) -> float:
    contracts = max(1, int(MAX_TRADE_USD / opp.cost_after_fees))
    total_profit = contracts * opp.profit

    print(f"\n  {'─'*60}")
    print(f"  ARB  {opp.contract.asset} 15-min UP/DOWN binary")
    print(f"  Strike   : ${opp.contract.strike:,.0f}  |  Expiry: {opp.contract.expiry.strftime('%H:%M:%S')}")
    print(f"  YES on   : {opp.buy_yes_platform.upper():<8}  @ ${opp.yes_price:.1f}")
    print(f"  NO  on   : {opp.buy_no_platform.upper():<8}  @ ${opp.no_price:.1f}")
    print(f"  Raw cost : ${opp.raw_cost:.2f}  →  after fees: ${opp.cost_after_fees:.2f}")
    print(f"  Payout   : $100.00  →  profit: ${opp.profit:.2f} ({opp.profit_pct*100:.1f}%)")
    print(f"  Contracts: {contracts}  →  total profit: ${total_profit:.2f}")
    print(f"  {'─'*60}")

    if opp.buy_yes_platform == "nadex":
        nadex.place_order(opp.contract.asset, "YES", contracts, opp.yes_price)
        derive.place_order(opp.contract.asset, "NO",  contracts, opp.no_price)
    else:
        derive.place_order(opp.contract.asset, "YES", contracts, opp.yes_price)
        nadex.place_order(opp.contract.asset, "NO",  contracts, opp.no_price)

    return total_profit

# ══════════════════════════════════════════════════════════════════════════════
#  SCANNER
# ══════════════════════════════════════════════════════════════════════════════
ASSETS = ["BTC", "ETH"]

def run_scanner(nadex, derive, max_scans: Optional[int] = None) -> None:
    total_profit = 0.0
    scan = 0

    while True:
        scan += 1
        sim.tick()  # move simulated prices

        print(f"\n{'═'*62}")
        print(f"  SCAN #{scan}  |  BTC ${sim.btc_price:,.0f}  |  ETH ${sim.eth_price:,.0f}")
        print(f"{'═'*62}")

        scan_profit = 0.0
        for asset in ASSETS:
            opp = evaluate_arb(asset, nadex, derive)
            if opp:
                scan_profit += execute_arb(opp, nadex, derive)
            else:
                # Show current prices even when no arb
                n_yes, n_no = nadex.get_yes_ask(asset), nadex.get_no_ask(asset)
                d_yes, d_no = derive.get_yes_ask(asset), derive.get_no_ask(asset)
                spread = (n_yes + d_no) - CONTRACT_SIZE  # negative = arb exists
                log.info("  No arb  %s  |  Nadex YES $%.1f / NO $%.1f  |  Derive YES $%.1f / NO $%.1f  |  best spread: $%.2f",
                         asset, n_yes, n_no, d_yes, d_no,
                         min(n_yes + d_no, d_yes + n_no) - CONTRACT_SIZE)

        total_profit += scan_profit
        print(f"\n  Scan #{scan} profit: ${scan_profit:.2f}  |  Session total: ${total_profit:.2f}")

        if max_scans and scan >= max_scans:
            break
        time.sleep(POLL_INTERVAL_S)

    print(f"\n{'═'*62}")
    print(f"  DONE  |  {scan} scans  |  Simulated profit: ${total_profit:.2f}")
    print(f"{'═'*62}\n")

# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    random.seed(99)
    print(f"\n  BTC/ETH 15-min Binary Arb  —  {'LIVE' if LIVE_MODE else 'MOCK'} MODE")
    print(f"  MIN_PROFIT={MIN_PROFIT_PCT*100:.0f}%  MAX_TRADE=${MAX_TRADE_USD}  CONTRACT=${CONTRACT_SIZE}\n")

    if LIVE_MODE:
        nadex = NadexClient()
        nadex.login(
            username = os.environ["NADEX_USERNAME"],
            password = os.environ["NADEX_PASSWORD"],
        )
        derive = DeriveClient()   # loads wallet from DERIVE_PRIVATE_KEY in .env
        run_scanner(nadex, derive)
    else:
        nadex  = MockNadexClient()
        derive = MockDeriveClient()
        run_scanner(nadex, derive, max_scans=NUM_MOCK_SCANS)
