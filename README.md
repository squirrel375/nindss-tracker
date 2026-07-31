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
| `data/weekly_snapshots.csv` | One row per disease per state per run: the run date and that year's cumulative notification count at the time. This is the source of truth for "new cases this week" (see below). |
| `data/annual_totals.csv` | Full-history annual totals per disease per state (confirmed + probable notifications). Overwritten each run. |
| `graphs/weekly_new_cases_national.png` | Chart of new notifications since the previous run, per disease, national. |
| `graphs/weekly_new_cases_by_state.png` | Same, broken out per state/territory. |

### How "new cases this week" is calculated

The dashboard itself only exposes cumulative annual totals, not a weekly
breakdown. So each week this tool takes a snapshot of the current year's
running total per disease, and the "new cases" number is just this week's
snapshot minus last week's snapshot for the same disease and year. It's
exact - not an estimate - as long as the workflow runs every week without
a gap.

## Running it yourself

```bash
pip install -r requirements.txt
python nindss_tracker.py --data-dir data --graphs-dir graphs
```

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

The NINDSS dashboard is a Power BI report with no published API - this
script talks to the same internal Microsoft endpoints the dashboard's own
web page uses, decodes Power BI's compact response format, and extracts
national confirmed + probable notification counts.

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
