"""
Maine Independent Expenditure Tracker
-------------------------------------
Collects independent expenditure records from
mainecampaignfinancedisclosure.com, visits each record's detail page
to get the target candidate / ballot question, and saves everything
to ie_tracker.csv.

Safe to run repeatedly: it only adds records it hasn't seen before.

How to run (after installing Python and the two libraries below):
    python ie_tracker.py
"""

import csv
import os
import re
import sys
import time
from datetime import datetime

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit(
        "Missing libraries. Run this first:\n"
        "    pip install requests beautifulsoup4"
    )

BASE = "https://www.mainecampaignfinancedisclosure.com"
LIST_URL = BASE + "/public/activities"
CSV_FILE = "ie_tracker.csv"
DELAY_SECONDS = 1.5          # pause between requests (be polite to the server)
MAX_PAGES = 200              # safety cap on pagination

COLUMNS = [
    "transaction_id", "date", "filer", "transaction_type", "amount",
    "payee", "purpose", "explanation", "targets", "detail_url", "first_seen",
]

session = requests.Session()
session.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
})


def get(url, params=None):
    """Fetch a page, with a cache-busting parameter to avoid stale results."""
    params = dict(params or {})
    params["_cb"] = str(int(time.time() * 1000))  # cache buster
    resp = session.get(url, params=params, timeout=60)
    resp.raise_for_status()
    time.sleep(DELAY_SECONDS)
    return resp.text


def load_existing():
    """Read IDs already saved so we don't re-fetch them."""
    seen = set()
    rows = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                seen.add(row.get("transaction_id", ""))
                rows.append(row)
    return seen, rows


def list_page_params(page):
    """Query parameters for the IE-filtered transaction list."""
    return [
        ("q[transaction_type_in][]", "independent_expenditure"),
        ("q[transaction_type_in][]", "returned_independent_expenditure"),
        ("q[s]", "date desc"),
        ("limit", "100"),   # ask for big pages; site may cap this lower
        ("page", str(page)),
    ]


def parse_list_page(html):
    """
    Pull rows out of the results table.
    Returns a list of dicts with id, filer, transaction_type, payee, date,
    amount, detail_url -- plus a flag for whether the filter looks applied.
    """
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for link in soup.select('a[href*="/public/activities/a_"]'):
        href = link.get("href", "")
        m = re.search(r"/public/activities/(a_[a-z0-9]+)", href)
        if not m:
            continue
        row = link.find_parent("tr")
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")] if row else []
        # Expected column order on the site: Filer | Type | Source/Payee | Date | Amount
        rec = {
            "transaction_id": m.group(1),
            "filer": cells[0] if len(cells) > 0 else link.get_text(strip=True),
            "transaction_type": cells[1] if len(cells) > 1 else "",
            "payee": cells[2] if len(cells) > 2 else "",
            "date": cells[3] if len(cells) > 3 else "",
            "amount": cells[4] if len(cells) > 4 else "",
            "detail_url": BASE + "/public/activities/" + m.group(1),
        }
        records.append(rec)

    # Sanity check: with the filter applied, rows should say Independent Expenditure
    typed = [r for r in records if r["transaction_type"]]
    filter_ok = (not typed) or any(
        "independent" in r["transaction_type"].lower() for r in typed
    )
    return records, filter_ok


def parse_detail_page(html):
    """
    Pull purpose, explanation, and the Target Candidate / Ballot Question
    section from a transaction's detail page.
    """
    soup = BeautifulSoup(html, "html.parser")
    text_pairs = {}

    # The detail pages are label/value lists. Try <dt>/<dd> first,
    # then fall back to scanning label text.
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            text_pairs[dt.get_text(strip=True)] = dd.get_text(" ", strip=True)

    if not text_pairs:
        # Fallback: walk all text lines and pair known labels with what follows
        labels = {"Type", "Date", "Amount", "Purpose", "Explanation of Purpose"}
        lines = [t.strip() for t in soup.get_text("\n").split("\n") if t.strip()]
        for i, line in enumerate(lines[:-1]):
            if line in labels:
                text_pairs[line] = lines[i + 1]

    # Grab everything in the target section as one text blob
    targets = ""
    heading = soup.find(
        string=re.compile(r"Target Candidate or Ballot Question", re.I)
    )
    if heading:
        parts = []
        node = heading.find_parent()
        for sib in node.find_all_next():
            t = sib.get_text(" ", strip=True)
            if not t:
                continue
            if "Contact the Maine Ethics Commission" in t:
                break
            parts.append(t)
            if len(parts) > 40:  # don't wander into the footer
                break
        # de-duplicate nested-element repeats while keeping order
        seen_t = set()
        clean = []
        for p in parts:
            if p not in seen_t:
                seen_t.add(p)
                clean.append(p)
        targets = " | ".join(clean)
        targets = targets.replace("No targets provided", "").strip(" |")

    return {
        "purpose": text_pairs.get("Purpose", ""),
        "explanation": text_pairs.get("Explanation of Purpose", ""),
        "targets": targets or "NONE LISTED",
    }


def main():
    seen, rows = load_existing()
    print(f"Already saved: {len(seen)} records")

    new_rows = []
    page = 1
    stale_streak = 0

    while page <= MAX_PAGES:
        print(f"Fetching list page {page}...")
        try:
            html = get(LIST_URL, params=list_page_params(page))
        except Exception as e:
            print(f"  Could not load page {page}: {e}")
            break

        records, filter_ok = parse_list_page(html)
        if not filter_ok:
            print("  WARNING: The site may have ignored the filter and served "
                  "cached results. Waiting 10 seconds and retrying once...")
            time.sleep(10)
            html = get(LIST_URL, params=list_page_params(page))
            records, filter_ok = parse_list_page(html)
            if not filter_ok:
                with open("debug_list_page.html", "w", encoding="utf-8") as f:
                    f.write(html)
                sys.exit("  Filter still not applied. Saved the page as "
                         "debug_list_page.html -- send this file to Claude.")

        if not records:
            print("  No more records. Done paging.")
            break

        fresh = [r for r in records if r["transaction_id"] not in seen]
        if not fresh:
            stale_streak += 1
            if stale_streak >= 2:
                print("  Reached records we already have. Stopping.")
                break
        else:
            stale_streak = 0

        for rec in fresh:
            print(f"  Getting details: {rec['transaction_id']} "
                  f"({rec['filer'][:40]}, {rec['amount']})")
            try:
                detail_html = get(rec["detail_url"])
                rec.update(parse_detail_page(detail_html))
            except Exception as e:
                print(f"    Problem reading detail page: {e}")
                rec.update({"purpose": "", "explanation": "",
                            "targets": "ERROR - CHECK MANUALLY"})
            rec["first_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            seen.add(rec["transaction_id"])
            new_rows.append(rec)

        page += 1

    if not new_rows:
        print("No new independent expenditures found.")

    all_rows = rows + new_rows
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in all_rows:
            writer.writerow(row)

    print(f"\nAdded {len(new_rows)} new records. "
          f"Total saved: {len(all_rows)}.")
    print(f"Open {CSV_FILE} in Excel or Google Sheets.")


if __name__ == "__main__":
    main()
