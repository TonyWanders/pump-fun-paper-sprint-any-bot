"""Pump.fun Paper Sprint: Any Bot Can Win — "Creation Sniper" bot.

Paper-trades pump.fun bonding curves on the agent-fun/replay/v2 harness.
One file, standard library only, fully deterministic: no clock, no random,
no environment reads; every iteration follows insertion order of the batch
stream, so two executions produce byte-identical action logs.

Idea (written from the kit's protocol and SDK, and from measuring the
kit's own baselines on the dev window with grade-local):
  - The dev window punishes chasing momentum: the kit momentum pilot lost
    9.45 SOL with a 96% drawdown, reversion lost 4.37 SOL. The sniper
    family lost least (-0.42 SOL) while meeting the guardrails with a wide
    margin, and buy-and-hold barely moved. So this bot buys at creation,
    not after a run-up.
  - Buy within 3 slots of a curve's CreateEvent, 0.10 SOL, only when the
    launch looks clean: real reserves 0.3-30 SOL, the creator has not sold
    and has not bought more than 2.5 SOL of their own coin, at most 3
    distinct buyers so far (it is still early), and the price has not
    already run up more than 100% from creation.
  - Exit fast and whole: +15% take-profit, -12% stop loss, out after 10
    slots, or at once if the creator sells. A fast cycle keeps all 5
    position slots turning over, which is what clears the ranking bar
    (a round trip of at least 0.01 SOL on at least 20 coins, drawdown
    at most 50%) with a wide margin.
  - Guardrail probes stay as a backstop: if round trips ever lag the
    clock, buy small probes on the most liquid fresh curves and sell
    them on the next batch.

Fees per round trip are about 1.25% of value per leg plus 105,000 lamports
of tx fees, so a 0.10 SOL stake needs roughly a 3% move to break even;
the +15% / -12% bracket is set around that.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

LAMPORTS_PER_SOL = makefun_bot.LAMPORTS_PER_SOL

# --- sizing (lamports) -------------------------------------------------------
STAKE = 100_000_000            # 0.10 SOL per launch
PROBE_BUY = 20_000_000         # 0.02 SOL guardrail probe buy
MIN_BUY = 10_000_000           # 0.01 SOL: the ranking bar's round-trip floor
BUY_SLIPPAGE_BPS = 10_000      # wide: a missed fill costs more than slippage
SELL_SLIPPAGE_BPS = 10_000

# --- entry filters -----------------------------------------------------------
MAX_AGE_SLOTS = 3              # buy within 3 slots of creation, never later
MIN_REAL_SOL = 300_000_000     # 0.3 SOL real reserves at creation
MAX_REAL_SOL = 30_000_000_000  # 30 SOL: not already a big mature curve
MAX_DEV_BUY = 2_500_000_000    # 2.5 SOL: creator must not have loaded up
MAX_BUYERS = 3                 # still early: at most 3 distinct buyers
MAX_RUNUP_BPS = 10000          # price at most +100% over creation price

# --- exits -------------------------------------------------------------------
STOP_LOSS_BPS = 8800            # sell when value falls to 88% of cost
TAKE_PROFIT_BPS = 11500         # sell when value reaches 115% of cost
MAX_HOLD_SLOTS = 10             # about 4 seconds: free the slot fast

# --- guardrail pacing --------------------------------------------------------
WINDOW_SLOTS = 54000            # about 6 hours of Solana slots
TARGET_COINS = 20               # ranking bar: round trips on 20 coins
TAPE_LEN = 40                   # trades kept per curve


class Tape:
    """A curve's early tape, from its CreateEvent on."""

    __slots__ = ("created_slot", "creator", "create_price", "trades",
                 "buyers", "dev_buy", "dev_sold")

    def __init__(self, created_slot, creator, create_price):
        self.created_slot = created_slot
        self.creator = creator
        self.create_price = create_price  # (num, den) at creation
        self.trades = 0
        self.buyers = {}          # wallet -> buy lamports (count only)
        self.dev_buy = 0          # lamports the creator bought
        self.dev_sold = False     # the creator sold any amount

    def add(self, is_buy, sol, user):
        self.trades += 1
        if is_buy:
            self.buyers[user] = self.buyers.get(user, 0) + sol
            if user == self.creator:
                self.dev_buy += sol
        elif user == self.creator:
            self.dev_sold = True


class Position:
    """One open position and its exit bookkeeping."""

    __slots__ = ("opened_slot", "invested", "probe", "exiting")

    def __init__(self, opened_slot, invested, probe=False):
        self.opened_slot = opened_slot
        self.invested = invested          # lamports spent on buys so far
        self.probe = probe                # guardrail probe: exit next batch
        self.exiting = False              # a sell is in flight for this mint


# --- module state (reset per process run; the harness runs one window) -------
tapes = {}          # mint -> Tape, insertion order
held = {}           # mint -> Position, insertion order
decided = {}        # mint -> True once its buy is decided (never re-enter)
round_tripped = {}  # mint -> True once it closed a >= 0.01 SOL round trip
bought_lamports = {}  # mint -> lamports of buys filled so far
first_slot = None


def price_of(curve):
    """Current price as (virtual SOL reserves, virtual token reserves)."""
    num = curve.virtual_token_reserves
    den = curve.virtual_quote_reserves
    if num <= 0 or den <= 0:
        return None
    return (den, num)  # lamports of SOL per token, as a fraction


def position_value(state, mint, holding):
    curve = state.curves.get(mint)
    if curve is None:
        return None
    price = price_of(curve)
    if price is None:
        return None
    return holding.token_amount * price[0] // price[1]


def entry_ok(state, mint, tape, slot):
    """True when a just-created curve earns its snipe buy."""
    if mint in decided or mint in held or mint in round_tripped:
        return False
    age = slot - tape.created_slot
    if age < 0 or age > MAX_AGE_SLOTS:
        return False
    curve = state.curves.get(mint)
    if curve is None or curve.complete:
        return False
    if tape.dev_sold:
        return False
    if tape.dev_buy > MAX_DEV_BUY:
        return False
    if len(tape.buyers) > MAX_BUYERS:
        return False
    real_sol = curve.real_quote_reserves
    if real_sol < MIN_REAL_SOL or real_sol > MAX_REAL_SOL:
        return False
    price = price_of(curve)
    if price is None or tape.create_price is None:
        return False
    # Price must not already have run up more than 100% from creation.
    if price[0] * tape.create_price[1] * 10000 > (10000 + MAX_RUNUP_BPS) * tape.create_price[0] * price[1]:
        return False
    return True


def on_batch(state, batch):
    global first_slot
    slot = batch["slot"]
    if first_slot is None:
        first_slot = slot
    orders = []
    caps = state.hello["caps"]
    tx = state.tx_fee_lamports()
    free = state.free_cash_lamports()
    slots_free = caps["max_open_positions"] - state.committed_position_count()

    # 1. Fold this batch's trades into the tapes (insertion order preserved).
    for e in batch["events"]:
        if e["event"] == "CreateEvent":
            v_sol = int(e["virtual_sol_reserves"])
            v_tok = int(e["virtual_token_reserves"])
            create_price = (v_sol, v_tok) if v_sol > 0 and v_tok > 0 else None
            creator = e.get("creator") or e.get("user")
            tapes.setdefault(e["mint"], Tape(e["slot"], creator, create_price))
        elif e["event"] == "TradeEvent":
            mint = e["mint"]
            tape = tapes.get(mint)
            if tape is None:
                tape = Tape(e["slot"], None, None)
                tapes[mint] = tape
            tape.add(bool(e["is_buy"]), int(e["sol_amount"]), e["user"])

    # 2. Track fills: record buy sizes, close round trips on sells.
    for f in batch["fills"]:
        if f.get("status") != "filled":
            continue
        mint = f["mint"]
        if f["side"] == "buy":
            bought_lamports[mint] = bought_lamports.get(mint, 0) + int(f["quote_lamports"])
        elif f["side"] == "sell":
            if bought_lamports.get(mint, 0) >= MIN_BUY:
                round_tripped[mint] = True

    # 3. Exits first: every held position is judged on value, age and the
    # creator. A buy in flight is not in state.positions yet (fills land 2
    # slots after the decision), so a held mint with a pending buy is kept,
    # not mistaken for a harness-closed position. A sell already in flight
    # is not re-sent; if it was rejected, exiting clears and it is judged
    # again.
    pending_sells = {p.order["mint"] for p in state.pending.values()
                     if p.order["side"] == "sell"}
    pending_buys = {p.order["mint"] for p in state.pending.values()
                    if p.order["side"] == "buy"}
    for mint in list(held.keys()):
        holding = state.positions.get(mint)
        pos = held[mint]
        if holding is None:
            if mint in pending_buys:
                continue  # buy not filled yet; keep managing it
            # Position already closed by the harness (complete / flatten).
            del held[mint]
            continue
        if pos.exiting:
            if mint in pending_sells:
                continue
            pos.exiting = False  # the sell did not land; judge again
        value = position_value(state, mint, holding)
        if value is None:
            continue
        cost = holding.cost_lamports or pos.invested or 1
        sell = False
        if pos.probe and slot > pos.opened_slot:
            sell = True  # guardrail probe: out on the next batch, win or lose
        elif value * 10000 <= STOP_LOSS_BPS * cost:
            sell = True
        elif value * 10000 >= TAKE_PROFIT_BPS * cost:
            sell = True
        elif slot - pos.opened_slot >= MAX_HOLD_SLOTS:
            sell = True
        else:
            tape = tapes.get(mint)
            if tape is not None and tape.dev_sold:
                sell = True  # creator sold: get out at once
        if sell:
            orders.append({"side": "sell", "mint": mint,
                           "token_amount": holding.token_amount,
                           "max_slippage_bps": SELL_SLIPPAGE_BPS})
            pos.exiting = True  # stays in held until the fill confirms
            if len(orders) >= caps["max_orders_per_batch"]:
                return orders

    # 4. New entries: every curve created within the last 3 slots, in the
    # batch's event order, then any other fresh curve in the tape order.
    for e in batch["events"]:
        if len(orders) >= caps["max_orders_per_batch"] or slots_free <= 0:
            break
        mint = e["mint"]
        tape = tapes.get(mint)
        if tape is None or not entry_ok(state, mint, tape, slot):
            continue
        cap = state.max_buy_lamports(mint)
        amount = min(cap, STAKE)
        if amount < MIN_BUY or free < amount + tx:
            continue  # cap grows with reserves; retry on later batches
        orders.append({"side": "buy", "mint": mint, "quote_lamports": amount,
                       "max_slippage_bps": BUY_SLIPPAGE_BPS})
        decided[mint] = True
        held[mint] = Position(slot, amount)
        free -= amount + tx
        slots_free -= 1

    # 5. Guardrail probe: if round trips lag the clock, force the pace.
    if first_slot is not None and len(round_tripped) < TARGET_COINS:
        elapsed = slot - first_slot
        expected = TARGET_COINS * elapsed // WINDOW_SLOTS
        lagging = len(round_tripped) + len(held) < expected - 2
        if lagging and slots_free > 0 and len(orders) < caps["max_orders_per_batch"]:
            best_mint = None
            best_real = 0
            for m, curve in state.curves.items():
                if m in held or m in decided or m in round_tripped or curve.complete:
                    continue
                if curve.real_quote_reserves > best_real:
                    best_real = curve.real_quote_reserves
                    best_mint = m
            if best_mint is not None and best_real >= MIN_REAL_SOL:
                cap = state.max_buy_lamports(best_mint)
                amount = min(cap, PROBE_BUY)
                if amount >= MIN_BUY and free >= amount + tx:
                    orders.append({"side": "buy", "mint": best_mint,
                                   "quote_lamports": amount,
                                   "max_slippage_bps": BUY_SLIPPAGE_BPS})
                    decided[best_mint] = True
                    held[best_mint] = Position(slot, amount, probe=True)
                    free -= amount + tx

    return orders


def on_done(state, done):
    sys.stderr.write(
        "creation-sniper done: batches=%d coins_traded=%d round_trips=%d "
        "cash=%d\n" % (state.seq, len(bought_lamports), len(round_tripped),
                       state.cash))


if __name__ == "__main__":
    makefun_bot.run(on_batch, on_done=on_done)
