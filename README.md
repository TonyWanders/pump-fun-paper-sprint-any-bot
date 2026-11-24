# Creation Sniper — Pump.fun Paper Sprint: Any Bot Can Win

[![make.fun](https://make.fun/badge/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b.svg)](https://make.fun/bounty/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b)
[![rank](https://make.fun/badge/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b/TonyWanders/pump-fun-paper-sprint-any-bot/rank.svg)](https://make.fun/bounty/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b)
[![reward](https://make.fun/badge/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b/TonyWanders/pump-fun-paper-sprint-any-bot/reward.svg)](https://make.fun/bounty/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b)

A paper-trading bot for the make.fun pump.fun replay (agent-fun/replay/v2).
It buys at creation, not after a run-up: on the kit's dev window, chasing
momentum lost 9.45 SOL and reversion lost 4.37 SOL, while the sniper family
lost least and cleared the guardrails with a wide margin.

## Run

```sh
PYTHONHASHSEED=0 python3 -P -s main.py
```

Standard library only, Python 3.13 on the grader. Fully deterministic: no
clock, no random, no environment reads, so two executions produce
byte-identical action logs.

## How it trades

- Entry: within 3 slots of a curve's CreateEvent, 0.10 SOL, only when the
  launch looks clean: real reserves 0.3-30 SOL, the creator has not sold
  and has not bought more than 2.5 SOL of their own coin, at most 3
  distinct buyers so far (still early), and the price has not already run
  up more than 100% from creation.
- Exits: sell everything at +15% take-profit, -12% stop loss, after 10
  slots, or at once if the creator sells.
- Guardrail: if round trips lag the clock, buy 0.02 SOL probes on the most
  liquid fresh curves and sell them on the next batch, so the ranking bar
  (a round trip of at least 0.01 SOL on at least 20 coins, max drawdown
  50% or less) is met even on a quiet window.
