# NINDSS Tracker

Weekly-updated case numbers for **COVID-19, Influenza, RSV and Measles** in
Australia, scraped from the [National Notifiable Diseases Surveillance
System (NINDSS) public dashboard](https://nindss.health.gov.au/pbi-dashboard/)
and committed to this repo automatically every week via GitHub Actions.

No login, API key, or manual download required to get the data - just clone
or pull this repo, or browse the CSVs directly on GitHub.

## Data files

Data and CSV history live in [`data/`](./data), charts live in
[`graphs/`](./graphs), both updated weekly by
[`.github/workflows/weekly-update.yml`](./.github/workflows/weekly-update.yml):

| File | What it is |
|---|---|
| `data/weekly_snapshots.csv` | The run date and a year's cumulative notification count per disease/state, as of that run. The current year gets a row every run; past years only get a new row when their number actually changed (see below). This is the source of truth for "new cases this week". |
| `data/annual_totals.csv` | Full-history annual totals per disease per state (confirmed + probable notifications). Overwritten each run. |
| `graphs/weekly_new_cases_national.png` | Chart of new notifications since the previous run, per disease, national. |
| `graphs/weekly_new_cases_by_state.png` | Same, broken out per state/territory. |
| `graphs/weekly_new_cases_national.html` / `weekly_new_cases_by_state.html` | Interactive versions of the two charts above - hover a point for its exact date and value. Open in a browser (needs internet access once to load Plotly's JS from a CDN). |

Both PNG and HTML versions also come in per-year variants, e.g.
`weekly_new_cases_national_2026.png` / `.html`.

### How "new cases this week" is calculated

The dashboard itself only exposes cumulative annual totals, not a weekly
breakdown. So each week this tool checks every year's running total per
disease/state (not just the current year - that's what catches
late-reported/backdated corrections to past years), and the "new cases"
number is just this week's snapshot minus the last recorded snapshot for
the same disease, state, and year. It's exact - not an estimate - as long
as the workflow runs every week without a gap.

The current year gets a row in `weekly_snapshots.csv` every single run,
since that's the series you actually watch week to week. A past
(already-completed) year only gets a new row when its number actually
changed - i.e. a correction landed - otherwise nothing is written for it
that week. Without this, the file would grow by roughly
(diseases × states × every tracked year back to 1991) rows on *every*
run forever, nearly all of them just restating a number that hasn't moved.

If a run is missed, a skipped week or two just means the next delta covers
a slightly longer period - still a real number, and a quiet past year
naturally going a couple of months between real rows is expected, not a
problem. But if a disease/state/year combination goes more than 120 days
without a snapshot (e.g. the workflow breaks for over a month, or - as
happened before this tool tracked every year each week - a year simply
wasn't being checked yet), that gap is treated as a fresh start rather than
reported as one artificially huge "new cases" figure covering the whole
gap.

Occasionally the source data itself is revised **downward** (health.gov.au
correcting an earlier count) - these show up as red-ringed points on the
charts rather than a negative "new cases" number.

## Running it yourself

```bash
pip install -r requirements.txt
playwright install chromium
python nindss_tracker.py --data-dir data --graphs-dir graphs
```

`playwright install chromium` is a one-time step that downloads an actual
Chromium binary - the `playwright` pip package alone isn't enough to run
it, since the fetch itself runs inside a real headless browser (see
"How it works" below).

Re-running it repeatedly (e.g. via your own cron job instead of forking
this repo) will keep appending to `weekly_snapshots.csv` in whatever
`--data-dir` you point it at.

## Using your own copy on GitHub

1. Fork or use this repo as a template.
2. GitHub Actions should just work once you push - it's scheduled for
   Wednesday 04:00 UTC or 2pm AEST (or 3pm during daylight savings), with a manual "Run workflow" button too (Actions tab →
   "Weekly NINDSS data update" → "Run workflow").
3. Make sure Actions has write permission: **Settings → Actions → General →
   Workflow permissions → "Read and write permissions"** (this is required
   for the workflow to commit the updated data back to the repo).

## How it works, and its limitations

The NINDSS dashboard is a Power BI report with no published API. The
underlying endpoints are behind bot-protection that blocks plain HTTP
clients even when their headers and TLS fingerprint are made to match a
real browser exactly - so instead of imitating a browser, this script
drives an actual one: it loads the real dashboard page with Playwright,
finds the Power BI iframe the page embeds, and runs the same `fetch()`
calls a real session would from inside that iframe's own JS context. That
gives it the real session's cookies, bearer token, and whatever
bot-challenge state the page picked up - rather than trying to fake any of
it. It then decodes Power BI's compact response format ("DSR") to extract
confirmed + probable notification counts per disease/state/year.

This is inherently fragile: it's an undocumented API that could change
without notice. If a scheduled run fails, the workflow automatically opens
a GitHub issue on this repo so it doesn't fail silently - see the top of
[`nindss_tracker.py`](./nindss_tracker.py) for how to diagnose and fix it
(you'll generally need to re-capture a HAR file from the dashboard and
compare the query shape).

Numbers include both "Confirmed" and "Probable" notifications, matching
what the public dashboard itself displays. A small number of historical
values may show as suppressed (`<5`) on the dashboard for privacy reasons;
these are approximated as `3` in the data here.

## License

MIT - see [LICENSE](./LICENSE). Underlying data belongs to the Australian
Government Department of Health, Disability and Ageing / Australian Centre
for Disease Control via the NINDSS dashboard; this repo just automates
collecting the publicly displayed figures.
