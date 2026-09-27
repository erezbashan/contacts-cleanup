#!/usr/bin/env python3
"""
Google Contacts Cleanup Script
==============================
Cross-references an exported Google Contacts CSV against the macOS WhatsApp
Desktop SQLite database to identify contacts with recent interactions.

Contacts with WhatsApp activity within --years are written to cleaned_contacts.csv;
the rest go to archived_contacts.csv. Both files are always written.

Usage:
    python cleanup_contacts.py contacts.csv               # uses 5-year default
    python cleanup_contacts.py contacts.csv --years 3
    python cleanup_contacts.py contacts.csv --output-dir ./output
"""

import argparse
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WHATSAPP_DB_PATH = os.path.expanduser(
    "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
)

# Apple Core Data epoch (NSDate reference date)
CORE_DATA_EPOCH = datetime(2001, 1, 1)

# Phone column pattern in Google Contacts CSV export
PHONE_COLUMN_RE = re.compile(r"^Phone \d+ - Value$")

# Minimum number of digits required for suffix matching to avoid false positives.
MIN_DIGITS_DEFAULT = 7

# Year thresholds printed in the breakdown report
BREAKDOWN_YEARS = [1, 2, 3, 5, 7, 10, 15, 20]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WhatsApp extraction
# ---------------------------------------------------------------------------


def extract_active_numbers_with_dates(db_path: str) -> dict[str, datetime]:
    """
    Query the macOS WhatsApp Desktop DB for all phone numbers and their
    most recent interaction date.

    Returns a dict mapping raw digit string -> latest interaction datetime.
    e.g. {"972523364281": datetime(2025, 3, 15, ...), ...}
    """
    if not Path(db_path).exists():
        log.error("WhatsApp database not found at: %s", db_path)
        sys.exit(1)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    # -----------------------------------------------------------------------
    # Collect JID + latest message date from all sources.
    # We take MAX(date) per contact to find the most recent interaction.
    #
    # Sources:
    #   1. Direct messages: ZFROMJID / ZTOJID (only @s.whatsapp.net)
    #   2. Chat sessions:   ZCONTACTJID with ZLASTMESSAGEDATE
    #   3. Group members:   ZMEMBERJID from groups with recent activity
    #                       (only @s.whatsapp.net — @lid JIDs are opaque)
    #
    # Group member join path:
    #   ZWAGROUPMEMBER.ZCHATSESSION -> ZWACHATSESSION.Z_PK (the group)
    # -----------------------------------------------------------------------

    query = """
    SELECT jid, MAX(latest) AS latest FROM (

        -- Direct messages: sender
        SELECT ZFROMJID AS jid, MAX(ZMESSAGEDATE) AS latest
        FROM ZWAMESSAGE
        WHERE ZFROMJID LIKE '%@s.whatsapp.net'
        GROUP BY ZFROMJID

        UNION ALL

        -- Direct messages: recipient
        SELECT ZTOJID AS jid, MAX(ZMESSAGEDATE) AS latest
        FROM ZWAMESSAGE
        WHERE ZTOJID LIKE '%@s.whatsapp.net'
        GROUP BY ZTOJID

        UNION ALL

        -- Chat sessions (direct, reliable last-message date)
        SELECT ZCONTACTJID AS jid, ZLASTMESSAGEDATE AS latest
        FROM ZWACHATSESSION
        WHERE ZCONTACTJID LIKE '%@s.whatsapp.net'

        UNION ALL

        -- Group members from recently-active groups
        SELECT gm.ZMEMBERJID AS jid, cs.ZLASTMESSAGEDATE AS latest
        FROM ZWAGROUPMEMBER gm
        JOIN ZWACHATSESSION cs ON gm.ZCHATSESSION = cs.Z_PK
        WHERE gm.ZMEMBERJID LIKE '%@s.whatsapp.net'

    ) GROUP BY jid
    """

    df = pd.read_sql_query(query, conn)
    conn.close()

    result: dict[str, datetime] = {}
    for _, row in df.iterrows():
        jid = row["jid"]
        ts = row["latest"]
        if pd.isna(jid) or pd.isna(ts):
            continue
        digits = re.sub(r"\D", "", jid.split("@")[0])
        if digits:
            dt = CORE_DATA_EPOCH + timedelta(seconds=float(ts))
            # Keep the latest date for this number
            if digits not in result or dt > result[digits]:
                result[digits] = dt

    log.info(
        "Extracted %d unique phone numbers with interaction dates from WhatsApp",
        len(result),
    )
    return result


# ---------------------------------------------------------------------------
# Contact matching
# ---------------------------------------------------------------------------


def normalize_phone(raw: str) -> str:
    return re.sub(r"\D", "", str(raw))


def find_latest_interaction(
    contact_digits: str, number_dates: dict[str, datetime], min_digits: int
) -> datetime | None:
    """
    Find the latest WhatsApp interaction date for a phone number using
    suffix matching (handles varying country-code prefixes).
    Enforces a minimum overlap to avoid false positives from short numbers.
    """
    if not contact_digits or len(contact_digits) < min_digits:
        return None

    best: datetime | None = None
    for active, dt in number_dates.items():
        overlap = min(len(contact_digits), len(active))
        if overlap < min_digits:
            continue
        if contact_digits[-overlap:] == active[-overlap:]:
            if best is None or dt > best:
                best = dt
    return best


def classify_contacts(
    csv_path: str, number_dates: dict[str, datetime], min_digits: int, years: int
) -> pd.DataFrame:
    """
    Read a Google Contacts CSV. For each contact, find the latest WhatsApp
    interaction date across all phone columns. Adds three helper columns:
      _latest_interaction  – datetime or None
      _interaction_year    – int year or None
      _keep                – bool
    """
    contacts = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    log.info("Loaded %d contacts from %s", len(contacts), csv_path)

    phone_cols = [c for c in contacts.columns if PHONE_COLUMN_RE.match(c)]
    if not phone_cols:
        log.error(
            "No phone columns found (expected 'Phone N - Value'). "
            "Columns present: %s",
            list(contacts.columns),
        )
        sys.exit(1)
    log.info("Matching against %d phone columns: %s", len(phone_cols), phone_cols)

    cutoff = datetime.now() - timedelta(days=years * 365)
    latest_dates: list[datetime | None] = []

    for _, row in contacts.iterrows():
        best: datetime | None = None
        for col in phone_cols:
            val = row[col]
            if val:
                dt = find_latest_interaction(normalize_phone(val), number_dates, min_digits)
                if dt is not None and (best is None or dt > best):
                    best = dt
        latest_dates.append(best)

    contacts["_latest_interaction"] = latest_dates
    contacts["_interaction_year"] = contacts["_latest_interaction"].apply(
        lambda d: d.year if d else None
    )
    contacts["_keep"] = contacts["_latest_interaction"].apply(
        lambda d: d is not None and d >= cutoff
    )
    return contacts


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_year_breakdown(contacts: pd.DataFrame) -> None:
    now = datetime.now()
    total = len(contacts)
    has_match = contacts["_latest_interaction"].notna().sum()
    no_match = total - has_match

    log.info("")
    log.info("=" * 60)
    log.info("RESULTS")
    log.info("=" * 60)
    log.info("  Total contacts:          %d", total)
    log.info("  With WhatsApp match:     %d", has_match)
    log.info("  No WhatsApp match:       %d  (always archived)", no_match)
    log.info("")
    log.info("  %-30s  %8s  %8s", "Threshold", "Keep", "Archive")
    log.info("  " + "-" * 54)

    for y in BREAKDOWN_YEARS:
        cutoff = now - timedelta(days=y * 365)
        keep_n = contacts["_latest_interaction"].apply(
            lambda d, c=cutoff: d is not None and d >= c
        ).sum()
        log.info(
            "  Last %-3d years  (since %d)   %8d  %8d",
            y,
            cutoff.year,
            keep_n,
            total - keep_n,
        )

    log.info("=" * 60)

    # Histogram by year of last interaction
    year_counts = (
        contacts[contacts["_interaction_year"].notna()]
        .groupby("_interaction_year")
        .size()
        .sort_index()
    )
    if not year_counts.empty:
        log.info("")
        log.info("Last interaction year breakdown:")
        max_count = year_counts.max()
        for year, count in year_counts.items():
            bar = "█" * int(40 * count / max_count)
            log.info("  %d │ %4d  %s", int(year), count, bar)
        log.info("")


def print_archive_sample(archive: pd.DataFrame, n: int = 30) -> None:
    if archive.empty:
        return
    # Try to find a name column
    for candidate in ["Name", "First Name", archive.columns[0]]:
        if candidate in archive.columns:
            name_col = candidate
            break

    log.info("Sample contacts being archived (first %d of %d):", min(n, len(archive)), len(archive))
    for _, row in archive.head(n).iterrows():
        name = row.get(name_col, "").strip() or "(no name)"
        year = row["_interaction_year"]
        tag = f"last seen {int(year)}" if pd.notna(year) else "no WhatsApp match"
        log.info("  - %-38s  [%s]", name, tag)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean up Google Contacts using WhatsApp interaction history."
    )
    parser.add_argument("csv", help="Path to the exported Google Contacts CSV file.")
    parser.add_argument(
        "--years",
        type=int,
        default=5,
        help="Number of years of interaction history to keep (default: 5).",
    )
    parser.add_argument(
        "--min-digits",
        type=int,
        default=MIN_DIGITS_DEFAULT,
        help=f"Minimum digit overlap for phone suffix matching (default: {MIN_DIGITS_DEFAULT}).",
    )
    parser.add_argument(
        "--whatsapp-db",
        default=WHATSAPP_DB_PATH,
        help="Path to WhatsApp ChatStorage.sqlite (auto-detected on macOS).",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for output CSVs (default: current directory).",
    )

    args = parser.parse_args()

    # 1. Extract WhatsApp interactions
    number_dates = extract_active_numbers_with_dates(args.whatsapp_db)

    # 2. Classify contacts
    contacts = classify_contacts(args.csv, number_dates, args.min_digits, args.years)

    keep = contacts[contacts["_keep"]]
    archive = contacts[~contacts["_keep"]]

    # 3. Print breakdown (always)
    print_year_breakdown(contacts)
    log.info("With --years %d: keeping %d, archiving %d", args.years, len(keep), len(archive))
    log.info("")
    print_archive_sample(archive)

    # 4. Write output files
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    internal_cols = ["_latest_interaction", "_interaction_year", "_keep"]
    keep.drop(columns=internal_cols).to_csv(out_dir / "cleaned_contacts.csv", index=False)
    archive.drop(columns=internal_cols).to_csv(out_dir / "archived_contacts.csv", index=False)

    log.info("")
    log.info("Files written:")
    log.info("  cleaned_contacts.csv   → %d contacts to re-import into Google", len(keep))
    log.info("  archived_contacts.csv  → %d contacts  (upload to Drive as backup)", len(archive))
    log.info("")
    log.info("NEXT STEPS:")
    log.info("  1. Review archived_contacts.csv — restore any keepers manually")
    log.info("  2. Upload archived_contacts.csv to Google Drive as a backup")
    log.info("  3. contacts.google.com → select all → delete (goes to Trash, 30-day safety net)")
    log.info("  4. contacts.google.com → Import → cleaned_contacts.csv")


if __name__ == "__main__":
    main()
