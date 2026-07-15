# 2D — Strategy Plugin System + Refined Long/Short Strategy Plan

> **For agentic workers:** TDD, one PR per task through the protected-main gate. Build 2D **before or alongside** 2A's StrategyRegistry (Task A3 depends on the registry API below).

**Goal:** A plugin system where a strategy is "drop a file, tag it with a name, launch by name," plus a new long/short strategy that replaces the deleted AMVR/APTT.

## Global Constraints
- Python 3.11/3.12/3.13; pyright clean; TDD; no network in tests.
- Delete the old concrete strategies; keep the `Strategy` protocol + one minimal reference strategy.

## File Structure
- `src/tradingbot/strategies/__init__.py` — auto-imports sibling modules so `@strategy` registrations fire
- `src/tradingbot/strategies/base.py` — `Strategy` protocol + `StrategyContext`
- `src/tradingbot/strategies/registry.py` — `@strategy(name)`, `build_strategy(name, ctx)`, `available_strategies()`
- `src/tradingbot/strategies/example.py` — minimal reference strategy (`@strategy("example")`)
- `tests/test_strategies.py`

## Tasks

### Task D1 — Registry + StrategyContext + protocol
- **Files:** `strategies/base.py`, `strategies/registry.py`, `tests/test_strategies.py`
- **Deliverable:**
  - `StrategyContext` (frozen dataclass): `symbol: str`, `timeframe: str`, `quantity: float`, `data_feed: Any` (for MTF fetches, satisfies `warmup_candles`), `params: dict`.
  - `Strategy` protocol: `on_bar(candles: Sequence[Candle]) -> Signal | None`.
  - `registry.py`: `@strategy(name)` class decorator registering a factory; `build_strategy(name, ctx) -> Strategy` (constructs via `cls(ctx)` or a registered `create(ctx)`); `available_strategies() -> list[str]`; duplicate-name registration raises.
- **Tests:** register a dummy strategy, `available_strategies()` includes it, `build_strategy` returns an instance, unknown name raises, duplicate name raises.

### Task D2 — Auto-discovery + example strategy + delete AMVR
- **Files:** `strategies/__init__.py`, `strategies/example.py`; move any reusable HMA/velocity helpers out of `amvr.py` into a shared module if the reference strategy needs them; **delete** `src/tradingbot/amvr.py` + `tests/test_amvr_strategy.py`; update `__main__.py` to build the strategy via `build_strategy` from config (`STRATEGY` env, default `"example"`).
- **Deliverable:** `strategies/__init__.py` walks the package (`pkgutil.iter_modules`) and imports each module so decorators run on package import. `example.py`: a minimal, well-behaved reference strategy (e.g. a simple SMA cross or "always hold") registered as `@strategy("example")`, constructed from `StrategyContext`. `__main__.py` no longer imports AMVR.
- **Tests:** importing `tradingbot.strategies` makes `"example"` appear in `available_strategies()` with no manual import; `example` builds and returns `None`/a valid `Signal` from `on_bar`. Full suite green after AMVR removal (fix any references).

### Task D3 — Refined long/short strategy (its own design first)
- **Files:** `strategies/<name>.py`, `tests/test_<name>_strategy.py`
- **Note:** the actual refined strategy is **not yet specified** — its rules come from a separate design pass (brainstorm the entry/exit/risk logic with the user before coding). This task is a placeholder for that work.
- **Deliverable (once specified):** a `@strategy("<name>")` long/short strategy: emits `Side.buy`/`Action.buy` (long) and `Side.sell` (short) plus `Action.close` (flatten); uses `ctx.data_feed` for any MTF confirmation; edge-triggered internal state; conservative on missing data.
- **Tests:** entry conditions (long and short), exit/flatten, edge-trigger no-repeat, insufficient-data returns None, MTF-fetch failure blocks entry (mirror the AMVR test patterns).

## Deferred
- The refined strategy's exact rules — brainstorm before Task D3.
