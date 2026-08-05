#!/usr/bin/env python3
"""
nindss_tracker.py

Weekly tracker for new case numbers of COVID-19, Influenza, RSV and Measles
from the Australian National Notifiable Diseases Surveillance System (NINDSS)
public Power BI dashboard:

    https://nindss.health.gov.au/pbi-dashboard/

HOW IT WORKS
------------
The dashboard is a Power BI report published "for customers" (i.e. it's
technically an anonymous/public embed - there is no login involved). The
browser talks to two Microsoft services to get data:

  1. A "bootstrap" call (modelsAndExploration) that tells the browser which
     Power BI model/dataset backs the report, and which capacity node
     (a specific *.pbidedicated.windows.net host) is currently serving it.
  2. A QueryExecutionService call to that capacity node, which runs the
     actual DAX-ish query and returns data in Power BI's compact "DSR"
     format (values are run-length encoded).

This script replicates both calls using the `requests` library, decodes the
DSR response, and extracts national (all states/territories) confirmed +
probable notification counts per disease per calendar year.

Because the dashboard does not expose a daily/weekly breakdown directly, we
get "new cases this week" by keeping our own running history: each time you
run the script it stores a snapshot of the current year's running total, and
compares it to last week's snapshot to work out how many new notifications
have appeared since then. Weekly is just the cadence you choose to run
the script at (e.g. via cron) - see the bottom of this file for scheduling
notes.

OUTPUT FILES:
  In --data-dir (default "./data"):
  - annual_totals.csv     Full-history annual totals per disease per state
                           (overwritten every run - a convenience export).
  - weekly_snapshots.csv  Cumulative-count snapshot per disease/state/run
                           (appended every run - this is the source of truth
                           for the "new cases" numbers).

  In --graphs-dir (default "./graphs"):
  - weekly_new_cases_national.png   Line chart of new cases per week, national.
  - weekly_new_cases_by_state.png   Same, broken out per state/territory.

CAVEATS
-------
- This relies on an undocumented, internal Power BI API that Microsoft/health.gov.au
  could change at any time without notice. If it stops working, re-capture a
  HAR file from https://nindss.health.gov.au/pbi-dashboard/ and update
  REPORT_ID below (and check DISEASE_NAMES still match what's in the "Where"
  filters of the QueryExecutionService requests).
- "New cases this week" is only as good as your run cadence - if you skip a
  week, the delta just covers a longer period; the script does not try to
  guess actual daily case dates.
- Counts include both "Confirmed" and "Probable" notifications, matching what
  the public dashboard displays.
"""

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import uuid

# curl_cffi, not the plain `requests` library: identical HTTP headers were
# confirmed (via a real browser's HAR capture) to NOT be enough to avoid a
# 403 here as of Aug 2026 - the remaining difference is TLS fingerprint
# (JA3/JA4) and HTTP/2 handshake behaviour, which `requests`' stock TLS
# stack can't replicate no matter what headers you set. curl_cffi wraps
# curl-impersonate to send an actual Chrome-shaped TLS ClientHello. Its
# .get()/.post()/.raise_for_status() API matches `requests` closely enough
# to be a near drop-in swap. If dashboard requests start failing again,
# suspect this same TLS-fingerprinting layer before anything else.
from curl_cffi import requests

# ---------------------------------------------------------------------------
# Configuration - update these if the dashboard changes (see CAVEATS above)
# ---------------------------------------------------------------------------

REPORT_ID = "bc027587-5e9e-4920-bf03-a45fd3079f25"

MODELS_URL = (
    f"https://wabi-australia-southeast-redirect.analysis.windows.net/explore/"
    f"reports/{REPORT_ID}/modelsAndExploration"
    f"?preferReadOnlySession=true&skipQueryData=true"
)

# Exact "DISEASE NAME" values as used by the dashboard's own filters.
DISEASE_NAMES = {
    "COVID-19": "COVID-19",
    "Influenza": "Influenza (laboratory confirmed)",
    "RSV": "Respiratory syncytial virus (RSV)",
    "Measles": "Measles",
}

COMMON_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://app.powerbi.com",
    "referer": "https://app.powerbi.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    # As of Aug 2026 the dashboard started returning 403 Forbidden without
    # these. They're headers a real browser sends automatically that the
    # `requests` library never adds on its own - Microsoft's edge/WAF layer
    # for Power BI appears to now check for them as a basic bot-detection
    # signal. If this script starts failing again, re-capture a HAR (see
    # CAVEATS above) and diff its headers against this list first, since
    # that's the most likely thing to have changed again.
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "dnt": "1",
    "sec-gpc": "1",
    "priority": "u=1, i",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
}

REQUEST_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Step 1: bootstrap - find current modelId / datasetId / capacity URL
# ---------------------------------------------------------------------------

def bootstrap_session():
    headers = dict(COMMON_HEADERS)
    headers["x-powerbi-hostenv"] = "Embed for Customers"
    headers["activityid"] = str(uuid.uuid4())
    headers["requestid"] = str(uuid.uuid4())

    resp = requests.get(MODELS_URL, headers=headers, timeout=REQUEST_TIMEOUT, impersonate="chrome")
    if not resp.ok:
        print(f"  [debug] HTTP {resp.status_code} response headers: {dict(resp.headers)}", file=sys.stderr)
        print(f"  [debug] response body (first 500 chars): {resp.text[:500]}", file=sys.stderr)
    resp.raise_for_status()
    data = resp.json()

    models = data.get("models")
    if not models:
        raise RuntimeError(
            "Couldn't find 'models' in modelsAndExploration response - the "
            "dashboard's API may have changed. See CAVEATS in this script."
        )

    model = models[0]
    model_id = model["id"]
    dataset_id = model["dbName"]

    capacity_uri = model.get("capacityUri")
    if not capacity_uri:
        raise RuntimeError(
            "Couldn't find 'capacityUri' in modelsAndExploration response."
        )
    # capacityUri already ends in '.../public/' - the query endpoint is
    # that plus 'query'.
    query_url = capacity_uri.rstrip("/") + "/query"

    return {
        "model_id": model_id,
        "dataset_id": dataset_id,
        "query_url": query_url,
    }


# ---------------------------------------------------------------------------
# Step 2: build & run a query for one disease -> {year: national_total}
# ---------------------------------------------------------------------------

def build_query_body(disease_name, model_id, dataset_id, report_id):
    visual_id = uuid.uuid4().hex[:20]
    query = {
        "version": "1.0.0",
        "queries": [
            {
                "Query": {
                    "Commands": [
                        {
                            "SemanticQueryDataShapeCommand": {
                                "Query": {
                                    "Version": 2,
                                    "From": [
                                        {"Name": "d", "Entity": "DELTALOAD_DATAMART NOTIFIABLE_EVENT_FACT", "Type": 0},
                                        {"Name": "d1", "Entity": "DELTALOAD_DATAMART LOCATION_DIM", "Type": 0},
                                        {"Name": "d11", "Entity": "DELTALOAD_DATAMART DISEASE_DIM", "Type": 0},
                                        {"Name": "d3", "Entity": "DELTALOAD_DATAMART CASE_DIM", "Type": 0},
                                    ],
                                    "Select": [
                                        {
                                            "Column": {"Expression": {"SourceRef": {"Source": "d"}}, "Property": "DAX_Year"},
                                            "Name": "DELTALOAD_DATAMART NOTIFIABLE_EVENT_FACT.DAX_Year",
                                        },
                                        {
                                            "Column": {"Expression": {"SourceRef": {"Source": "d1"}}, "Property": "STATE"},
                                            "Name": "DELTALOAD_DATAMART LOCATION_DIM.STATE",
                                        },
                                        {
                                            "Measure": {"Expression": {"SourceRef": {"Source": "d"}}, "Property": "Count_Notification"},
                                            "Name": "DELTALOAD_DATAMART NOTIFIABLE_EVENT_FACT.M_Notification",
                                        },
                                    ],
                                    "Where": [
                                        {"Condition": {"Not": {"Expression": {"In": {"Expressions": [
                                            {"Column": {"Expression": {"SourceRef": {"Source": "d1"}}, "Property": "STATE"}}
                                        ], "Values": [[{"Literal": {"Value": "'AUS'"}}], [{"Literal": {"Value": "'Unknown'"}}]]}}}}},
                                        {"Condition": {"In": {"Expressions": [
                                            {"Column": {"Expression": {"SourceRef": {"Source": "d11"}}, "Property": "DISEASE NAME"}}
                                        ], "Values": [[{"Literal": {"Value": "'%s'" % disease_name}}]]}}},
                                        {"Condition": {"Comparison": {
                                            "ComparisonKind": 1,
                                            "Left": {"Column": {"Expression": {"SourceRef": {"Source": "d"}}, "Property": "DAX_Year"}},
                                            "Right": {"Literal": {"Value": "1990L"}},
                                        }}},
                                        {"Condition": {"Not": {"Expression": {"In": {"Expressions": [
                                            {"Column": {"Expression": {"SourceRef": {"Source": "d11"}}, "Property": "DISEASE GROUP"}}
                                        ], "Values": [[{"Literal": {"Value": "'Unknown'"}}], [{"Literal": {"Value": "null"}}]]}}}}},
                                        {"Condition": {"Not": {"Expression": {"In": {"Expressions": [
                                            {"Column": {"Expression": {"SourceRef": {"Source": "d3"}}, "Property": "Age Group"}}
                                        ], "Values": [[{"Literal": {"Value": "null"}}]]}}}}},
                                        {"Condition": {"In": {"Expressions": [
                                            {"Column": {"Expression": {"SourceRef": {"Source": "d3"}}, "Property": "CONFIRMATION_STATUS"}}
                                        ], "Values": [[{"Literal": {"Value": "'Confirmed'"}}], [{"Literal": {"Value": "'Probable'"}}]]}}},
                                    ],
                                },
                                "Binding": {
                                    # Primary axis = Year (Select index 0), Secondary axis = State (index 1)
                                    # with the measure (index 2) nested under it. This is the reverse of
                                    # how the dashboard's own table visual groups it (State-primary), but
                                    # produces the same underlying data - just organised by year, which is
                                    # what we want to sum nationally per year.
                                    "Primary": {"Groupings": [{"Projections": [0]}]},
                                    "Secondary": {"Groupings": [{"Projections": [1, 2]}]},
                                    "DataReduction": {
                                        "DataVolume": 3,
                                        "Primary": {"Window": {"Count": 100}},
                                        "Secondary": {"Top": {"Count": 100}},
                                    },
                                    "Version": 1,
                                },
                                "ExecutionMetricsKind": 1,
                            }
                        }
                    ]
                },
                "QueryId": "",
                "ApplicationContext": {
                    "DatasetId": dataset_id,
                    "Sources": [{"ReportId": report_id, "VisualId": visual_id}],
                },
            }
        ],
        "cancelQueries": [],
        "modelId": model_id,
        "userPreferredLocale": "en-US",
        "allowLongRunningQueries": True,
    }
    return query


def _parse_dsr_number(value):
    """
    Power BI DSR numeric values are sometimes plain JSON numbers (0, 1, 2...)
    and sometimes typed literal strings like "0L" (Int64) or "12.5D" (Double)
    the first time a given type appears in a column. Handle both.
    """
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        s = value
        if s and s[-1] in "LDMldm":
            s = s[:-1]
        try:
            return float(s) if "." in s else int(s)
        except ValueError:
            return 0
    return 0


def _lookup_dict_value(dict_list, index):
    """
    Resolve one entry from a Power BI ValueDicts list, e.g. "'3,363,646'" or
    "'<5'" (privacy-suppressed small counts, shown as '<5' on the dashboard
    itself). We treat '<5' as 3 (the midpoint of 0-4) since the true value
    isn't published - this only affects a handful of very small/old rows.
    """
    try:
        raw = dict_list[index]
    except (IndexError, TypeError):
        return 0
    s = str(raw).strip()
    if s.startswith("'") and s.endswith("'"):
        s = s[1:-1]
    s = s.replace(",", "")
    if s in ("<5", "< 5"):
        return 3
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return 0


def decode_dsr_by_state(dsr):
    """
    Decode a Power BI 'DSR' (DataShapeResult) payload for a query shaped like
    ours: Primary grouping = Year, Secondary grouping = [State, Measure].
    Returns {year: {state: value}} - i.e. keeps the state breakdown instead
    of summing it away.

    Three compression tricks are handled:
      - Repeated values on the secondary (state) axis are shown as {"R": n}
        ("repeat the last real value for the next n columns") instead of
        restating it - we expand these back out positionally.
      - Larger/more varied measure values are dictionary-encoded: the first
        appearance carries an "S"/"DN" descriptor naming a dictionary (in
        dsr.DS[i].ValueDicts), and later same-column values are plain integer
        *indices* into that dictionary rather than the values themselves.
      - The actual state names/order for the secondary axis live in
        dsr.DS[i].SH (schema hierarchy), not in the data rows themselves -
        column position N in a row's X array corresponds to the Nth entry
        there.
    """
    result = {}
    for ds in dsr.get("DS", []):
        value_dicts = ds.get("ValueDicts") or {}

        # Secondary (state) category order, e.g. ['ACT','NSW','NT',...]
        state_order = []
        sh = ds.get("SH") or []
        if sh:
            dm_key = next(iter(sh[0]), None)
            if dm_key:
                state_order = [entry.get("G1") for entry in sh[0][dm_key]]

        dict_name = None
        for ph_row in ds.get("PH", []):
            for row in ph_row.get("DM0", []):
                year = row.get("G0")
                if year is None:
                    continue
                year_result = result.setdefault(year, {})
                last_value = 0
                col = 0
                for item in row.get("X", []):
                    if "S" in item:
                        for s_desc in item["S"]:
                            if s_desc.get("N") == "M0" and "DN" in s_desc:
                                dict_name = s_desc["DN"]
                    if "M0" in item:
                        raw = item["M0"]
                        if isinstance(raw, str):
                            value = _parse_dsr_number(raw)
                        elif dict_name is not None and dict_name in value_dicts:
                            value = _lookup_dict_value(value_dicts[dict_name], raw)
                        else:
                            value = raw or 0
                        last_value = value
                        state = state_order[col] if col < len(state_order) else f"col{col}"
                        year_result[state] = year_result.get(state, 0) + value
                        col += 1
                    elif "R" in item:
                        for _ in range(item["R"]):
                            state = state_order[col] if col < len(state_order) else f"col{col}"
                            year_result[state] = year_result.get(state, 0) + last_value
                            col += 1
                    # items with neither key (rare) are ignored/treated as 0
    return result


def national_totals_from_state_totals(year_state_totals):
    """{year: {state: value}} -> {year: total} summed across all states."""
    return {
        year: sum(state_totals.values())
        for year, state_totals in year_state_totals.items()
    }


def fetch_disease_year_state_totals(session_info, disease_name, report_id, max_retries=3):
    """Returns {year: {state: cumulative_notifications}} for one disease."""
    headers = dict(COMMON_HEADERS)
    headers["content-type"] = "application/json;charset=UTF-8"
    headers["activityid"] = str(uuid.uuid4())
    headers["requestid"] = str(uuid.uuid4())

    body = build_query_body(
        disease_name, session_info["model_id"], session_info["dataset_id"], report_id
    )

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                session_info["query_url"], headers=headers, json=body,
                timeout=REQUEST_TIMEOUT, impersonate="chrome",
            )
            if not resp.ok:
                print(f"  [debug] HTTP {resp.status_code} response headers: {dict(resp.headers)}", file=sys.stderr)
                print(f"  [debug] response body (first 500 chars): {resp.text[:500]}", file=sys.stderr)
            resp.raise_for_status()
            data = resp.json()
            dsr = data["results"][0]["result"]["data"]["dsr"]
            return decode_dsr_by_state(dsr)
        except Exception as exc:  # noqa: BLE001 - we want to retry on anything and report it
            last_error = exc
            time.sleep(2 * attempt)
    raise RuntimeError(
        f"Failed to fetch data for '{disease_name}' after {max_retries} attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# Step 3: persist snapshots + history, compute new-cases-this-week
# ---------------------------------------------------------------------------

def load_weekly_snapshots(path):
    rows = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append(r)
    return rows


def save_annual_totals_csv(path, all_state_totals):
    """
    all_state_totals: {disease: {year: {state: total}}}
    Writes one row per disease per year per state, PLUS a synthetic
    state="National" row per disease/year summing all states.
    """
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["disease", "state", "year", "total_notifications"])
        for disease, year_state_totals in all_state_totals.items():
            for year in sorted(year_state_totals):
                state_totals = year_state_totals[year]
                writer.writerow([disease, "National", year, sum(state_totals.values())])
                for state in sorted(state_totals):
                    writer.writerow([disease, state, year, state_totals[state]])


def append_weekly_snapshot(path, run_date, all_state_totals):
    """
    all_state_totals: {disease: {year: {state: total}}}
    Appends one row per disease PER YEAR PER STATE (not just the current
    year, and not just the national total) - plus a synthetic
    state="National" row summing all states. Snapshotting every year every
    week means each year's series is self-contained (never compared against
    a different year's total at a year rollover) and still catches late-
    reported/backdated corrections that show up weeks after the fact.
    """
    file_exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["run_date", "disease", "state", "year", "cumulative_notifications"])
        for disease, year_state_totals in all_state_totals.items():
            for year, state_totals in sorted(year_state_totals.items()):
                writer.writerow([run_date, disease, "National", year, sum(state_totals.values())])
                for state, cumulative in sorted(state_totals.items()):
                    writer.writerow([run_date, disease, state, year, cumulative])


def compute_new_cases(snapshot_rows):
    """
    Given all historical rows from weekly_snapshots.csv (list of dicts),
    compute new cases between consecutive runs for the same disease+state+year.
    Returns list of dicts: run_date, disease, state, year, cumulative,
    new_cases, anomaly (bool).

    Because comparisons only ever happen within the same disease+state+year
    (never across a year boundary), a year rollover can never produce a
    nonsense negative "new cases" number by comparing e.g. December's high
    cumulative total to January's low one - each year's counter starts its
    own series with new_cases=None on its first sighting. The only way
    you'd see a negative number is a genuine data correction (health.gov.au
    revising a count downward) within the same year - those are flagged as
    anomalies rather than silently reported as a negative "new case" count.

    Rows from before the 'state' column existed are treated as state="National"
    for backwards compatibility with older weekly_snapshots.csv files.
    """
    by_group = {}
    for row in snapshot_rows:
        state = row.get("state") or "National"
        key = (row["disease"], state)
        by_group.setdefault(key, []).append(row)

    results = []
    for (disease, state), rows in by_group.items():
        # Sort by (year, run_date) - not just run_date - so each year's own
        # chronology is correct regardless of row order in the file (e.g.
        # imported historical data listed out of order).
        rows_sorted = sorted(rows, key=lambda r: (r["year"], r["run_date"]))
        prev_by_year = {}
        for row in rows_sorted:
            year = row["year"]
            cumulative = int(row["cumulative_notifications"])
            prev = prev_by_year.get(year)
            anomaly = False
            if prev is None:
                new_cases = None
            else:
                new_cases = cumulative - prev
                if new_cases < 0:
                    anomaly = True
            results.append(
                {
                    "run_date": row["run_date"],
                    "disease": disease,
                    "state": state,
                    "year": year,
                    "cumulative": cumulative,
                    "new_cases": new_cases,
                    "anomaly": anomaly,
                }
            )
            prev_by_year[year] = cumulative
    return results


# ---------------------------------------------------------------------------
# Step 4: plotting
# ---------------------------------------------------------------------------

def _plot_new_cases_on_ax(ax, results, state, diseases, year=None):
    """Draw the weekly-new-cases lines for one state onto one axis.
    If year is given (e.g. '2025'), only that calendar year is plotted."""
    import matplotlib.dates as mdates

    chartable = [r for r in results if r["state"] == state and (year is None or r["year"] == year)]

    any_plotted = False
    any_anomaly = False
    all_dates = []
    for disease in diseases:
        points = [r for r in chartable if r["disease"] == disease and r["new_cases"] is not None]
        points.sort(key=lambda r: (r["year"], r["run_date"]))
        if not points:
            continue
        # Plot the full line through every point, including anomalies, so a
        # genuine reported decrease actually shows as a dip rather than a
        # gap in the line. Dates are parsed to real datetime objects (not
        # left as strings) so the x-axis can use a proper monthly tick
        # locator instead of labeling every single week.
        xs = [dt.datetime.strptime(r["run_date"], "%Y-%m-%d") for r in points]
        ys = [r["new_cases"] for r in points]
        ax.plot(xs, ys, marker="o", markersize=3, label=disease)
        any_plotted = True
        all_dates.extend(xs)
        anomalies = [r for r in points if r["anomaly"]]
        if anomalies:
            any_anomaly = True
            ax.scatter(
                [dt.datetime.strptime(r["run_date"], "%Y-%m-%d") for r in anomalies],
                [r["new_cases"] for r in anomalies],
                marker="o", s=90, facecolors="none", edgecolors="red",
                linewidths=1.8, zorder=5,
            )

    if any_plotted:
        # Full history can span well over a year - space tick labels out
        # further (every 2nd/3rd month) once there's enough range that
        # monthly labels would start overlapping.
        span_months = (max(all_dates).year - min(all_dates).year) * 12 + (max(all_dates).month - min(all_dates).month)
        interval = 1 if span_months <= 14 else (2 if span_months <= 26 else 3)
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=interval))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    else:
        ax.text(
            0.5, 0.5,
            "Not enough history yet",
            ha="center", va="center", transform=ax.transAxes, wrap=True, fontsize=9,
        )
    return any_plotted, any_anomaly


def plot_weekly_new_cases(results, out_path, state="National", year=None):
    """Single chart of weekly new cases for one state (default: National).
    If year is given, only that calendar year is shown."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    diseases = sorted(set(r["disease"] for r in results if r["state"] == state))
    fig, ax = plt.subplots(figsize=(10, 6.5))
    any_plotted, any_anomaly = _plot_new_cases_on_ax(ax, results, state, diseases, year=year)

    title_suffix = f" - {year}" if year else ""
    if any_plotted:
        ax.set_xlabel("Month")
        ax.set_ylabel("New notifications since previous run")
        ax.set_title(f"Weekly new case numbers - {state} (confirmed + probable){title_suffix}")
        handles, labels = ax.get_legend_handles_labels()
        if any_anomaly:
            marker = plt.Line2D(
                [], [], marker="o", markersize=9, markerfacecolor="none",
                markeredgecolor="red", markeredgewidth=1.8, linestyle="None",
            )
            handles.append(marker)
            labels.append("Reported total went down\nthat week (source data,\nnot an error)")
        # Legend placed below the plot in its own reserved space, rather than
        # "best fit" inside the axes, so a long label can never overlap or
        # get clipped by the data itself.
        fig.legend(
            handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.02),
            ncol=min(len(labels), 3), fontsize=9, frameon=True,
        )
        fig.autofmt_xdate(rotation=45)
        fig.tight_layout(rect=(0, 0.13, 1, 1))
    else:
        ax.set_title(f"Weekly new case numbers - {state}{title_suffix}")
        fig.tight_layout()

    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def plot_weekly_new_cases_by_state(results, out_path, states, year=None):
    """Grid of small weekly-new-cases charts, one subplot per state.
    If year is given, only that calendar year is shown."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    diseases = sorted(set(r["disease"] for r in results if r["state"] != "National"))
    ncols = 4
    nrows = (len(states) + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(5 * ncols, 4.6 * nrows), squeeze=False)

    any_anomaly_overall = False
    for i, state in enumerate(states):
        ax = axes[i // ncols][i % ncols]
        _, any_anomaly = _plot_new_cases_on_ax(ax, results, state, diseases, year=year)
        any_anomaly_overall = any_anomaly_overall or any_anomaly
        ax.set_title(state, fontsize=11)
        ax.tick_params(axis="x", labelrotation=45, labelsize=7)

    # Hide any unused grid cells (e.g. 8 states doesn't perfectly fill a grid)
    for i in range(len(states), nrows * ncols):
        axes[i // ncols][i % ncols].axis("off")

    handles, labels = axes[0][0].get_legend_handles_labels()
    if any_anomaly_overall:
        marker = plt.Line2D(
            [], [], marker="o", markersize=9, markerfacecolor="none",
            markeredgecolor="red", markeredgewidth=1.8, linestyle="None",
        )
        handles.append(marker)
        labels.append("Reported total went down that week (source data, not an error)")
    if handles:
        # A generous, explicitly-reserved bottom margin (rect + bottom=)
        # rather than a small fixed fraction - this is what was clipping
        # the legend before when the label text ran long.
        fig.legend(
            handles, labels, loc="lower center", ncol=2,
            bbox_to_anchor=(0.5, 0.0), fontsize=10, frameon=True,
        )
    title_suffix = f" - {year}" if year else ""
    fig.suptitle(f"Weekly new case numbers by state/territory{title_suffix}", fontsize=14)
    fig.tight_layout(rect=(0, 0.09, 1, 0.96))
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--data-dir", default="data",
        help="Directory to store CSV history (default: ./data)",
    )
    parser.add_argument(
        "--graphs-dir", default="graphs",
        help="Directory to store PNG charts, separate from --data-dir (default: ./graphs)",
    )
    args = parser.parse_args()

    os.makedirs(args.data_dir, exist_ok=True)
    os.makedirs(args.graphs_dir, exist_ok=True)
    annual_csv = os.path.join(args.data_dir, "annual_totals.csv")
    weekly_csv = os.path.join(args.data_dir, "weekly_snapshots.csv")
    weekly_national_png = os.path.join(args.graphs_dir, "weekly_new_cases_national.png")
    weekly_by_state_png = os.path.join(args.graphs_dir, "weekly_new_cases_by_state.png")

    run_date = dt.date.today().isoformat()
    current_year = str(dt.date.today().year)

    print("Connecting to NINDSS dashboard...")
    session_info = bootstrap_session()
    print(f"  model_id={session_info['model_id']} dataset_id={session_info['dataset_id']}")
    print(f"  query endpoint={session_info['query_url']}")

    all_state_totals = {}  # {disease: {year: {state: total}}}
    states_seen = set()
    for label, disease_name in DISEASE_NAMES.items():
        print(f"Fetching {label} ('{disease_name}')...")
        year_state_totals = fetch_disease_year_state_totals(session_info, disease_name, REPORT_ID)
        all_state_totals[label] = year_state_totals
        for state_totals in year_state_totals.values():
            states_seen.update(state_totals.keys())
        national_this_year = sum(year_state_totals.get(current_year, {}).values())
        print(f"  -> {current_year} national total so far: {national_this_year}")
    states = sorted(states_seen)

    save_annual_totals_csv(annual_csv, all_state_totals)
    append_weekly_snapshot(weekly_csv, run_date, all_state_totals)

    snapshot_rows = load_weekly_snapshots(weekly_csv)
    results = compute_new_cases(snapshot_rows)

    print("\nNew cases since last run (national):")
    latest_by_disease = {}
    anomalies_this_run = []
    for r in results:
        if r["run_date"] != run_date:
            continue
        if r["anomaly"]:
            anomalies_this_run.append(r)
        if r["year"] == current_year and r["state"] == "National":
            latest_by_disease[r["disease"]] = r["new_cases"]
    for label in DISEASE_NAMES:
        new_cases = latest_by_disease.get(label)
        if new_cases is None:
            print(f"  {label}: (first run - no baseline yet)")
        else:
            print(f"  {label}: {new_cases}")

    # Previous-year totals sometimes creep up after New Year's due to
    # reporting lag - surface that explicitly rather than letting it hide
    # silently in the CSV.
    prev_year = str(int(current_year) - 1)
    prev_year_updates = [
        r for r in results
        if r["run_date"] == run_date and r["year"] == prev_year
        and r["state"] == "National" and r["new_cases"] not in (None, 0)
    ]
    if prev_year_updates:
        print(f"\n{prev_year} totals were revised since last run (late-reported notifications):")
        for r in prev_year_updates:
            direction = "up" if r["new_cases"] > 0 else "down"
            print(f"  {r['disease']}: {direction} by {abs(r['new_cases'])}")

    if anomalies_this_run:
        print("\nWARNING: the source data was revised DOWNWARD for these (unusual - worth a look):")
        for r in anomalies_this_run:
            print(f"  {r['disease']} / {r['state']} {r['year']}: now {r['cumulative']} (was higher last run)")

    plot_weekly_new_cases(results, weekly_national_png, state="National")
    plot_weekly_new_cases_by_state(results, weekly_by_state_png, states)

    years_present = sorted(set(r["year"] for r in results if r["new_cases"] is not None))
    year_files = []
    for year in years_present:
        national_year_png = os.path.join(args.graphs_dir, f"weekly_new_cases_national_{year}.png")
        by_state_year_png = os.path.join(args.graphs_dir, f"weekly_new_cases_by_state_{year}.png")
        plot_weekly_new_cases(results, national_year_png, state="National", year=year)
        plot_weekly_new_cases_by_state(results, by_state_year_png, states, year=year)
        year_files += [national_year_png, by_state_year_png]

    print(
        f"\nSaved:\n  {annual_csv}\n  {weekly_csv}\n  {weekly_national_png}\n"
        f"  {weekly_by_state_png}\n  " + "\n  ".join(year_files)
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# SCHEDULING NOTES
# ---------------------------------------------------------------------------
# macOS/Linux (cron), run every Monday at 9am:
#   0 9 * * 1 /usr/bin/python3 /full/path/to/nindss_tracker.py --data-dir /full/path/to/data --graphs-dir /full/path/to/graphs >> /full/path/to/data/log.txt 2>&1
#
# Windows: use Task Scheduler to run weekly:
#   python.exe C:\path\to\nindss_tracker.py --data-dir C:\path\to\data --graphs-dir C:\path\to\graphs
# ---------------------------------------------------------------------------
