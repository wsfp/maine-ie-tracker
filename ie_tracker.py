"""
Maine Independent Expenditure Tracker
-------------------------------------
Collects current-cycle independent expenditure records from
mainecampaignfinancedisclosure.com, gets each record's target
candidate from its detail page, matches candidates to their race
(office + district + party) using the site's candidate list, and
saves everything to ie_tracker.csv.

Safe to run repeatedly: it only fetches records it hasn't seen.
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
    sys.exit("Missing libraries. Run: pip install requests beautifulsoup4")

BASE = "https://www.mainecampaignfinancedisclosure.com"
LIST_URL = BASE + "/public/activities"
CANDIDATES_URL = BASE + "/public/candidates"
CSV_FILE = "ie_tracker.csv"
DELAY_SECONDS = 1.5
MAX_PAGES = 200
CYCLE_START = "2025-01-01"   # ignore anything before this date (prior cycle)
PRIMARY_DATE = "2026-06-09"  # on/before this = Primary, after = General

COLUMNS = [
    "transaction_id", "date", "filer", "transaction_type", "amount",
    "payee", "purpose", "explanation", "target_candidate", "support_oppose",
    "amount_toward_target", "race", "office", "district", "party",
    "detail_url", "first_seen", "phase", "target_as_filed",
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
    params = list(params or [])   # keep duplicate keys (filter checkboxes)
    params.append(("_cb", str(int(time.time() * 1000))))
    resp = session.get(url, params=params, timeout=60)
    resp.raise_for_status()
    time.sleep(DELAY_SECONDS)
    return resp.text


def iso_date(us_date):
    """Turn 07/01/2026 into 2026-07-01 (sortable). Leave odd values alone."""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", us_date or "")
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else (us_date or "")


def money_to_number(text):
    """Turn $7,496.00 into 7496.00 for easy math in spreadsheets."""
    cleaned = re.sub(r"[^\d.]", "", text or "")
    return cleaned or ""


NICKNAMES = {
    "rick": "richard", "dick": "richard", "rich": "richard",
    "bobby": "robert", "bob": "robert", "rob": "robert",
    "mike": "michael", "bill": "william", "will": "william",
    "jim": "james", "jimmy": "james", "tom": "thomas",
    "dan": "daniel", "danny": "daniel", "dave": "david",
    "steve": "steven", "chris": "christopher", "matt": "matthew",
    "tony": "anthony", "ed": "edward", "ted": "edward",
    "joe": "joseph", "ben": "benjamin", "andy": "andrew",
    "drew": "andrew", "chuck": "charles", "charlie": "charles",
    "ken": "kenneth", "kenny": "kenneth", "nick": "nicholas",
    "pat": "patrick", "greg": "gregory", "jeff": "jeffrey",
    "sam": "samuel", "beth": "elizabeth", "liz": "elizabeth",
    "betsy": "elizabeth", "kate": "katherine", "katie": "katherine",
    "kathy": "katherine", "sue": "susan", "peggy": "margaret",
    "meg": "margaret", "jen": "jennifer", "jenny": "jennifer",
    "becky": "rebecca", "vicki": "victoria", "deb": "deborah",
    "debbie": "deborah", "abby": "abigail",
}


def name_key(name):
    """Normalize a candidate name for matching across formats.
    Handles 'Troy Jackson' vs 'Jackson, Troy', middle names, suffixes,
    nicknames (Rick/Richard), and stray 'for Governor' style endings."""
    n = re.sub(r"[.,]", " ", (name or "").lower())
    n = re.sub(r"\bfor (governor|senate|senator|house|representative"
               r"|congress|sheriff|mayor)\b.*", " ", n)
    drop = {"jr", "sr", "ii", "iii", "iv"}
    tokens = [NICKNAMES.get(t, t) for t in n.split() if t and t not in drop]
    return frozenset(tokens)


def build_race_map():
    """Scrape the All Registered Candidates list into
    {name_key: (race, office, district, party)}."""
    print("Building candidate/race map...")
    race_map = {}
    page = 1
    while page <= 50:
        html = get(CANDIDATES_URL, params=[("page", str(page)),
                                           ("limit", "100")])
        soup = BeautifulSoup(html, "html.parser")
        found = 0
        for link in soup.select('a[href*="/public/filers/f_"]'):
            row = link.find_parent("tr")
            if not row:
                continue
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
            if len(cells) < 5:
                continue
            name, _year, office, district, party = cells[0], cells[1], cells[2], cells[3], cells[4]
            race = office
            if district and district != "-":
                race = f"{office} District {district}"
            key = name_key(name)
            if key and key not in race_map:
                race_map[key] = (name, race, office, district, party)
                found += 1
        if found == 0:
            break
        page += 1
    print(f"  Mapped {len(race_map)} candidates.")
    return race_map


def lookup_race(target_name, race_map):
    """Find a candidate's official name and race, tolerating
    name-format differences (Rick/Richard, Last-First order, etc.)."""
    key = name_key(target_name)
    if not key:
        return ("", "", "", "", "")
    if key in race_map:
        return race_map[key]
    # Fall back: allow extra middle names on either side,
    # as long as at least first + last name overlap.
    best = None
    for k, v in race_map.items():
        if (k <= key or key <= k) and len(k & key) >= 2:
            if best is not None:      # two possible matches -> too risky
                return ("", "", "", "", "")
            best = v
    return best or ("", "", "", "", "")


def load_existing():
    seen = set()
    rows = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                seen.add(row.get("transaction_id", ""))
                rows.append(row)
    return seen, rows


def list_page_params(page):
    return [
        ("q[transaction_type_in][]", "independent_expenditure"),
        ("q[transaction_type_in][]", "returned_independent_expenditure"),
        ("q[s]", "date desc"),
        ("limit", "100"),
        ("page", str(page)),
    ]


def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for link in soup.select('a[href*="/public/activities/a_"]'):
        href = link.get("href", "")
        m = re.search(r"/public/activities/(a_[a-z0-9]+)", href)
        if not m:
            continue
        row = link.find_parent("tr")
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")] if row else []
        records.append({
            "transaction_id": m.group(1),
            "filer": cells[0] if len(cells) > 0 else link.get_text(strip=True),
            "transaction_type": cells[1] if len(cells) > 1 else "",
            "payee": cells[2] if len(cells) > 2 else "",
            "date": iso_date(cells[3]) if len(cells) > 3 else "",
            "amount": money_to_number(cells[4]) if len(cells) > 4 else "",
            "detail_url": BASE + "/public/activities/" + m.group(1),
        })
    typed = [r for r in records if r["transaction_type"]]
    filter_ok = (not typed) or any(
        "independent" in r["transaction_type"].lower() for r in typed
    )
    return records, filter_ok


def parse_detail_page(html):
    soup = BeautifulSoup(html, "html.parser")
    text_pairs = {}
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            text_pairs[dt.get_text(strip=True)] = dd.get_text(" ", strip=True)
    if not text_pairs:
        labels = {"Type", "Date", "Amount", "Purpose", "Explanation of Purpose"}
        lines = [t.strip() for t in soup.get_text("\n").split("\n") if t.strip()]
        for i, line in enumerate(lines[:-1]):
            if line in labels:
                text_pairs[line] = lines[i + 1]

    target_list = []
    heading = soup.find(string=re.compile(r"Target Candidate or Ballot Question", re.I))
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
            if len(parts) > 60:
                break
        section = parts[0] if parts else ""
        target_list = re.findall(
            r"Candidate Name (.+?) Amount Spent (\$[\d,.]+) "
            r"Support or Oppose (Support|Oppose)",
            section,
        )
    if not target_list:
        target_list = [("NONE LISTED", "", "")]

    return {
        "purpose": text_pairs.get("Purpose", ""),
        "explanation": text_pairs.get("Explanation of Purpose", ""),
        "target_list": target_list,
    }


def main():
    seen, rows = load_existing()

    # Drop anything from before the current cycle (handles old data too)
    before = len(rows)
    rows = [r for r in rows if iso_date(r.get("date", "")) >= CYCLE_START]
    if before - len(rows):
        print(f"Removed {before - len(rows)} rows from before {CYCLE_START}.")
    for r in rows:
        r["date"] = iso_date(r.get("date", ""))
        r["amount"] = money_to_number(r.get("amount", ""))
        r["amount_toward_target"] = money_to_number(r.get("amount_toward_target", ""))

    print(f"Already saved: {len(seen)} transactions")

    race_map = build_race_map()

    new_rows = []
    page = 1
    stale_streak = 0
    reached_old_records = False

    while page <= MAX_PAGES and not reached_old_records:
        print(f"Fetching list page {page}...")
        try:
            html = get(LIST_URL, params=list_page_params(page))
        except Exception as e:
            print(f"  Could not load page {page}: {e}")
            break

        records, filter_ok = parse_list_page(html)
        if not filter_ok:
            print("  WARNING: filter may not have applied; retrying once...")
            time.sleep(10)
            html = get(LIST_URL, params=list_page_params(page))
            records, filter_ok = parse_list_page(html)
            if not filter_ok:
                with open("debug_list_page.html", "w", encoding="utf-8") as f:
                    f.write(html)
                sys.exit("  Filter still not applied; saved debug_list_page.html")

        if not records:
            print("  No more records. Done paging.")
            break

        # Records are newest-first, so stop once we're past the cycle start
        in_cycle = [r for r in records if r["date"] >= CYCLE_START]
        if len(in_cycle) < len(records):
            reached_old_records = True

        fresh = [r for r in in_cycle if r["transaction_id"] not in seen]
        if not fresh and in_cycle:
            stale_streak += 1
            if stale_streak >= 2:
                print("  Reached records we already have. Stopping.")
                break
        elif fresh:
            stale_streak = 0

        for rec in fresh:
            print(f"  Getting details: {rec['transaction_id']} "
                  f"({rec['filer'][:40]}, {rec['amount']})")
            try:
                detail_html = get(rec["detail_url"])
                details = parse_detail_page(detail_html)
            except Exception as e:
                print(f"    Problem reading detail page: {e}")
                details = {"purpose": "", "explanation": "",
                           "target_list": [("ERROR - CHECK MANUALLY", "", "")]}
            rec["purpose"] = details["purpose"]
            rec["explanation"] = details["explanation"]
            rec["first_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            seen.add(rec["transaction_id"])
            for name, amt, so in details["target_list"]:
                row = dict(rec)
                row["target_candidate"] = name.strip()
                row["support_oppose"] = so
                row["amount_toward_target"] = money_to_number(amt)
                new_rows.append(row)

        page += 1

    all_rows = rows + new_rows

    # (Re)apply the race mapping to every row, old and new, so
    # late-registering candidates get filled in over time.
    unmatched = set()
    for row in all_rows:
        official, race, office, district, party = lookup_race(
            row.get("target_candidate", ""), race_map)
        row["race"], row["office"] = race, office
        row["district"], row["party"] = district, party
        if official and official != row.get("target_candidate"):
            # Unify spellings: keep what the filer wrote for reference,
            # but use the official registered name everywhere.
            row["target_as_filed"] = row.get("target_as_filed") or \
                row.get("target_candidate", "")
            row["target_candidate"] = official
        if not race and row.get("target_candidate") not in ("", "NONE LISTED"):
            unmatched.add(row.get("target_candidate"))
        d = row.get("date", "")
        if d < "2026-01-01":
            row["phase"] = "2025 Special"
        elif d <= PRIMARY_DATE:
            row["phase"] = "Primary"
        else:
            row["phase"] = "General"

    all_rows.sort(key=lambda r: r.get("date", ""), reverse=True)

    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nAdded {len(new_rows)} new rows. Total: {len(all_rows)}.")
    if unmatched:
        print(f"Couldn't match {len(unmatched)} candidate name(s) to a race:")
        for n in sorted(unmatched):
            print(f"  - {n}")


if __name__ == "__main__":
    main()
