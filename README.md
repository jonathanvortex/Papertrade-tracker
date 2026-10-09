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
| 1. Inspect and save fixtures | **Blocked**: no fixtures yet. `fetch-fixtures` is ready; it needs network access to `exchange.papertrade.xyz`. |
| 2. Client with retry | Done: `papertracker/client.py`, `tests/test_client.py` |
| 3. Store | Not started; its schema depends on the field names from step 1 |
| 4. Semantics detection | Not started; needs the real fixtures |
| 5. Economics | Done: `papertracker/economics.py`, `tests/test_economics.py` (section 9 tests pass) |
| 6–8 | Not started |

Step 5 was built ahead of steps 3–4 because it's pure formula and doesn't
depend on the endpoint data. Its mint rate `r` takes `rate`, `decay`,
`tailProgress`, tracked LP and `cliff` as arguments. Mapping those to
`summary` fields waits on step 1.

## Endpoint field reference

To be filled in from the step 1 fixtures. For each endpoint it records:
every field and its units, whether `totals` has a value per column, and
what `paperRevenue` and `source` mean, as far as the data shows.
