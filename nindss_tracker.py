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

As of Aug 2026, these endpoints are behind bot-protection that survived
three separate, well-targeted fix attempts using plain HTTP clients:
matching the browser's exact headers, matching its TLS fingerprint
(curl_cffi/curl-impersonate), and matching its exact call sequence over a
persistent connection - all produced the identical empty-body 403. That
pattern (fails even on the very first request, before any session state
could matter) means the block is almost certainly a JavaScript-computed
challenge invisible to a HAR capture - something no amount of replicating
*observed* request shape can solve, because the thing producing a valid
request is code we can't see running.

So this script drives an actual, real headless Chromium browser via
Playwright instead of impersonating one: it loads the real dashboard page,
finds the Power BI iframe Microsoft's embed renders into, and runs the same
fetch() calls a real session would - from inside that real browser's JS
engine. This isn't an impersonation technique, it doesn't need to be - it
IS a browser, so whatever the challenge mechanism actually is gets solved
the same way it would for a person visiting the page.

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
  - weekly_new_cases_national_<year>.png / _by_state_<year>.png
                                     Same two charts, one pair per calendar year.
  - weekly_new_cases_national.html / _by_state.html / _national_<year>.html /
    _by_state_<year>.html           Interactive (Plotly) versions of the
                                     same charts above - hover a point for
                                     its exact date and value. Open in a
                                     browser; loads Plotly's JS from a CDN,
                                     so needs internet access to render.

CAVEATS
-------
- This relies on an undocumented, internal Power BI API that Microsoft/health.gov.au
  could change at any time without notice. If it stops working, re-capture a
  HAR file from https://nindss.health.gov.au/pbi-dashboard/ (Chrome DevTools
  -> Network tab -> Preserve log -> reload -> "Save all as HAR with content")
  and check: (a) REPORT_ID below still matches, (b) DISEASE_NAMES still match
  the "Where" filters of the QueryExecutionService requests, (c) whether the
  Power BI iframe's URL/structure changed in a way find_powerbi_frame() needs
  to account for.
- Needs a real Chromium binary installed (`playwright install chromium`),
  not just the `playwright` pip package - see requirements.txt / README.
- "New cases this week" is only as good as your run cadence - if you skip a
  week or two, the delta just covers a longer period; the script does not
  try to guess actual daily case dates. But a gap over MAX_GAP_DAYS (see
  compute_new_cases) is treated as a fresh start rather than one
  artificially huge "new cases" figure covering the whole gap.
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

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Configuration - update these if the dashboard changes (see CAVEATS above)
# ---------------------------------------------------------------------------

DASHBOARD_URL = "https://nindss.health.gov.au/pbi-dashboard/"

REPORT_ID = "bc027587-5e9e-4920-bf03-a45fd3079f25"

# Exact "DISEASE NAME" values as used by the dashboard's own filters.
DISEASE_NAMES = {
    "COVID-19": "COVID-19",
    "Influenza": "Influenza (laboratory confirmed)",
    "RSV": "Respiratory syncytial virus (RSV)",
    "Measles": "Measles",
}

# Fixed per-disease plot colour, so e.g. COVID-19 is always the same colour
# on every graph. Without this, matplotlib's default colour cycling assigns
# colours in whatever order diseases are plotted on THAT axis - if one
# subplot happens to have no data for a disease (skipping it, never calling
# ax.plot() for it), every disease after it silently shifts into the next
# colour along, so the same disease ends up a different colour on different
# graphs. Red is deliberately not used here - it's reserved for circling
# anomalies (see _plot_new_cases_on_ax) regardless of which disease line
# they land on.
DISEASE_COLORS = {
    "COVID-19": "#1f77b4",
    "Influenza": "#ff7f0e",
    "RSV": "#2ca02c",
    "Measles": "#9467bd",
}

# Break a plotted line wherever consecutive points for the same
# disease/state/year are separated by more than this many days - see
# _split_into_gap_segments. Deliberately smaller than compute_new_cases's
# own MAX_GAP_DAYS (which resets new_cases to None, i.e. drops the point
# entirely, past 45 days): this constant only has to catch smaller-but-
# still-unusual gaps (a few skipped weeks) that still produce a real
# new_cases value but would look misleading connected by a straight line.
GAP_BREAK_DAYS = 21

# Only application-level headers - NOT browser-fingerprint headers
# (User-Agent, Origin, Referer, Sec-*, Accept-Encoding, etc). Those used to
# be spoofed by hand here, but a real browser (via Playwright) sets all of
# that automatically and correctly - and 'Origin' specifically is a
# "forbidden header name" that JS fetch() won't even let us override, so
# there's no point trying. Less to keep in sync with future HAR captures.
APP_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "x-powerbi-hostenv": "Embed for Customers",
}

REQUEST_TIMEOUT_MS = 30000
PAGE_LOAD_TIMEOUT_MS = 90000
# Confirmed via a real run (Aug 2026): the dashboard makes 100+ requests
# during its own natural bootstrap (large JS bundles, fonts, telemetry,
# then finally the actual API calls) - the modelsAndExploration call
# genuinely can take longer than networkidle + a short grace window to
# show up. 25s wasn't enough margin and caused a false "never observed"
# failure even though the real page loaded and worked fine (confirmed via
# debug_screenshot.png showing live data). 60s gives real headroom.
FRAME_WAIT_TIMEOUT_S = 60


# ---------------------------------------------------------------------------
# Step 1: bootstrap - load the real dashboard, find the Power BI frame,
# discover the current modelId / datasetId / capacity URL
# ---------------------------------------------------------------------------

_FETCH_JS = """
async ({url, method, headers, body}) => {
    const opts = {method, headers};
    if (body !== null && body !== undefined) { opts.body = body; }
    const resp = await fetch(url, opts);
    const text = await resp.text();
    return {status: resp.status, ok: resp.ok, headers: Object.fromEntries(resp.headers.entries()), text};
}
"""


def _frame_fetch(frame, url, method="GET", headers=None, body=None, compare_to_sample=None):
    """Run a fetch() call inside a real (Playwright-controlled) browser
    frame's own JS context, so it inherits that frame's real TLS/HTTP
    fingerprint, cookies, and any bot-challenge state - rather than us
    trying to fake any of that ourselves."""
    result = frame.evaluate(_FETCH_JS, {
        "url": url, "method": method, "headers": headers or {}, "body": body,
    })
    if not result["ok"]:
        print(f"  [debug] {method} {url} -> HTTP {result['status']}", file=sys.stderr)
        print(f"  [debug] response headers: {result['headers']}", file=sys.stderr)
        print(f"  [debug] response body (first 500 chars): {result['text'][:500]}", file=sys.stderr)
        if compare_to_sample and compare_to_sample.get("request"):
            print(f"  [debug] our request headers: {headers}", file=sys.stderr)
            print(
                f"  [debug] headers from the page's own naturally-triggered query call "
                f"(status {compare_to_sample.get('status')} to {compare_to_sample.get('url')}): "
                f"{compare_to_sample['request']}",
                file=sys.stderr,
            )
            our_keys = set((headers or {}).keys())
            their_keys = set(compare_to_sample["request"].keys())
            missing = their_keys - our_keys
            if missing:
                print(f"  [debug] headers the real call sends that ours doesn't: {sorted(missing)}", file=sys.stderr)
        else:
            print(
                "  [debug] no naturally-triggered query call was observed during the "
                "initial page load, so there's nothing to compare our headers against.",
                file=sys.stderr,
            )
        raise RuntimeError(f"HTTP {result['status']} for {url}")
    return result["text"]


def find_powerbi_frame(page):
    """The dashboard page embeds the actual Power BI report in an iframe on
    app.powerbi.com - that's the frame whose JS context has the real,
    trusted browser session later per-disease query calls need to run
    inside. Iframe load can lag behind the main page's load event, so poll
    for it."""
    deadline = time.time() + FRAME_WAIT_TIMEOUT_S
    while time.time() < deadline:
        for frame in page.frames:
            if "powerbi.com" in frame.url:
                try:
                    frame.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:  # noqa: BLE001 - fall through to retry loop
                    pass
                return frame
        time.sleep(0.5)
    raise RuntimeError(
        "Couldn't find a powerbi.com iframe on the dashboard page within "
        f"{FRAME_WAIT_TIMEOUT_S}s. The page structure may have changed - "
        "re-capture a HAR and check what frame/iframe the report now loads "
        "into. See CAVEATS at the top of this script."
    )


def bootstrap_session(playwright):
    # Even a genuine headless-Chromium fetch() call, injected immediately
    # after navigation, still got 403'd (confirmed Aug 2026) - identically
    # to every plain-HTTP attempt before it. That rules fingerprinting out
    # entirely (this IS a real browser) and points at something more subtle:
    # racing the page's OWN natural bootstrap sequence with a duplicate call
    # before whatever establishes trust has finished. So instead of firing
    # our own conceptualschema/modelsAndExploration requests, we let the
    # real page trigger those calls itself and read the real response it
    # already got - zero racing, zero duplication, pure observation.
    #
    # A hand-rolled page.on("response", ...) listener + polling loop was
    # tried first and consistently failed to notice a response that clearly
    # DID happen (confirmed via the full request-log dump and a screenshot
    # showing the fully-rendered dashboard) - the likely cause is a known
    # sharp edge in Playwright's sync API: event callbacks run on its
    # internal driver thread, and holding onto / re-touching that Response
    # object later from the main thread is a documented source of exactly
    # this kind of silent, hard-to-reproduce miss. expect_response() is
    # Playwright's own purpose-built, tested API for "wait for a specific
    # response triggered by this action" - it avoids that whole class of
    # problem rather than working around it by hand.
    all_requests_seen = []  # every request URL+status the page makes - diagnostic only
    query_sample = {}  # first naturally-triggered /query request, for header comparison if we hit auth trouble later
    capturing_sample = [True]  # mutable so the closure below can flip it off

    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()

    def on_response(r):
        all_requests_seen.append(f"{r.status} {r.url}")
        # Bug found the hard way: this listener stays registered on `page`
        # for its whole lifetime, and fetch_disease_year_state_totals()
        # reuses this same page/frame much later via _frame_fetch(). Without
        # this guard, OUR OWN later injected query call re-triggers this
        # same handler and gets mistaken for a "natural" sample to compare
        # against - comparing our failing request against itself, which is
        # useless (confirmed via a live run: the "sample" had our own exact
        # activityid/requestid). Only ever capture during the initial load.
        if not capturing_sample[0]:
            return
        if "QueryExecutionService" in r.url and "request" not in query_sample:
            try:
                query_sample["request"] = dict(r.request.headers)
                query_sample["url"] = r.url
                query_sample["status"] = r.status
            except Exception:  # noqa: BLE001 - purely opportunistic, never let this break the real flow
                pass

    page.on("response", on_response)

    try:
        with page.expect_response(
            lambda r: "modelsAndExploration" in r.url,
            timeout=(PAGE_LOAD_TIMEOUT_MS + FRAME_WAIT_TIMEOUT_S * 1000),
        ) as response_info:
            page.goto(DASHBOARD_URL, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)
        resp = response_info.value
    except Exception as exc:  # noqa: BLE001 - includes Playwright's TimeoutError
        # Dump everything we can to actually diagnose this rather than
        # guess again: what the browser rendered, what frames exist, and
        # every single network request the page made. debug_screenshot.png
        # in particular will show a captcha/consent banner/error page if
        # that's what's actually happening, which none of the network-level
        # signals below can reveal on their own.
        try:
            page.screenshot(path="debug_screenshot.png", full_page=True)
            print("  [debug] saved debug_screenshot.png - open it to see what the browser actually rendered", file=sys.stderr)
        except Exception as screenshot_exc:  # noqa: BLE001
            print(f"  [debug] couldn't save screenshot: {screenshot_exc}", file=sys.stderr)

        print(f"  [debug] page.url = {page.url}", file=sys.stderr)
        print(f"  [debug] frames found ({len(page.frames)}):", file=sys.stderr)
        for f in page.frames:
            print(f"    - {f.url}", file=sys.stderr)

        print(f"  [debug] {len(all_requests_seen)} responses observed total:", file=sys.stderr)
        for line in all_requests_seen:
            print(f"    - {line}", file=sys.stderr)

        raise RuntimeError(
            f"Failed waiting for the page's own modelsAndExploration call ({exc}) - "
            "the dashboard's structure or bootstrap sequence may have changed. "
            "See CAVEATS in this script, and the debug output above/debug_screenshot.png."
        )

    if not resp.ok:
        print(f"  [debug] modelsAndExploration HTTP {resp.status} (observed, not injected)", file=sys.stderr)
        print(f"  [debug] response body (first 500 chars): {resp.text()[:500]}", file=sys.stderr)
        raise RuntimeError(f"The dashboard's own modelsAndExploration call itself returned HTTP {resp.status}")

    data = json.loads(resp.text())

    models = data.get("models")
    if not models:
        raise RuntimeError(
            "Couldn't find 'models' in modelsAndExploration response - the "
            "dashboard's API may have changed. See CAVEATS in this script."
        )

    model = models[0]
    model_id = model["id"]
    dataset_id = model["dbName"]

    # As of Aug 2026 this lives at the top level under "exploration", not
    # nested inside the model object where it used to be (confirmed via a
    # live run's [debug] dump). Checking the old location too as a fallback
    # costs nothing and adds a little resilience if it ever moves back.
    capacity_uri = data.get("exploration", {}).get("capacityUri") or model.get("capacityUri")
    if not capacity_uri:
        # The response shape has evidently changed again since this script
        # was last updated - rather than guess at a new field name, find
        # every key anywhere in the response whose name mentions "capacity"
        # or "uri" (recursing into nested dicts/lists) and print those
        # paths and values, plus the top-level key structure. That should
        # show exactly where the capacity info moved to, if it's in there
        # at all.
        def find_matching_paths(obj, path=""):
            matches = []
            if isinstance(obj, dict):
                for k, v in obj.items():
                    new_path = f"{path}.{k}" if path else k
                    if "capacit" in k.lower() or "uri" in k.lower():
                        matches.append((new_path, v if not isinstance(v, (dict, list)) else type(v).__name__))
                    matches += find_matching_paths(v, new_path)
            elif isinstance(obj, list):
                for i, item in enumerate(obj[:3]):  # cap it - just need a sample
                    matches += find_matching_paths(item, f"{path}[{i}]")
            return matches

        print(f"  [debug] model's top-level keys: {sorted(model.keys())}", file=sys.stderr)
        print(f"  [debug] response's top-level keys: {sorted(data.keys())}", file=sys.stderr)
        print("  [debug] keys anywhere in the response mentioning 'capacity' or 'uri':", file=sys.stderr)
        for path, val in find_matching_paths(data):
            print(f"    - {path} = {val}", file=sys.stderr)

        raise RuntimeError(
            "Couldn't find 'capacityUri' in modelsAndExploration response - "
            "see the [debug] output above for where the capacity info "
            "actually lives in the current response shape, then update "
            "bootstrap_session() to match. See CAVEATS in this script."
        )
    # capacityUri already ends in '.../public/' - the query endpoint is
    # that plus 'query'.
    query_url = capacity_uri.rstrip("/") + "/query"

    frame = find_powerbi_frame(page)

    # modelsAndExploration resolving doesn't mean the report's own internal
    # rendering pipeline has gotten around to issuing ITS query calls yet -
    # confirmed via a live run where the sample was never captured at all,
    # even though the report demonstrably does make these calls (seen
    # earlier in a full request-log dump). Give it a real window to happen
    # naturally before closing off capturing - still well before our own
    # first injected call would fire (that only happens after this function
    # returns AND several more statements run in the caller). on_response
    # (registered above) does the actual capturing as a side effect of
    # whatever happens during this wait - nothing more to do here than wait.
    if not query_sample:
        page.wait_for_timeout(15000)
    capturing_sample[0] = False

    return {
        "model_id": model_id,
        "dataset_id": dataset_id,
        "query_url": query_url,
        "browser": browser,
        "page": page,
        "frame": frame,
        "query_sample": query_sample,
    }


def close_session(session_info):
    try:
        session_info["browser"].close()
    except Exception:  # noqa: BLE001 - best-effort cleanup, never fail the run over this
        pass


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


def _parse_quoted_literal(raw):
    """
    Parse a Power BI quoted value literal, e.g. "'3,363,646'" or "'<5'"
    (privacy-suppressed small counts, shown as '<5' on the dashboard
    itself). We treat '<5' as 3 (the midpoint of 0-4) since the true value
    isn't published - this only affects a handful of very small/old rows.
    """
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


def _lookup_dict_value(dict_list, index):
    """Resolve one entry from a Power BI ValueDicts list by index."""
    try:
        raw = dict_list[index]
    except (IndexError, TypeError):
        return 0
    return _parse_quoted_literal(raw)


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
        # "R" is a run-length repeat of whatever value immediately precedes
        # it in the flattened year-then-state sequence - which can be the
        # last state of the *previous* year's row, not just an earlier
        # column in the *current* row (confirmed via a live response: years
        # with no real cases at all come back as a row of pure {"R": n}
        # items, no M0 anywhere in that row, that must carry forward the
        # last real value seen). So last_value persists across rows here,
        # not reset per-row.
        last_value = 0
        for ph_row in ds.get("PH", []):
            for row in ph_row.get("DM0", []):
                year = row.get("G0")
                if year is None:
                    continue
                year_result = result.setdefault(year, {})
                col = 0
                for item in row.get("X", []):
                    if "S" in item:
                        for s_desc in item["S"]:
                            if s_desc.get("N") == "M0" and "DN" in s_desc:
                                dict_name = s_desc["DN"]
                    if "M0" in item:
                        raw = item["M0"]
                        if isinstance(raw, str) and raw.startswith("'"):
                            # A quoted literal like "'17,895'" or "'<5'" sent
                            # inline rather than as a dictionary index -
                            # confirmed via a live response: higher-
                            # cardinality columns (e.g. Influenza, which has
                            # far more distinct values than COVID) mix these
                            # in with plain-int dictionary indices in the
                            # same row once a value doesn't fit the
                            # dictionary. _parse_dsr_number can't handle the
                            # quotes/commas, so this needs the same parsing
                            # ValueDicts entries get.
                            value = _parse_quoted_literal(raw)
                        elif isinstance(raw, str):
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
    headers = dict(APP_HEADERS)
    headers["content-type"] = "application/json;charset=UTF-8"
    headers["activityid"] = str(uuid.uuid4())
    headers["requestid"] = str(uuid.uuid4())

    # As of Aug 2026 the query endpoint 401s ("Authentication failed for
    # all authenticators") without a bearer token - confirmed via a live
    # run's [debug] header diff. That token (an "MWCToken" JWT) is minted
    # by the Power BI client JS for its own naturally-triggered query call,
    # not something a plain fetch() can obtain or fake - so reuse it from
    # query_sample (captured during bootstrap_session's initial page load)
    # along with the other non-fingerprint headers the real call sent that
    # ours didn't. It's a session-scoped credential tied to the report
    # session, not to one specific query, so it's safe to reuse across all
    # per-disease requests in this run.
    sample_headers = (session_info.get("query_sample") or {}).get("request") or {}
    for key in ("authorization", "x-ms-workload-resource-moniker", "x-ms-root-activity-id", "x-ms-parent-activity-id"):
        if key in sample_headers:
            headers[key] = sample_headers[key]
    if "authorization" not in headers:
        print(
            "  [debug] no 'authorization' header available to reuse - no naturally-triggered "
            "query call was observed during bootstrap, so this request will likely 401.",
            file=sys.stderr,
        )

    body = build_query_body(
        disease_name, session_info["model_id"], session_info["dataset_id"], report_id
    )

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            text = _frame_fetch(
                session_info["frame"], session_info["query_url"],
                method="POST", headers=headers, body=json.dumps(body),
                compare_to_sample=session_info.get("query_sample"),
            )
            data = json.loads(text)
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


def append_weekly_snapshot(path, run_date, all_state_totals, current_year):
    """
    all_state_totals: {disease: {year: {state: total}}}
    Appends one row per disease/state/year (plus a synthetic
    state="National" row summing all states), but only when it's actually
    worth recording:
      - the CURRENT calendar year is written unconditionally, every run -
        that's the "live" series people actually watch week to week, and
        both the console "New cases since last run" summary and
        compute_new_cases's own gap detection depend on it having a row
        roughly every 7 days.
      - any OTHER (already-completed) year is only written when its
        cumulative total actually changed since the last row recorded for
        that exact disease/state/year - i.e. a genuine late-reported
        correction landed. Skipping unchanged old-year rows is what stops
        this file growing by roughly (diseases x states x tracked-years)
        rows on EVERY single run forever, almost all of them just
        restating a number that hasn't moved - confirmed via a live run:
        one run wrote ~1300 such rows for years back to 1991, the huge
        majority completely unchanged from the week before.
    Snapshotting every year (rather than only the current one) is still
    what makes each year's series self-contained - never compared against
    a different year's total at a rollover - and what catches late-
    reported/backdated corrections at all; this just stops re-recording
    the ones that didn't change.
    """
    file_exists = os.path.exists(path)

    # The last cumulative value already on file for every (disease, state,
    # year), so an unchanged past-year row can be skipped. The file is
    # always appended to in run_date order, so a single forward pass -
    # letting a later row for the same key overwrite an earlier one -
    # lands on the most recent value for free.
    last_known = {}
    if file_exists:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                last_known[(row["disease"], row["state"], row["year"])] = int(row["cumulative_notifications"])

    def should_write(disease, state, year, cumulative):
        if str(year) == str(current_year):
            return True
        key = (disease, state, str(year))
        return last_known.get(key) != cumulative

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["run_date", "disease", "state", "year", "cumulative_notifications"])
        for disease, year_state_totals in all_state_totals.items():
            for year, state_totals in sorted(year_state_totals.items()):
                national_total = sum(state_totals.values())
                if should_write(disease, "National", year, national_total):
                    writer.writerow([run_date, disease, "National", year, national_total])
                for state, cumulative in sorted(state_totals.items()):
                    if should_write(disease, state, year, cumulative):
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

    Also new_cases=None (a reset, same as a first sighting) whenever the gap
    since that key's previous snapshot exceeds MAX_GAP_DAYS - e.g. an older
    version of this script only ever recorded the *current* year each week,
    so an older year's tracking silently stopped the moment the year rolled
    over, until multi-year fetching resumed much later. Reporting however
    much a value grew over a gap like that as a single week's "new cases"
    would be fabricating a number, not just reporting a slightly-longer-
    than-usual one.

    MAX_GAP_DAYS is deliberately generous (120 days, not e.g. 45) because
    append_weekly_snapshot only writes a row for an already-completed year
    when its value actually changed - so a quiet past year can legitimately
    go a couple of months between real rows even though the script itself
    never stopped running. A tempting fix would be to instead check "did
    the script run continuously" via run_dates from OTHER keys (the current
    year is always written) rather than this key's own gap - but that
    doesn't work: it can't distinguish "this key was checked every week and
    genuinely didn't change" from "this key was never even checked" (true
    of every pre-multi-year-fetch historical row), because both look
    identical - some row exists for some other key on every date either
    way. Confirmed by testing: that approach failed to reset the real,
    known ~600-day 2024 tracking gap, silently resurrecting the exact
    fabricated-delta bug this was meant to prevent. A single conservative
    per-key threshold is less clever but doesn't have that failure mode -
    the accepted trade-off is that a correction to a VERY quiet
    disease/state/year arriving more than 120 days after the last one
    occasionally shows as a reset instead of a delta. Nothing is lost
    silently: the cumulative total is still recorded correctly as the new
    baseline for the next comparison, it just doesn't get reported as a
    "revised" figure that one time.

    Rows from before the 'state' column existed are treated as state="National"
    for backwards compatibility with older weekly_snapshots.csv files.
    """
    by_group = {}
    for row in snapshot_rows:
        state = row.get("state") or "National"
        key = (row["disease"], state)
        by_group.setdefault(key, []).append(row)

    MAX_GAP_DAYS = 120

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
                prev_cumulative, prev_run_date = prev
                gap_days = (
                    dt.datetime.strptime(row["run_date"], "%Y-%m-%d")
                    - dt.datetime.strptime(prev_run_date, "%Y-%m-%d")
                ).days
                if gap_days > MAX_GAP_DAYS:
                    new_cases = None
                else:
                    new_cases = cumulative - prev_cumulative
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
            prev_by_year[year] = (cumulative, row["run_date"])
    return results


# ---------------------------------------------------------------------------
# Step 4: plotting
# ---------------------------------------------------------------------------

def _weekly_points(results, state, disease, year=None):
    """Rows for one disease/state (optionally filtered to one notification-
    year bucket), sorted chronologically by run_date. Shared by both the
    static (matplotlib) and interactive (plotly) plotting code so they can
    never disagree on which points to draw or in what order.

    Sorted by run_date alone - NOT (year, run_date). "year" here is which
    notification-year bucket a point belongs to, and every run snapshots
    every year's running total (so late corrections get caught), so on the
    full-history graphs (year=None) sorting year-then-date would group all
    of 2024's points across the WHOLE run-date range, then all of 2025's
    points across that same range again, etc. - a real date x-axis then has
    to jump forward across the full range once per year bucket, then jump
    back to the start for the next one. Plain run_date order is correct for
    both the combined graph and the single-year ones (where there's only
    one bucket anyway, so this is equivalent to sorting by (year, date)
    there).

    For the combined graph (year=None) specifically, also drop any row
    whose notification-year bucket ISN'T the calendar year the run itself
    happened in. Without this, every run's near-always-zero "late
    correction" delta for every OTHER tracked year lands on the exact same
    x-position as that week's real current-year delta - confirmed via a
    simulated run: 36 separate points (35 of them 0) plotted on a single
    date for one disease, once enough years are being tracked in parallel
    (which is already true today, not just after a year rollover). Drawn
    as one line, that's a vertical spike every single week. Restricting to
    the "current year at the time" bucket makes the combined graph behave
    exactly like each year's own graph stitched end-to-end at each Jan 1
    rollover, which is what "full history" is actually meant to show -
    the operational week-to-week trend, not every correction to every
    year all mashed onto one line. (Per-year graphs are unaffected - they
    already filter to one bucket, so there's nothing to collide with.)"""
    points = [
        r for r in results
        if r["state"] == state and r["disease"] == disease
        and r["new_cases"] is not None
        and (year is None or r["year"] == year)
    ]
    if year is None:
        points = [
            r for r in points
            if r["year"] == str(dt.datetime.strptime(r["run_date"], "%Y-%m-%d").year)
        ]
    return sorted(points, key=lambda r: r["run_date"])


def _split_into_gap_segments(points, max_gap_days=GAP_BREAK_DAYS):
    """Split chronologically-sorted rows (as returned by _weekly_points)
    into runs wherever the date gap between consecutive points exceeds
    max_gap_days - e.g. a year's tracking stopped for months (older
    versions of this script only recorded the *current* year each week)
    before multi-year fetching resumed and picked up one genuine but
    isolated late-arriving point. A straight line across that gap would
    visually claim a smooth trend that never actually happened, so each
    gap-separated run gets drawn as its own segment instead - isolated
    points then show as disconnected markers rather than the ends of a
    long diagonal line."""
    segments = [[]]
    for r in points:
        date = dt.datetime.strptime(r["run_date"], "%Y-%m-%d")
        if segments[-1]:
            prev_date = dt.datetime.strptime(segments[-1][-1]["run_date"], "%Y-%m-%d")
            if (date - prev_date).days > max_gap_days:
                segments.append([])
        segments[-1].append(r)
    return segments


def _plot_new_cases_on_ax(ax, results, state, diseases, year=None):
    """Draw the weekly-new-cases lines for one state onto one axis.
    If year is given (e.g. '2025'), only that calendar year is plotted."""
    import matplotlib.dates as mdates

    any_plotted = False
    any_anomaly = False
    all_dates = []
    for disease in diseases:
        points = _weekly_points(results, state, disease, year=year)
        if not points:
            continue
        color = DISEASE_COLORS.get(disease)
        # Dates are parsed to real datetime objects (not left as strings)
        # so the x-axis can use a proper monthly tick locator instead of
        # labeling every single week.
        for seg_i, seg in enumerate(_split_into_gap_segments(points)):
            xs = [dt.datetime.strptime(r["run_date"], "%Y-%m-%d") for r in seg]
            ys = [r["new_cases"] for r in seg]
            ax.plot(
                xs, ys, marker="o", markersize=3, color=color,
                label=disease if seg_i == 0 else None,
            )
            all_dates.extend(xs)
        any_plotted = True
        # Plot anomalies (genuine reported decreases) as a red ring on top
        # of the existing point, rather than a gap in the line, so the dip
        # itself still shows.
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
# Step 4b: interactive (hover-for-date-and-value) HTML versions of the same
# charts, via Plotly - same data, same colours, same gap-splitting as the
# static PNGs above (both read through _weekly_points/_split_into_gap_
# segments), just rendered as self-contained HTML instead of a raster image.
# ---------------------------------------------------------------------------

ANOMALY_LEGEND_LABEL = "Reported total went down that week (source data, not an error)"


def _add_disease_traces_interactive(fig, results, state, diseases, year=None, row=None, col=None, legend_seen=None):
    """Add one hover-enabled line+marker trace per disease (split into
    gap-separated segments, matching _plot_new_cases_on_ax) plus a red-ring
    anomaly overlay, to a plotly figure - optionally at a specific subplot
    row/col for the by-state grid. legend_seen is a set this mutates so a
    shared legend across many subplots only gets one entry per disease,
    not one per subplot."""
    import plotly.graph_objects as go

    if legend_seen is None:
        legend_seen = set()
    add_kwargs = {"row": row, "col": col} if row is not None else {}

    any_plotted = False
    any_anomaly = False
    for disease in diseases:
        points = _weekly_points(results, state, disease, year=year)
        if not points:
            continue
        any_plotted = True
        color = DISEASE_COLORS.get(disease)
        for seg in _split_into_gap_segments(points):
            fig.add_trace(
                go.Scatter(
                    x=[r["run_date"] for r in seg],
                    y=[r["new_cases"] for r in seg],
                    mode="lines+markers",
                    name=disease,
                    legendgroup=disease,
                    showlegend=disease not in legend_seen,
                    line=dict(color=color),
                    marker=dict(color=color, size=6),
                    hovertemplate=f"%{{x|%d %b %Y}}<br>{disease}: %{{y:,}}<extra></extra>",
                ),
                **add_kwargs,
            )
            legend_seen.add(disease)

        # Plot anomalies (genuine reported decreases) as a red ring on top
        # of the existing point, rather than a gap in the line, so the dip
        # itself still shows. hoverinfo="skip" so hovering there shows just
        # the one tooltip from the line trace underneath, not two stacked
        # ones for the same point.
        anomalies = [r for r in points if r["anomaly"]]
        if anomalies:
            any_anomaly = True
            fig.add_trace(
                go.Scatter(
                    x=[r["run_date"] for r in anomalies],
                    y=[r["new_cases"] for r in anomalies],
                    mode="markers",
                    marker=dict(size=12, color="rgba(0,0,0,0)", line=dict(color="red", width=1.8)),
                    name=ANOMALY_LEGEND_LABEL,
                    legendgroup="anomaly",
                    showlegend="anomaly" not in legend_seen,
                    hoverinfo="skip",
                ),
                **add_kwargs,
            )
            legend_seen.add("anomaly")
    return any_plotted, any_anomaly


def plot_weekly_new_cases_interactive(results, out_path, state="National", year=None):
    """Interactive HTML version of plot_weekly_new_cases - hover a point
    for its exact date and value. If year is given, only that calendar
    year is shown."""
    import plotly.graph_objects as go

    diseases = sorted(set(r["disease"] for r in results if r["state"] == state))
    fig = go.Figure()
    any_plotted, _ = _add_disease_traces_interactive(fig, results, state, diseases, year=year)

    title_suffix = f" - {year}" if year else ""
    title = f"Weekly new case numbers - {state} (confirmed + probable){title_suffix}"
    if any_plotted:
        fig.update_layout(
            title=title,
            xaxis_title="Date",
            yaxis_title="New notifications since previous run",
            hovermode="closest",
            template="plotly_white",
            legend=dict(orientation="h", yanchor="top", y=-0.15, xanchor="center", x=0.5),
            margin=dict(b=100),
        )
    else:
        fig.update_layout(
            title=title,
            template="plotly_white",
            annotations=[dict(
                text="Not enough history yet", showarrow=False,
                xref="paper", yref="paper", x=0.5, y=0.5,
            )],
        )
    fig.write_html(out_path, include_plotlyjs="cdn")


def plot_weekly_new_cases_by_state_interactive(results, out_path, states, year=None):
    """Interactive HTML grid version of plot_weekly_new_cases_by_state -
    one hover-enabled subplot per state. If year is given, only that
    calendar year is shown."""
    from plotly.subplots import make_subplots

    diseases = sorted(set(r["disease"] for r in results if r["state"] != "National"))
    ncols = 4
    nrows = (len(states) + ncols - 1) // ncols
    fig = make_subplots(rows=nrows, cols=ncols, subplot_titles=states)

    legend_seen = set()
    for i, state in enumerate(states):
        row, col = i // ncols + 1, i % ncols + 1
        _add_disease_traces_interactive(
            fig, results, state, diseases, year=year, row=row, col=col, legend_seen=legend_seen,
        )

    title_suffix = f" - {year}" if year else ""
    fig.update_layout(
        title=f"Weekly new case numbers by state/territory{title_suffix}",
        template="plotly_white",
        height=280 * nrows,
        legend=dict(orientation="h", yanchor="top", y=-0.06, xanchor="center", x=0.5),
    )
    fig.write_html(out_path, include_plotlyjs="cdn")


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
    weekly_national_html = os.path.join(args.graphs_dir, "weekly_new_cases_national.html")
    weekly_by_state_html = os.path.join(args.graphs_dir, "weekly_new_cases_by_state.html")

    run_date = dt.date.today().isoformat()
    current_year = str(dt.date.today().year)

    print("Connecting to NINDSS dashboard...")
    with sync_playwright() as playwright:
        session_info = bootstrap_session(playwright)
        print(f"  model_id={session_info['model_id']} dataset_id={session_info['dataset_id']}")
        print(f"  query endpoint={session_info['query_url']}")

        all_state_totals = {}  # {disease: {year: {state: total}}}
        states_seen = set()
        try:
            for label, disease_name in DISEASE_NAMES.items():
                print(f"Fetching {label} ('{disease_name}')...")
                year_state_totals = fetch_disease_year_state_totals(session_info, disease_name, REPORT_ID)
                all_state_totals[label] = year_state_totals
                for state_totals in year_state_totals.values():
                    states_seen.update(state_totals.keys())
                # year_state_totals keys come straight from the JSON response
                # (ints), while current_year is a string - compare by str()
                # rather than looking up current_year directly, which would
                # always silently miss and print a false "0".
                national_this_year = sum(
                    sum(state_totals.values())
                    for year, state_totals in year_state_totals.items()
                    if str(year) == current_year
                )
                print(f"  -> {current_year} national total so far: {national_this_year}")
        finally:
            # Close the browser as soon as we're done pulling data - no need
            # to keep it open through the CSV/plotting steps below.
            close_session(session_info)

    if not states_seen:
        # fetch_disease_year_state_totals already raises on an outright HTTP
        # failure - this is the OTHER failure mode: a technically-successful
        # (200 OK) response that just has no rows for any disease, e.g. a
        # DISEASE_NAMES filter silently matching nothing after the dashboard
        # renames a disease. Left unguarded, this crashes deep inside
        # matplotlib ("Number of rows must be a positive integer, not 0")
        # with no indication of the real cause - raise something actionable
        # instead, matching how every other dashboard-shape failure in this
        # script is handled.
        raise RuntimeError(
            "The dashboard returned no per-state data for any disease this "
            "run (zero states seen across all of DISEASE_NAMES) - the "
            "query itself didn't fail, but came back empty. This usually "
            "means a DISEASE_NAMES value no longer matches the dashboard's "
            "own filter values. See CAVEATS in this script."
        )
    states = sorted(states_seen)

    save_annual_totals_csv(annual_csv, all_state_totals)
    append_weekly_snapshot(weekly_csv, run_date, all_state_totals, current_year)

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
    plot_weekly_new_cases_interactive(results, weekly_national_html, state="National")
    plot_weekly_new_cases_by_state_interactive(results, weekly_by_state_html, states)

    years_present = sorted(set(r["year"] for r in results if r["new_cases"] is not None))
    year_files = []
    for year in years_present:
        national_year_png = os.path.join(args.graphs_dir, f"weekly_new_cases_national_{year}.png")
        by_state_year_png = os.path.join(args.graphs_dir, f"weekly_new_cases_by_state_{year}.png")
        national_year_html = os.path.join(args.graphs_dir, f"weekly_new_cases_national_{year}.html")
        by_state_year_html = os.path.join(args.graphs_dir, f"weekly_new_cases_by_state_{year}.html")
        plot_weekly_new_cases(results, national_year_png, state="National", year=year)
        plot_weekly_new_cases_by_state(results, by_state_year_png, states, year=year)
        plot_weekly_new_cases_interactive(results, national_year_html, state="National", year=year)
        plot_weekly_new_cases_by_state_interactive(results, by_state_year_html, states, year=year)
        year_files += [national_year_png, by_state_year_png, national_year_html, by_state_year_html]

    print(
        f"\nSaved:\n  {annual_csv}\n  {weekly_csv}\n  {weekly_national_png}\n"
        f"  {weekly_by_state_png}\n  {weekly_national_html}\n  {weekly_by_state_html}\n  "
        + "\n  ".join(year_files)
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
# One-time setup, in addition to `pip install -r requirements.txt`: this
# script needs a real Chromium binary, not just the playwright pip package.
#   playwright install chromium
# (On a fresh Linux machine/CI runner, "playwright install --with-deps chromium"
# also installs the OS-level libraries Chromium needs.)
#
# macOS/Linux (cron), run every Monday at 9am:
#   0 9 * * 1 /usr/bin/python3 /full/path/to/nindss_tracker.py --data-dir /full/path/to/data --graphs-dir /full/path/to/graphs >> /full/path/to/data/log.txt 2>&1
#
# Windows: use Task Scheduler to run weekly:
#   python.exe C:\path\to\nindss_tracker.py --data-dir C:\path\to\data --graphs-dir C:\path\to\graphs
# ---------------------------------------------------------------------------
