# Papertrade PAPER Tracker — Spec v2

Owner: Jonathan Low (Vortex) · Updated 2026-10-09

## 1. Goal

Track one live number: **is minting PAPER paying for itself right now?**

```
Live ratio   = staker rewards per staked PAPER per day ÷ cost to mint one PAPER
Payback days = 1 ÷ live ratio
```

The live ratio reads as "% of minting cost earned back per day". Show it hourly, daily, and as a 7-day average, next to the diagnostics in section 5 that explain why it moves. No projections and no time windows: live values only.

Phase 1 (this spec) polls Papertrade's public dashboard endpoints. Phase 2 cross-checks against the chain. Phase 3 (a separate spec) is the trading bot.

## 2. Data sources

Both endpoints are public, plain `GET`, no auth.

### 2.1 History: `GET https://exchange.papertrade.xyz/query/protocol/history?interval=1h`

- Top-level keys: `startMs`, `intervalMs`, `source`, `totals`, `columns`.
- `columns` is an object of parallel arrays, one value per interval:
  `tvl`, `stakingRewards`, `paperSupply`, `paperStaked`, `paperRevenue`, `volume`, `volumeBtc`, `volumeEth`, `traderPnl`, `users`, `trades`, `liquidation`.
- Timestamp of point `i` = `startMs + i × intervalMs`.
- Values are already in normal units (for example, TVL ≈ 14.76M).
- `interval`:
  - `1m` returns the last 24 hours, minute by minute.
  - `1h` returns the full history since genesis (60 points on 2026-10-09).
  - `1d` returns daily points.

### 2.2 Summary: `GET https://exchange.papertrade.xyz/query/protocol/summary`

- The current snapshot. Values are **raw 18-decimal integers**: divide by 1e18. Use Python `int`/`Decimal`, never float, before dividing.
- Has fields the history doesn't:
  - PAPER emission settings: `cliff` (2M), `cap` (5M), `rate`, `decay`
  - `pendingRewards`, `accumulator`, `tailProgress`
  - source block number and timestamp, used for the phase 2 cross-check
- Record **every** field it returns, not just these. If it exposes queue totals (queued debt, sideBucket, tracked LP), add them to the diagnostics.

### 2.3 Known quirks

- **Alternating 503s.** Calls to both endpoints alternate between 200 and 503, probably from one unhealthy server behind a load balancer. Retry on 503/5xx/timeouts: up to 6 attempts, 0.5 s → 8 s exponential backoff with jitter. Log the status of every attempt.
- **Zero series.** PAPER supply, staked and rewards were all 0 on 2026-10-09. Every metric must handle zero denominators and show `n/a — not started` rather than crash or print `inf`.
- **Field names and units are from a manual inspection.** Confirm them in step 1 of the build plan before writing any metric code.

## 3. Polling and storage

- **Hourly job:** fetch `summary` and `history?interval=1h`.
- **Daily job:** also fetch `history?interval=1d` and `history?interval=1m`, so there's minute-level detail for the last 24 h.
- **Store every raw response** before parsing (`raw_responses`: fetched_at, endpoint, http_status, attempt, body). This lets bugs be fixed and history re-parsed later.
- **Upsert by timestamp:** `history_1h(ts PRIMARY KEY, <one column per series>)`, and the same for `history_1d` and `history_1m`. The latest point can still be changing while its interval is open, so overwrite it on each poll and mark it `is_partial = 1` until the next interval starts.
- `summary_snapshots(fetched_at, source_block, source_ts, <every field, as text and as decimal>)`.
- `metrics(ts, granularity, <every metric in section 5>)`.
- SQLite is the default, in `data/papertracker.sqlite`.

## 4. Cumulative or per-interval? Detect it, don't assume

The charts appear cumulative, but each column could be either. `history.totals` can't decide it: its keys don't match the column names (`tvlRaw`, `rewardsRaw`, …), its units are mixed (18-decimal and plain counts), most columns have no total, and the totals hold current values rather than sums (`tvlRaw` equals the latest `tvl`). See the README's field reference (fixtures from 2026-10-09).

For each column, classify it from the `1h` series and store the result in `column_semantics`:

1. `tvl`, `users`, `paperSupply`, `paperStaked` → **level**.
2. Any other column that is all zeros → **unknown** (not classifiable yet).
3. Any other column that never decreases → **cumulative**.
4. Any other column that decreases → **per-interval**, and log a warning. (A cumulative series that can fall, such as `traderPnl`, would land here; the classification-change alert is what surfaces it.)

Use `totals` only as a cross-check, through an explicit key → column map (`tvlRaw` → `tvl`, `rewardsRaw` → `stakingRewards` (unconfirmed), `volumeRaw` → `volume`, `liquidationRaw` → `liquidation`, `tradesRaw` → `trades`, `usersRaw` → `users`), scaling each key by its own units. Compare the total with the latest value for a level or cumulative column, or with the sum of the series for a per-interval one, and warn if they differ by more than 0.1%. `stakerFees24hRaw` has no column; record it.

Then:

- per-interval flow for a **cumulative** column = `value[t] − value[t−1]`
- flow for a **per-interval** column = `value[t]`
- **levels** (`tvl`, `paperSupply`, `paperStaked`, `users`) are used as-is
- **unknown** columns have no flow yet; metrics that need one show `n/a — not started`

Re-check the detection every run and alert if a column's classification changes.

## 5. Metrics

Compute each hourly (`1h`), daily (rolling 24 h) and as a 7-day rolling average.

### 5.1 Headline

| Metric | Formula |
|---|---|
| Rewards flow | Flow of `stakingRewards` per hour / per 24 h |
| Staked supply | `paperStaked` (level). If it's 0 but supply isn't, fall back to `paperSupply` and flag it. |
| **Reward per staked PAPER per day** | rewards in the last 24 h ÷ average staked supply over those 24 h |
| **Cost per PAPER** | see 5.2 |
| **Live ratio** | reward per staked PAPER per day ÷ cost per PAPER, shown as %/day |
| **Payback days** | 1 ÷ live ratio |

### 5.2 Cost per PAPER

Two values, both shown:

1. **Marginal cost** (formula) = `(1 − k(m)) ÷ r`
2. **Measured cost** = USDC lost ÷ PAPER minted, read from `config.yaml` (manual entry for now; the bot fills it in phase 3). When it's set, the headline ratio uses it.

**k(m): share of the winning side's gain kept on a hedged pair closed at move m.** All parameters live in `config.yaml`; the defaults are Papertrade's launch values for BTC:

```
m'    = m − 0.00002                      # 0.2 bps deadband (entryPrice / 50,000)
term1 = 1 / (m' × rateMultiplier)        # rateMultiplier = 15,000
term2 = referenceNotional / (1e6 × m' × positionMultiplier)
                                         # referenceNotional = 100,000; positionMultiplier BTC = 814.598, ETH = 483.979
scale = (1 − baseRate) / (1 + term1 + term2)   # baseRate = 0.10
k     = scale × (m' / m) × (1 − winFee)        # winFee = 0.02
```

**r: PAPER minted per $1 of loss.** Use the `summary` emission settings:

```
if tracked LP < cliff:  r = rate                               # flat region, 100 at launch
else:                   r = rate × (decay / (decay + tailProgress))²
```

- Confirm from the summary values that `rate` = 100 and `decay` = $120M (Papertrade's docs list tailDecayScaleUsd = $120M).
- If tracked LP isn't in `summary`, use the flat rate while `tailProgress == 0`, and flag it.
- If the queue is empty, the 2% loss fee means minting on 98% of the loss: divide by `0.98 × r` instead. Default to queue active (`r`) unless `summary` shows the queue state.

`config.yaml` sets the target close move `m` (default 0.016) and the asset (default BTC).

### 5.3 Diagnostics (why the ratio moves)

| Metric | Formula | Read it as |
|---|---|---|
| PAPER minted per hour / 24 h | flow of `paperSupply` | supply growth |
| Dilution rate | minted in 24 h ÷ `paperSupply` | %/day your share shrinks if you stop minting |
| Rewards per new PAPER | rewards in 24 h ÷ minted in 24 h | whether new supply brings matching rewards |
| Net trader loss | −(flow of `traderPnl`) | what fills the queue, then the LP, then reaches stakers |
| Liquidations | flow of `liquidation` | nearly pure LP gain at high leverage |
| Volume, BTC/ETH split | flow of `volume`, `volumeBtc`, `volumeEth` | activity |
| Net loss per $ volume | net trader loss ÷ volume | how much activity leaves money behind |
| Users, trades | `users` level, flow of `trades` | breadth |
| TVL | `tvl` | capital in the protocol |
| Protocol revenue | flow of `paperRevenue` | confirm what it measures against `stakingRewards` in step 1 |
| Pending rewards | `pendingRewards` (summary) | distributed but not yet claimed |
| Mint rate r | 5.2 | rises → cheaper PAPER; falls only past the cliff |

Always read the live ratio next to the dilution rate. A 5%/day ratio with 10%/day dilution means your share is shrinking faster than it pays.

## 6. Outputs

1. **CLI:** `python -m papertracker status` prints the latest headline and diagnostics as a table: hourly, 24 h and 7-day columns, with the data age.
2. **Static dashboard:** `python -m papertracker build-dashboard` writes `site/index.html` (one self-contained file, charts inline):
   - headline tiles: live ratio, payback days, cost per PAPER, reward per staked PAPER per day
   - time series: rewards flow, PAPER minted, dilution rate, live ratio
   - diagnostics table
   - data-freshness and 503-rate footer
3. **Alerts (optional, Telegram via `.env`):**
   - rewards flow turns non-zero for the first time
   - the live ratio crosses a configurable threshold either way
   - any emission setting in `summary` changes
   - a column's detected type (section 4) changes
   - no successful poll for 3 hours

## 7. Repository layout

```
papertracker/
  SPEC.md
  CLAUDE.md
  README.md
  config.yaml            # m, asset, impact params, alert thresholds, measured cost
  .env.example           # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  .gitignore             # .env, data/*.sqlite, site/
  papertracker/
    client.py            # HTTP + retry
    store.py             # SQLite schema + upserts
    semantics.py         # cumulative / per-interval detection
    economics.py         # k(m), r, cost per PAPER
    metrics.py           # section 5
    dashboard.py
    alerts.py
    __main__.py          # CLI: poll, status, build-dashboard
  tests/
    fixtures/            # real saved responses from step 1
  .github/workflows/poll.yml   # optional hourly schedule
```

Python 3.11+, `httpx`, `pyyaml`, standard-library `sqlite3` and `decimal`. Keep dependencies minimal.

## 8. Build plan (in order; test each step before the next)

1. **Inspect and save fixtures.** Fetch each endpoint (`summary`, `history` at `1m`/`1h`/`1d`) with the retry client and save the raw JSON to `tests/fixtures/`. Write down in `README.md`:
   - every field and its units
   - whether `totals` is present per column
   - what `paperRevenue` and `source` mean, as far as the data shows

   Stop and report if anything contradicts section 2.
2. **Client:** retry logic, with tests that simulate alternating 200/503.
3. **Store:** schema and upserts, including partial-interval overwrite. Re-running a poll must not duplicate rows.
4. **Semantics detection:** tests on synthetic cumulative, per-interval and level series, plus the real fixtures.
5. **Economics:** `k(m)` and `r`, with the tests in section 9.
6. **Metrics:** with zero-safe handling.
7. **CLI, dashboard, alerts.**
8. **Scheduling.** Use local cron (`0 * * * *`), or the GitHub Actions workflow (note that Actions schedules can run late, and the repo must stay private if it ever holds keys). If using Actions, commit only CSV exports of `history_1h` and `metrics`, not the SQLite file.

## 9. Tests that must pass

| Test | Expected |
|---|---|
| k(0.01), BTC | 0.8638 ± 0.0005 (cost 13.6% of the loss) |
| k(0.001), BTC, before fee | scale × m′/m ≈ 0.739, matching the docs' "a 0.1% move keeps ~74%" |
| k(0.016), BTC | ≈ 0.871 → marginal cost at r = 100 ≈ $0.00129 |
| k(0.0107), BTC | 0.8650 → 50×, $50K per side: cost per round $3,611, PAPER 2,675,000 |
| r with tailProgress = $100M, decay = $120M, rate = 100 | 29.75 |
| Any metric with 0 rewards or 0 supply | `n/a`, no exception |
| 6 alternating 200/503 calls | every call ends in a 200 within 2 attempts |
| Re-polling the same hour twice | one row |

## 10. Phase 2: on-chain cross-check (later)

- Once Papertrade publishes the contract addresses, or they're found in the app bundle, read PaperToken `totalSupply`, PaperStaking total staked, and the Exchange queue totals at the `source_block` from `summary`.
- Alert if they differ from the endpoint by more than 0.5%.
- Add queue size and days-until-queue-clears to the diagnostics.

## 11. Done when

- [ ] Fixtures saved, and field semantics documented in README
- [ ] Hourly polling runs unattended for 48 h with no gaps, and the 503 rate is logged
- [ ] `status` shows every section 5 metric; zeros display as `n/a — not started`
- [ ] Dashboard builds into one self-contained HTML file
- [ ] All section 9 tests pass
- [ ] An alert fires on a simulated change to an emission setting
