# Papertrade PAPER tracker

Tracks one live number: is minting PAPER paying for itself right now? See
[SPEC.md](SPEC.md) for the full design. This repo only reads public data.

## Setup

```
pip install -e '.[dev]'
python -m pytest
```

## Commands

| Command | What it does |
|---|---|
| `python -m papertracker fetch-fixtures` | Build step 1: fetches `summary` and `history` at `1m`/`1h`/`1d` once with the retry client, saves the raw bodies to `tests/fixtures/`, writes every attempt's status to `tests/fixtures/fetch_log.json`, and prints an outline of each response's structure. Add `-v` to log each attempt. |

`poll`, `status` and `build-dashboard` come in later build steps.

## Build progress (SPEC section 8)

| Step | State |
|---|---|
| 1. Inspect and save fixtures | Done 2026-10-09 09:17 UTC: `tests/fixtures/`, documented below |
| 2. Client with retry | Done: `papertracker/client.py`, `tests/test_client.py` |
| 3. Store | Done: `papertracker/store.py`, `tests/test_store.py` |
| 4. Semantics detection | Done: `papertracker/semantics.py`, `tests/test_semantics.py`, using the revised SPEC section 4 |
| 5. Economics | Done: `papertracker/economics.py`, `tests/test_economics.py` (section 9 tests pass) |
| 6–8 | Not started |

Step 5 was built ahead of steps 3–4 because it's pure formula and doesn't
depend on the endpoint data.

## Endpoint field reference

From the fixtures fetched 2026-10-09 09:17 UTC (block 48068869). Every
numeric series was 0 at that time except `tvl` and `users`, so some meanings
below can't be confirmed from the data yet and are marked *unconfirmed*.

### `GET /query/protocol/summary`

Nested object. Amounts are **strings of raw 18-decimal integers**: divide by
1e18 using `int`/`Decimal`.

| Field | Type | Units | Value at fetch | Notes |
|---|---|---|---|---|
| `ok` | bool | | `true` | |
| `source.block` | string | block number | `48068869` | for the phase 2 cross-check |
| `source.at` | int | ms since epoch | 2026-10-09 09:17:40 | time of the snapshot |
| `source.genesis` | int | ms since epoch | 2026-10-06 22:42:00 | protocol genesis |
| `balances.tvl` | string | 18-dec USD | 14,831,637.903488 | equals `history` `totals.tvlRaw` |
| `balances.margin` | string | 18-dec USD | 0 | *unconfirmed*: trader margin |
| `balances.lp` | string | 18-dec USD | 0 | *unconfirmed*: LP balance |
| `balances.reserve` | string | 18-dec USD | 0 | *unconfirmed* |
| `balances.queue` | string | 18-dec USD | 0 | queue total, for the queue state in SPEC 5.2 |
| `activity.volume` | string | 18-dec USD | 0 | |
| `activity.open` | int | count | 0 | *unconfirmed*: open positions. Plain int, not 18-dec |
| `fees.pending` | string | 18-dec | 0 | *unconfirmed*; compare with `paper.pendingRewards` once non-zero |
| `fees.lifetime` | string | 18-dec | 0 | *unconfirmed* |
| `paper.supply` | string | 18-dec PAPER | 0 | |
| `paper.staked` | string | 18-dec PAPER | 0 | |
| `paper.trackedLp` | string | 18-dec USD | 0 | tracked LP, compared against `cliff` in SPEC 5.2 |
| `paper.accumulator` | string | 18-dec | 0 | |
| `paper.tailProgress` | string | 18-dec USD | 0 | |
| `paper.reserve` | string | 18-dec PAPER | 0 | *unconfirmed* |
| `paper.unallocated` | string | 18-dec PAPER | 0 | *unconfirmed* |
| `paper.pendingRewards` | string | 18-dec | 0 | |
| `paper.cliff` | string | 18-dec | 2,000,000 | matches SPEC (2M) |
| `paper.cap` | string | 18-dec | 5,000,000 | matches SPEC (5M) |
| `paper.excess` | string | 18-dec | 0 | *unconfirmed* |
| `paper.rate` | string | 18-dec PAPER per $ | 100 | matches SPEC (`rate` = 100) |
| `paper.decay` | string | 18-dec USD | 120,000,000 | matches SPEC (tailDecayScaleUsd = $120M) |

No `sideBucket` or queued-debt fields beyond `balances.queue`.

### `GET /query/protocol/history?interval=…`

Top-level keys: `startMs`, `intervalMs`, `columns`, `source`, `totals`.

| `interval` | `intervalMs` | Points | Range |
|---|---|---|---|
| `1m` | 60,000 | 1440 | the last 24 h, to the current minute |
| `1h` | 3,600,000 | 60 | from 2026-10-06 22:00 (the hour of genesis) to the current hour |
| `1d` | 86,400,000 | 4 | from 2026-10-06 00:00 to the current day |

- **Timestamps:** point `i` is labeled `startMs + i × intervalMs`, which is the
  start of its interval. Its value is the value at the interval's **close**.
  For example, the 1h point labeled 08:00 equals the 1m point at 08:59, and
  the 1d point labeled 10-08 equals the 1h point at 10-08 23:00. The last
  point is the open interval and holds the current value.
- **Columns:** `tvl`, `stakingRewards`, `volume`, `volumeBtc`, `volumeEth`,
  `traderPnl`, `users`, `liquidation`, `trades`, `paperSupply`,
  `paperStaked`, `paperRevenue`. All are parallel arrays of JSON numbers in
  normal units: `tvl` ≈ 14.83M, matching `summary` `balances.tvl` / 1e18.
  Values are floats (`14831637.903487999` against the exact
  `14831637.903488`), so they carry float rounding.
- **What the data shows per column:** `tvl` goes down at times in `1h` and
  `1m`, so it's a level. `users` never goes down (2 → 2260 in 24 h): either
  a cumulative count of users or a level that only grew. Every other column
  was all zeros, so it can't be classified yet, and `paperRevenue`'s meaning
  can't be read from the data.
- **`source`:** `{asOfMs, revision, generation}`. `asOfMs` is the time the
  data is current to (≈ the summary's `source.at`). `revision` (`"2772"`) and
  `generation` (`"0x…:34"`, a 32-byte hash plus an index) look like indexer
  versioning. *Unconfirmed*: they were the same across the three intervals
  in one fetch.
- **`totals`:** the same object for every interval. See below.

| `totals` key | Value at fetch | Matches |
|---|---|---|
| `tvlRaw` | `14831637903488000000000000` (18-dec) | last `tvl`, and `summary` `balances.tvl` |
| `rewardsRaw` | `0` | probably `stakingRewards`, *unconfirmed* |
| `volumeRaw` | `0` | `volume` |
| `liquidationRaw` | `0` | `liquidation` |
| `tradesRaw` | `0` | `trades` |
| `usersRaw` | `2260` (plain count, **not** 18-dec) | last `users` |
| `stakerFees24hRaw` | `0` | no column; a trailing-24 h figure |

### Quirks seen

- **No 503s.** 8 of 8 calls returned 200 on the first attempt (4 by curl, 4
  by `fetch-fixtures`, see `tests/fixtures/fetch_log.json`). The
  alternating-503 quirk in SPEC 2.3 didn't happen this time; the retry stays
  in place.

## Contradictions with SPEC

The original SPEC section 4 detection didn't work on these `totals`, so
section 4 was revised (2026-10-09) to classify columns by known levels and
monotonicity, and to use `totals` only as a cross-check:

1. **Keys don't match column names.** `totals` uses `tvlRaw`, `rewardsRaw`
   and so on, not `tvl`, `stakingRewards`. Rules 1–2 look up `totals[col]`,
   so they never fire without a mapping.
2. **Units are mixed.** `tvlRaw` is 18-decimal, `usersRaw` is a plain count.
   Each key needs its own scale before comparing.
3. **Most columns have no total.** There's none for `paperSupply`,
   `paperStaked`, `paperRevenue`, `traderPnl`, `volumeBtc` or `volumeEth`.
   These always fall through to rule 3.
4. **Rule 1 misclassifies levels.** `tvlRaw` equals the last `tvl`, so rule 1
   calls `tvl` cumulative, but `tvl` goes down and is a level. The totals
   look like current values, not cumulative sums, so "equals the last value"
   doesn't separate cumulative columns from levels.
5. **Zero series can't be classified.** An all-zero series matches rules 1,
   2 and "never decreases" at once.
