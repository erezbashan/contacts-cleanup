#!/usr/bin/env python3
"""
Google Contacts Cleanup Script
==============================
Cross-references an exported Google Contacts CSV against the macOS WhatsApp
Desktop SQLite database to identify contacts with recent interactions.

Contacts with WhatsApp activity in the last N years are kept;
the rest are archived to a separate CSV for safekeeping.

Usage:
    python cleanup_contacts.py contacts.csv [--years 5] [--dry-run] [--min-digits 7]
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
# e.g. "1234" is too short — could match many unrelated numbers.
MIN_DIGITS_DEFAULT = 7

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WhatsApp extraction
# ---------------------------------------------------------------------------


def extract_active_numbers(db_path: str, years: int) -> set[str]:
    """
    Query the macOS WhatsApp Desktop DB for all phone numbers that had
    any interaction (direct message sent/received, or membership in a
    group that had a message) within the last `years` years.

    Returns a set of raw digit strings (e.g. "972523364281").
    """
    if not Path(db_path).exists():
        log.error("WhatsApp database not found at: %s", db_path)
        sys.exit(1)

    threshold = datetime.now() - timedelta(days=years * 365)
    apple_ts = (threshold - CORE_DATA_EPOCH).total_seconds()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    # -----------------------------------------------------------------------
    # 1. Direct 1-on-1 chats: ZFROMJID / ZTOJID with @s.whatsapp.net
    #    These are the most reliable — they contain the actual phone number.
    #
    # 2. Group members: ZWAGROUPMEMBER rows whose group chat session had
    #    a message within the window.  Only @s.whatsapp.net JIDs are useful;
    #    @lid (Linked Identity Device) JIDs are opaque and contain no phone.
    #
    # The join path for groups:
    #   ZWAGROUPMEMBER.ZCHATSESSION -> ZWACHATSESSION.Z_PK (the group)
    #   ZWAMESSAGE.ZCHATSESSION     -> ZWACHATSESSION.Z_PK (same group)
    # -----------------------------------------------------------------------

    query = f"""
    -- Direct messages: sender
    SELECT DISTINCT ZFROMJID AS jid
    FROM ZWAMESSAGE
    WHERE ZMESSAGEDATE > {apple_ts}
      AND ZFROMJID LIKE '%@s.whatsapp.net'

    UNION

    -- Direct messages: recipient
    SELECT DISTINCT ZTOJID AS jid
    FROM ZWAMESSAGE
    WHERE ZMESSAGEDATE > {apple_ts}
      AND ZTOJID LIKE '%@s.whatsapp.net'

    UNION

    -- Direct chats with recent activity (via chat session)
    SELECT DISTINCT cs.ZCONTACTJID AS jid
    FROM ZWACHATSESSION cs
    WHERE cs.ZLASTMESSAGEDATE > {apple_ts}
      AND cs.ZCONTACTJID LIKE '%@s.whatsapp.net'

    UNION

    -- Group members: only @s.whatsapp.net JIDs from groups active in window
    SELECT DISTINCT gm.ZMEMBERJID AS jid
    FROM ZWAGROUPMEMBER gm
    JOIN ZWACHATSESSION cs ON gm.ZCHATSESSION = cs.Z_PK
    WHERE cs.ZLASTMESSAGEDATE > {apple_ts}
      AND gm.ZMEMBERJID LIKE '%@s.whatsapp.net'
    """

    df = pd.read_sql_query(query, conn)
    conn.close()

    # Extract raw digit strings from JIDs like "972523364281@s.whatsapp.net"
    numbers: set[str] = set()
    for jid in df["jid"].dropna():
        digits = re.sub(r"\D", "", jid.split("@")[0])
        if digits:
            numbers.add(digits)

    log.info(
        "Extracted %d unique active phone numbers from WhatsApp (last %d years)",
        len(numbers),
        years,
    )
    return numbers


# ---------------------------------------------------------------------------
# Contact matching
# ---------------------------------------------------------------------------


def normalize_phone(raw: str) -> str:
    """Strip a phone string down to digits only."""
    return re.sub(r"\D", "", str(raw))


def phone_matches(contact_digits: str, active_set: set[str], min_digits: int) -> bool:
    """
    Check whether `contact_digits` matches any number in `active_set`
    using suffix matching (to handle varying country-code prefixes).

    A minimum digit overlap of `min_digits` is enforced to avoid
    false positives from short suffixes.
    """
    if not contact_digits or len(contact_digits) < min_digits:
        return False

    for active in active_set:
        # Compare the shorter suffix against the longer number
        overlap = min(len(contact_digits), len(active))
        if overlap < min_digits:
            continue
        if contact_digits[-overlap:] == active[-overlap:]:
            return True
    return False


def classify_contacts(
    csv_path: str, active_numbers: set[str], min_digits: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Read a Google Contacts CSV and split it into (keep, archive) DataFrames
    based on whether any phone column matches an active WhatsApp number.
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

    def row_is_active(row: pd.Series) -> bool:
        for col in phone_cols:
            val = row[col]
            if val:
                digits = normalize_phone(val)
                if phone_matches(digits, active_numbers, min_digits):
                    return True
        return False

    contacts["_keep"] = contacts.apply(row_is_active, axis=1)

    keep = contacts[contacts["_keep"]].drop(columns=["_keep"])
    archive = contacts[~contacts["_keep"]].drop(columns=["_keep"])

    return keep, archive


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean up Google Contacts using WhatsApp interaction history."
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
        "--dry-run",
        action="store_true",
        help="Print statistics without writing output files.",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for output CSVs (default: current directory).",
    )

    args = parser.parse_args()

    # Step 1: Extract active WhatsApp numbers
    active_numbers = extract_active_numbers(args.whatsapp_db, args.years)

    # Step 2: Classify contacts
    keep, archive = classify_contacts(args.csv, active_numbers, args.min_digits)

    # Step 3: Report
    total = len(keep) + len(archive)
    log.info("=" * 50)
    log.info("Results:")
    log.info("  Total contacts:    %d", total)
    log.info("  Keeping:           %d  (%.1f%%)", len(keep), 100 * len(keep) / max(total, 1))
    log.info("  Archiving:         %d  (%.1f%%)", len(archive), 100 * len(archive) / max(total, 1))
    log.info("=" * 50)

    if args.dry_run:
        log.info("[DRY RUN] No files written.")
        # Show a sample of what would be archived
        if len(archive) > 0:
            name_col = "Name" if "Name" in archive.columns else archive.columns[0]
            sample = archive[name_col].head(20).tolist()
            log.info("Sample contacts to archive: %s", sample)
        return

    # Step 4: Write output
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cleaned_path = out_dir / "cleaned_contacts.csv"
    archived_path = out_dir / "archived_contacts.csv"

    keep.to_csv(cleaned_path, index=False)
    archive.to_csv(archived_path, index=False)

    log.info("Wrote %s  (%d contacts)", cleaned_path, len(keep))
    log.info("Wrote %s  (%d contacts)", archived_path, len(archive))
    log.info("Done. Upload archived_contacts.csv to Google Drive for safekeeping.")


if __name__ == "__main__":
    main()
