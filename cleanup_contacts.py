#!/usr/bin/env python3
"""
Google Contacts Cleanup Script
==============================
Cross-references an exported Google Contacts CSV against the macOS WhatsApp
Desktop SQLite database to identify contacts with recent interactions.

Contacts with WhatsApp activity in the specified window are kept;
the rest are archived to a separate CSV for safekeeping.

Usage:
    python cleanup_contacts.py contacts.csv                    # dry run (default)
    python cleanup_contacts.py contacts.csv --execute          # actually write files
    python cleanup_contacts.py contacts.csv --years 3          # custom window
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

# Year thresholds for the breakdown report
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
    # -----------------------------------------------------------------------

    query = """
    -- Direct messages: sender
    SELECT ZFROMJID AS jid, MAX(ZMESSAGEDATE) AS latest
    FROM ZWAMESSAGE
    WHERE ZFROMJID LIKE '%@s.whatsapp.net'
    GROUP BY ZFROMJID

    UNION

    -- Direct messages: recipient
    SELECT ZTOJID AS jid, MAX(ZMESSAGEDATE) AS latest
    FROM ZWAMESSAGE
    WHERE ZTOJID LIKE '%@s.whatsapp.net'
    GROUP BY ZTOJID

    UNION

    -- Chat sessions with last message date
    SELECT ZCONTACTJID AS jid, ZLASTMESSAGEDATE AS latest
    FROM ZWACHATSESSION
    WHERE ZCONTACTJID LIKE '%@s.whatsapp.net'

    UNION

    -- Group members from groups — use the group's last message date
    SELECT gm.ZMEMBERJID AS jid, cs.ZLASTMESSAGEDATE AS latest
    FROM ZWAGROUPMEMBER gm
    JOIN ZWACHATSESSION cs ON gm.ZCHATSESSION = cs.Z_PK
    WHERE gm.ZMEMBERJID LIKE '%@s.whatsapp.net'
    """

    df = pd.read_sql_query(query, conn)
    conn.close()

    # Aggregate: for each phone number, keep the latest date across all sources
    number_dates: dict[str, float] = {}
    for _, row in df.iterrows():
        jid = row["jid"]
        ts = row["latest"]
        if pd.isna(jid) or pd.isna(ts):
            continue
        digits = re.sub(r"\D", "", jid.split("@")[0])
        if digits:
            number_dates[digits] = max(number_dates.get(digits, 0), ts)

    # Convert Apple timestamps to datetime
    result: dict[str, datetime] = {}
    for digits, apple_ts in number_dates.items():
        result[digits] = CORE_DATA_EPOCH + timedelta(seconds=apple_ts)

    log.info(
        "Extracted %d unique phone numbers with interaction dates from WhatsApp",
        len(result),
    )
    return result


# ---------------------------------------------------------------------------
# Contact matching
# ---------------------------------------------------------------------------


def normalize_phone(raw: str) -> str:
    """Strip a phone string down to digits only."""
    return re.sub(r"\D", "", str(raw))


def find_latest_interaction(
    contact_digits: str, number_dates: dict[str, datetime], min_digits: int
) -> datetime | None:
    """
    Find the latest WhatsApp interaction date for a phone number
    using suffix matching (to handle varying country-code prefixes).

    Returns the latest interaction datetime, or None if no match.
    """
    if not contact_digits or len(contact_digits) < min_digits:
        return None

    best_date: datetime | None = None
    for active, dt in number_dates.items():
        overlap = min(len(contact_digits), len(active))
        if overlap < min_digits:
            continue
        if contact_digits[-overlap:] == active[-overlap:]:
            if best_date is None or dt > best_date:
                best_date = dt
    return best_date


def classify_contacts(
    csv_path: str, number_dates: dict[str, datetime], min_digits: int, years: int
) -> pd.DataFrame:
    """
    Read a Google Contacts CSV. For each contact, find the latest WhatsApp
    interaction date across all its phone columns. Returns the DataFrame
    with added columns: _latest_interaction, _interaction_year, _keep.
    """
    contacts = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    log.info("Loaded %d contacts from %s", len(contacts), csv_path)

    # Find all phone-value columns
    phone_cols = [c for c in contacts.columns if PHONE_COLUMN_RE.match(c)]
    if not phone_cols:
        log.error(
            "No phone columns found (expected 'Phone N - Value'). "
            "Columns present: %s",
            list(contacts.columns),
        )
        sys.exit(1)

    log.info("Found %d phone columns: %s", len(phone_cols), phone_cols)

    # Check for any date-related columns (Created/Modified — unlikely in CSV)
    date_cols = [
        c
        for c in contacts.columns
        if any(
            kw in c.lower()
            for kw in ["created", "modified", "updated", "last changed"]
        )
    ]
    if date_cols:
        log.info("Found date columns in CSV (will use for retention): %s", date_cols)
    else:
        log.info(
            "No Created/Modified date columns in CSV "
            "(Google Contacts CSV doesn't include them — "
            "they're only available via the People API)"
        )

    # For each contact, find the latest interaction date
    cutoff = datetime.now() - timedelta(days=years * 365)
    latest_dates: list[datetime | None] = []

    for _, row in contacts.iterrows():
        best: datetime | None = None
        for col in phone_cols:
            val = row[col]
            if val:
                digits = normalize_phone(val)
                dt = find_latest_interaction(digits, number_dates, min_digits)
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
    """
    Print a table showing how many contacts would be kept at each
    year threshold.
    """
    now = datetime.now()
    total = len(contacts)
    has_phone = contacts["_latest_interaction"].notna().sum()
    no_match = total - has_phone

    log.info("")
    log.info("=" * 60)
    log.info("YEAR-BY-YEAR BREAKDOWN")
    log.info("=" * 60)
    log.info("  Total contacts:          %d", total)
    log.info("  With WhatsApp match:     %d", has_phone)
    log.info("  No WhatsApp match:       %d  (will be archived)", no_match)
    log.info("-" * 60)
    log.info("  %-30s  %8s  %8s", "Threshold", "Keep", "Archive")
    log.info("-" * 60)

    for y in BREAKDOWN_YEARS:
        cutoff = now - timedelta(days=y * 365)
        keep_count = contacts["_latest_interaction"].apply(
            lambda d, c=cutoff: d is not None and d >= c
        ).sum()
        archive_count = total - keep_count
        log.info(
            "  Last %-2d years (since %d)    %8d  %8d",
            y,
            cutoff.year,
            keep_count,
            archive_count,
        )

    log.info("=" * 60)

    # Also show the distribution by year of last interaction
    year_counts = (
        contacts[contacts["_interaction_year"].notna()]
        .groupby("_interaction_year")
        .size()
        .sort_index()
    )
    if not year_counts.empty:
        log.info("")
        log.info("Last interaction year distribution:")
        for year, count in year_counts.items():
            bar = "█" * min(int(count / 2), 50)
            log.info("  %d: %4d  %s", int(year), count, bar)
        log.info("")


def print_archive_sample(archive: pd.DataFrame, n: int = 30) -> None:
    """Show a sample of contacts that would be archived."""
    if archive.empty:
        return

    name_col = "Name" if "Name" in archive.columns else archive.columns[0]
    log.info("Sample contacts to ARCHIVE (first %d):", min(n, len(archive)))
    for _, row in archive.head(n).iterrows():
        name = row[name_col] if row[name_col] else "(no name)"
        year = row["_interaction_year"]
        year_str = f"last seen {int(year)}" if pd.notna(year) else "no WhatsApp match"
        log.info("  - %-35s  [%s]", name, year_str)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean up Google Contacts using WhatsApp interaction history.",
        epilog=(
            "By default, runs in DRY RUN mode. "
            "Use --execute to actually write output files."
        ),
    )
    parser.add_argument(
        "csv",
        help="Path to the exported Google Contacts CSV file.",
    )
    parser.add_argument(
        "--years",
        type=int,
        default=5,
        help="Number of years of interaction history to consider (default: 5).",
    )
    parser.add_argument(
        "--min-digits",
        type=int,
        default=MIN_DIGITS_DEFAULT,
        help=(
            f"Minimum digit overlap for phone suffix matching "
            f"(default: {MIN_DIGITS_DEFAULT})."
        ),
    )
    parser.add_argument(
        "--whatsapp-db",
        default=WHATSAPP_DB_PATH,
        help="Path to the WhatsApp ChatStorage.sqlite (auto-detected on macOS).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually write output files (default is dry run).",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for output CSVs (default: current directory).",
    )

    args = parser.parse_args()

    if not args.execute:
        log.info("*** DRY RUN MODE (use --execute to write files) ***")
        log.info("")

    # Step 1: Extract WhatsApp interaction data (with dates)
    number_dates = extract_active_numbers_with_dates(args.whatsapp_db)

    # Step 2: Classify contacts
    contacts = classify_contacts(args.csv, number_dates, args.min_digits, args.years)

    keep = contacts[contacts["_keep"]]
    archive = contacts[~contacts["_keep"]]

    # Step 3: Year-by-year breakdown (always shown)
    print_year_breakdown(contacts)

    # Step 4: Summary for the chosen threshold
    total = len(contacts)
    log.info("With --years %d:", args.years)
    log.info(
        "  KEEP:    %d contacts  (%.1f%%)",
        len(keep),
        100 * len(keep) / max(total, 1),
    )
    log.info(
        "  ARCHIVE: %d contacts  (%.1f%%)",
        len(archive),
        100 * len(archive) / max(total, 1),
    )
    log.info("")

    # Show sample of archived contacts
    print_archive_sample(archive)

    if not args.execute:
        log.info("")
        log.info("*** DRY RUN — no files written. ***")
        log.info("*** Re-run with --execute to write output files. ***")
        return

    # Step 5: Write output (only in execute mode)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Drop internal columns before writing
    internal_cols = ["_latest_interaction", "_interaction_year", "_keep"]
    keep_out = keep.drop(columns=internal_cols)
    archive_out = archive.drop(columns=internal_cols)

    cleaned_path = out_dir / "cleaned_contacts.csv"
    archived_path = out_dir / "archived_contacts.csv"

    keep_out.to_csv(cleaned_path, index=False)
    archive_out.to_csv(archived_path, index=False)

    log.info("")
    log.info("Output files written:")
    log.info("  %s  (%d contacts to keep)", cleaned_path, len(keep_out))
    log.info("  %s  (%d contacts archived)", archived_path, len(archive_out))
    log.info("")
    log.info("NEXT STEPS:")
    log.info("  1. Review archived_contacts.csv to make sure nothing important is lost")
    log.info("  2. Upload archived_contacts.csv to Google Drive as a backup")
    log.info("  3. Go to contacts.google.com -> select all -> delete")
    log.info("  4. Import cleaned_contacts.csv via contacts.google.com -> Import")
    log.info(
        "  5. Deleted contacts stay in Google Contacts Trash for 30 days as a safety net"
    )


if __name__ == "__main__":
    main()
