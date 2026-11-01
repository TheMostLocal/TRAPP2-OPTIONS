# TRAPP2-OPTIONS

Options chains for Valuatio, in their own repo, so 150+ tickers refreshed several times a day don't bloat the TRAPP2 equity book.

## Layout
| Path | What it holds |
|---|---|
| `pipeline/fetch_options.py` | yfinance chains + Black-Scholes Greeks (stdlib) |
| `data/options/<TICKER>.json` | One chain per ticker, same format as before. Near weeklies, monthlies and LEAPS; strikes ±25%. |
| `data/options/manifest.json` | Tickers covered |
| `data/options/summary.json` | Per ticker: spot, ATM IV, expected move, put/call OI, max pain |
| `data/options_universe.txt` | Core list, always refreshed |
| `data/requests.txt` | Tickers requested from the app; they stay covered |

## Universe
| Tier | When | Tickers |
|---|---|---|
| core | 10:00 and 13:00 ET, Mon–Fri | Seed list + requests + portfolio holdings + open bot positions |
| broad | 16:30 ET, Mon–Fri | Core + the 150 largest US equities/ETFs in the TRAPP2 / -2 / -3 books |
| dispatch | On demand | `tickers` input. The app's **Request options** button does this; the tickers are also appended to `requests.txt`. |

## Setup (once)
1. Create `TheMostLocal/TRAPP2-OPTIONS` and push these files.
2. Settings → Actions → General → Workflow permissions → **Read and write**.
3. Actions → **Options Chains** → Run workflow (tier `broad`) to fill it.
4. The app's GitHub token needs **Actions: Read and write** on this repo for the Request button (Contents is not needed; the workflow commits).

## History size
`compact.yml` squashes the repo to one commit on the 1st of each month. That is safe only because this repo has a single writer, and both workflows share the `options-data` concurrency group.
