#!/usr/bin/env python3
"""
Google Contacts Cleanup Script
==============================
Cross-references an exported Google Contacts CSV against:
1. macOS WhatsApp Desktop SQLite databases (messages + calls)
2. Google Takeout Gmail mbox archive (streaming directly without disk extraction)

Contacts with WhatsApp or Email activity within --years are written to
cleaned_contacts.csv; the rest go to archived_contacts.csv.
Both files are always written.

Usage:
    python cleanup_contacts.py contacts.csv               # uses 5-year default
    python cleanup_contacts.py contacts.csv --years 3
    python cleanup_contacts.py contacts.csv --output-dir ./output
"""

import argparse
from email.utils import parsedate_to_datetime
import glob
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta

try:
    from dateutil import parser as dateutil_parser
except ImportError:
    dateutil_parser = None

import pandas as pd

# ---------------------------------------------------------------------------
# Constants & Defaults
# ---------------------------------------------------------------------------

WHATSAPP_CHAT_DB_PATH = os.path.expanduser(
    "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
)

WHATSAPP_CALL_DB_PATH = os.path.expanduser(
    "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/CallHistory.sqlite"
)

EMAIL_CACHE_FILE = "email_interactions.json"

# Apple Core Data epoch (NSDate reference date)
CORE_DATA_EPOCH = datetime(2001, 1, 1)

# Column patterns in Google Contacts CSV export
PHONE_COLUMN_RE = re.compile(r"^Phone \d+ - Value$")
EMAIL_COLUMN_RE = re.compile(r"^E-mail \d+ - Value$")

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
# Helpers
# ---------------------------------------------------------------------------


def apple_ts_to_datetime(ts: float) -> datetime | None:
    try:
        dt = CORE_DATA_EPOCH + timedelta(seconds=float(ts))
        now_year = datetime.now().year
        if dt.year < 1990 or dt.year > now_year + 1:
            return None
        return dt
    except Exception:
        return None


def jid_to_digits(jid: str) -> str:
    """Extract the phone number digits from a @s.whatsapp.net JID."""
    return re.sub(r"\D", "", jid.split("@")[0])


def merge_into(result: dict[str, datetime], key: str, dt: datetime | None) -> None:
    """Keep the latest date for each key (phone digits or email)."""
    if key and dt and (key not in result or dt > result[key]):
        result[key] = dt


def parse_email_date(date_str: str) -> datetime | None:
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
    except Exception:
        if dateutil_parser:
            try:
                dt = dateutil_parser.parse(date_str)
            except Exception:
                return None
        else:
            return None

    if dt is not None:
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        now_year = datetime.now().year
        if dt.year < 1990 or dt.year > now_year + 1:
            return None
    return dt


# ---------------------------------------------------------------------------
# WhatsApp Messages Extraction (ChatStorage.sqlite)
# ---------------------------------------------------------------------------


def extract_from_messages(chat_db_path: str) -> tuple[dict[str, datetime], dict[str, str]]:
    """
    Extract phone numbers + latest interaction date from WhatsApp messages.
    Returns (phone_number -> latest_date, lid -> phone_digits).
    """
    if not Path(chat_db_path).exists():
        log.error("WhatsApp ChatStorage not found at: %s", chat_db_path)
        sys.exit(1)

    conn = sqlite3.connect(f"file:{chat_db_path}?mode=ro", uri=True)

    query = """
    SELECT jid, MAX(latest) AS latest FROM (
        SELECT ZFROMJID AS jid, MAX(ZMESSAGEDATE) AS latest
        FROM ZWAMESSAGE
        WHERE ZFROMJID LIKE '%@s.whatsapp.net'
        GROUP BY ZFROMJID

        UNION ALL

        SELECT ZTOJID AS jid, MAX(ZMESSAGEDATE) AS latest
        FROM ZWAMESSAGE
        WHERE ZTOJID LIKE '%@s.whatsapp.net'
        GROUP BY ZTOJID

        UNION ALL

        SELECT ZCONTACTJID AS jid, ZLASTMESSAGEDATE AS latest
        FROM ZWACHATSESSION
        WHERE ZCONTACTJID LIKE '%@s.whatsapp.net'

        UNION ALL

        SELECT gm.ZMEMBERJID AS jid, cs.ZLASTMESSAGEDATE AS latest
        FROM ZWAGROUPMEMBER gm
        JOIN ZWACHATSESSION cs ON gm.ZCHATSESSION = cs.Z_PK
        WHERE gm.ZMEMBERJID LIKE '%@s.whatsapp.net'
    ) GROUP BY jid
    """

    df = pd.read_sql_query(query, conn)

    # Build a LID -> phone mapping from ZWACHATSESSION for use in call extraction
    lid_map_df = pd.read_sql_query(
        """
        SELECT ZCONTACTJID AS lid, ZCONTACTIDENTIFIER AS phone
        FROM ZWACHATSESSION
        WHERE ZCONTACTJID LIKE '%@lid'
          AND ZCONTACTIDENTIFIER LIKE '%@s.whatsapp.net'
        """,
        conn,
    )
    conn.close()

    lid_to_digits: dict[str, str] = {}
    for _, row in lid_map_df.iterrows():
        if pd.notna(row["lid"]) and pd.notna(row["phone"]):
            lid_to_digits[row["lid"]] = jid_to_digits(row["phone"])

    result: dict[str, datetime] = {}
    for _, row in df.iterrows():
        if pd.isna(row["jid"]) or pd.isna(row["latest"]):
            continue
        digits = jid_to_digits(row["jid"])
        if digits:
            dt = apple_ts_to_datetime(row["latest"])
            merge_into(result, digits, dt)

    log.info("  WhatsApp messages:  %d unique numbers", len(result))
    return result, lid_to_digits


# ---------------------------------------------------------------------------
# WhatsApp Calls Extraction (CallHistory.sqlite)
# ---------------------------------------------------------------------------


def extract_from_calls(
    call_db_path: str, lid_to_digits: dict[str, str]
) -> dict[str, datetime]:
    """Extract phone numbers + latest call date from WhatsApp CallHistory.sqlite."""
    if not Path(call_db_path).exists():
        log.warning("WhatsApp CallHistory not found at: %s — skipping calls", call_db_path)
        return {}

    conn = sqlite3.connect(f"file:{call_db_path}?mode=ro", uri=True)
    result: dict[str, datetime] = {}

    try:
        df = pd.read_sql_query(
            "SELECT ZFROMJIDSTRING AS jid, ZDATE AS ts FROM ZWAJOINABLECALLEVENT "
            "WHERE ZFROMJIDSTRING IS NOT NULL AND ZDATE IS NOT NULL",
            conn,
        )
        for _, row in df.iterrows():
            jid = row["jid"]
            digits = jid_to_digits(jid) if "@s.whatsapp.net" in jid else lid_to_digits.get(jid)
            if digits:
                merge_into(result, digits, apple_ts_to_datetime(row["ts"]))
    except Exception as e:
        log.debug("ZWAJOINABLECALLEVENT query failed: %s", e)

    try:
        df = pd.read_sql_query(
            """
            SELECT p.ZJIDSTRING AS jid, e.ZDATE AS ts
            FROM ZWACDCALLEVENTPARTICIPANT p
            JOIN ZWACDCALLEVENT e ON p.Z1PARTICIPANTS = e.Z_PK
            WHERE p.ZJIDSTRING IS NOT NULL AND e.ZDATE IS NOT NULL
            """,
            conn,
        )
        for _, row in df.iterrows():
            jid = row["jid"]
            digits = jid_to_digits(jid) if "@s.whatsapp.net" in jid else lid_to_digits.get(jid)
            if digits:
                merge_into(result, digits, apple_ts_to_datetime(row["ts"]))
    except Exception as e:
        log.debug("ZWACDCALLEVENTPARTICIPANT query failed: %s", e)

    conn.close()
    log.info("  WhatsApp calls:     %d unique numbers", len(result))
    return result


def extract_all_whatsapp_numbers(
    chat_db_path: str, call_db_path: str
) -> dict[str, datetime]:
    """Merge message and call interaction data into a single number -> date map."""
    log.info("Extracting WhatsApp interaction data...")
    message_dates, lid_to_digits = extract_from_messages(chat_db_path)
    call_dates = extract_from_calls(call_db_path, lid_to_digits)

    combined = dict(message_dates)
    for digits, dt in call_dates.items():
        merge_into(combined, digits, dt)

    log.info("  Total WhatsApp:     %d unique numbers", len(combined))
    return combined


# ---------------------------------------------------------------------------
# Google Takeout Email Extraction (Streaming mbox directly from tgz)
# ---------------------------------------------------------------------------


def find_takeout_archive() -> str | None:
    """Find any large takeout tgz or mbox file in the current directory."""
    candidates = glob.glob("takeout-*.tgz") + glob.glob("takeout-*.tar.gz") + glob.glob("*.mbox")
    valid = [c for c in candidates if os.path.getsize(c) > 10 * 1024 * 1024]
    if valid:
        # Pick the largest archive
        return max(valid, key=os.path.getsize)
    return None


def extract_email_interactions(
    archive_path: str | None,
    target_emails: set[str],
    cache_path: str = EMAIL_CACHE_FILE,
    rebuild_cache: bool = False,
) -> dict[str, datetime]:
    """
    Extract email interaction dates from a cached JSON file or by streaming
    a Google Takeout tgz/mbox archive directly without unpacking to disk.
    """
    if not rebuild_cache and os.path.exists(cache_path):
        log.info("Loading email interaction history from cache: %s", cache_path)
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            result = {k: datetime.fromisoformat(v) for k, v in data.items()}
            log.info("  Loaded %d email interaction dates from cache", len(result))
            return result
        except Exception as e:
            log.warning("Failed to load cache %s (%s). Rebuilding from archive...", cache_path, e)

    if not archive_path or not os.path.exists(archive_path):
        log.info("No Takeout mail archive found (%s). Skipping email interactions.", archive_path)
        return {}

    archive_size_gb = os.path.getsize(archive_path) / (1024 ** 3)
    log.info(
        "Streaming mail archive: %s (%.1f GB) directly without unpacking to save disk...",
        archive_path,
        archive_size_gb,
    )

    if archive_path.endswith((".tgz", ".tar.gz")):
        cmd = ["tar", "-xzOf", archive_path]
    elif archive_path.endswith(".mbox"):
        cmd = ["cat", archive_path]
    else:
        cmd = ["tar", "-xzOf", archive_path]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=2 * 1024 * 1024,
    )

    email_dates: dict[str, datetime] = {}
    msg_count = 0
    start_time = time.time()

    in_header = False
    in_relevant_header = False
    current_date_str: str | None = None
    current_emails: set[str] = set()
    is_spam = False

    email_re = re.compile(rb"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")

    def finish_message():
        nonlocal current_date_str, current_emails, is_spam
        if not is_spam and current_date_str and current_emails:
            dt = parse_email_date(current_date_str)
            if dt:
                for em in current_emails:
                    merge_into(email_dates, em, dt)
        current_date_str = None
        current_emails = set()
        is_spam = False

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if not in_header:
                if line.startswith(b"From "):
                    in_header = True
                    in_relevant_header = False
                    msg_count += 1
                    if msg_count % 25000 == 0:
                        elapsed = time.time() - start_time
                        rate = msg_count / max(elapsed, 0.1)
                        log.info(
                            "  Scanned %d messages (%.0f msg/s) | %d contacts matched...",
                            msg_count,
                            rate,
                            len(email_dates),
                        )
            else:
                if line in (b"\n", b"\r\n"):
                    finish_message()
                    in_header = False
                    in_relevant_header = False
                    continue

                lower_line = line.lower()
                if lower_line.startswith(b"date:"):
                    in_relevant_header = False
                    current_date_str = line[5:].decode("utf-8", errors="ignore").strip()
                elif lower_line.startswith(b"x-gmail-labels:"):
                    in_relevant_header = False
                    if b"spam" in lower_line:
                        is_spam = True
                elif lower_line.startswith((b"from:", b"to:", b"cc:", b"bcc:")):
                    in_relevant_header = True
                    for match in email_re.findall(lower_line):
                        em = match.decode("ascii", errors="ignore")
                        if em in target_emails:
                            current_emails.add(em)
                elif in_relevant_header and line[0:1] in (b" ", b"\t"):
                    for match in email_re.findall(lower_line):
                        em = match.decode("ascii", errors="ignore")
                        if em in target_emails:
                            current_emails.add(em)
                elif line[0:1] not in (b" ", b"\t"):
                    in_relevant_header = False

        proc.wait()
        finish_message()
    except Exception as e:
        log.error("Error streaming mail archive: %s", e)
        if proc.poll() is None:
            proc.kill()

    elapsed = time.time() - start_time
    log.info(
        "  Finished scanning %d messages in %.1fs. Matched %d contact emails.",
        msg_count,
        elapsed,
        len(email_dates),
    )

    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({k: v.isoformat() for k, v in email_dates.items()}, f, indent=2)
        log.info("  Cached email interactions to %s", cache_path)
    except Exception as e:
        log.warning("  Could not save email cache: %s", e)

    return email_dates


# ---------------------------------------------------------------------------
# Contact Matching
# ---------------------------------------------------------------------------


def normalize_phone(raw: str) -> str:
    return re.sub(r"\D", "", str(raw))


def find_latest_phone_interaction(
    contact_digits: str, number_dates: dict[str, datetime], min_digits: int
) -> datetime | None:
    """Find the latest interaction date for a phone number using suffix matching."""
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
    csv_path: str,
    number_dates: dict[str, datetime],
    email_dates: dict[str, datetime],
    min_digits: int,
    years: int,
) -> pd.DataFrame:
    """
    Read Google Contacts CSV and classify each contact based on phone and email
    interactions.
    """
    contacts = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    log.info("Loaded %d contacts from %s", len(contacts), csv_path)

    phone_cols = [c for c in contacts.columns if PHONE_COLUMN_RE.match(c)]
    email_cols = [c for c in contacts.columns if EMAIL_COLUMN_RE.match(c)]

    log.info("Matching against %d phone columns: %s", len(phone_cols), phone_cols)
    log.info("Matching against %d email columns: %s", len(email_cols), email_cols)

    cutoff = datetime.now() - timedelta(days=years * 365)

    latest_dates: list[datetime | None] = []
    sources: list[str | None] = []

    for _, row in contacts.iterrows():
        # Check phone
        best_phone: datetime | None = None
        for col in phone_cols:
            val = row[col]
            if val:
                dt = find_latest_phone_interaction(normalize_phone(val), number_dates, min_digits)
                if dt is not None and (best_phone is None or dt > best_phone):
                    best_phone = dt

        # Check email
        best_email: datetime | None = None
        for col in email_cols:
            val = row[col]
            if val:
                em = val.strip().lower()
                dt = email_dates.get(em)
                if dt is not None and (best_email is None or dt > best_email):
                    best_email = dt

        # Combine
        combined_dates = [d for d in [best_phone, best_email] if d is not None]
        best_dt = max(combined_dates) if combined_dates else None
        latest_dates.append(best_dt)

        if best_phone and best_email:
            sources.append("both")
        elif best_phone:
            sources.append("whatsapp")
        elif best_email:
            sources.append("email")
        else:
            sources.append(None)

    contacts["_latest_interaction"] = latest_dates
    contacts["_interaction_year"] = contacts["_latest_interaction"].apply(
        lambda d: d.year if d else None
    )
    contacts["_source"] = sources
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
    log.info("=" * 65)
    log.info("INTERACTION THRESHOLD BREAKDOWN")
    log.info("=" * 65)
    log.info("  Total contacts:                 %d", total)
    log.info("  With any active interaction:    %d", has_match)
    log.info("  No interaction (archived):      %d", no_match)
    log.info("")
    log.info("  %-30s  %8s  %8s", "Threshold", "Keep", "Archive")
    log.info("  " + "-" * 59)

    for y in BREAKDOWN_YEARS:
        cutoff = now - timedelta(days=y * 365)
        keep_n = contacts["_latest_interaction"].apply(
            lambda d, c=cutoff: d is not None and d >= c
        ).sum()
        log.info(
            "  Last %-3d years  (since %d)     %8d  %8d",
            y, cutoff.year, keep_n, total - keep_n,
        )

    log.info("=" * 65)

    year_counts = (
        contacts[contacts["_interaction_year"].notna()]
        .groupby("_interaction_year")
        .size()
        .sort_index()
    )
    if not year_counts.empty:
        log.info("")
        log.info("Latest interaction year distribution:")
        max_count = year_counts.max()
        for year, count in year_counts.items():
            bar = "█" * int(40 * count / max_count)
            log.info("  %d │ %4d  %s", int(year), count, bar)
        log.info("")


def print_archive_sample(archive: pd.DataFrame, n: int = 20) -> None:
    if archive.empty:
        return
    name_col = next(
        (c for c in ["Name", "First Name"] if c in archive.columns),
        archive.columns[0],
    )
    log.info("Sample contacts being archived (first %d of %d):", min(n, len(archive)), len(archive))
    for _, row in archive.head(n).iterrows():
        name = row.get(name_col, "").strip() or "(no name)"
        year = row["_interaction_year"]
        tag = f"last seen {int(year)}" if pd.notna(year) else "no interaction"
        log.info("  - %-38s  [%s]", name, tag)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean up Google Contacts using WhatsApp (messages + calls) and Gmail interactions."
    )
    parser.add_argument("csv", help="Path to exported Google Contacts CSV file.")
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
        default=WHATSAPP_CHAT_DB_PATH,
        help="Path to WhatsApp ChatStorage.sqlite.",
    )
    parser.add_argument(
        "--call-db",
        default=WHATSAPP_CALL_DB_PATH,
        help="Path to WhatsApp CallHistory.sqlite.",
    )
    parser.add_argument(
        "--takeout-archive",
        default=None,
        help="Path to Google Takeout mail archive (.tgz or .mbox). Auto-detected if omitted.",
    )
    parser.add_argument(
        "--email-cache",
        default=EMAIL_CACHE_FILE,
        help=f"Path to cache file for extracted email dates (default: {EMAIL_CACHE_FILE}).",
    )
    parser.add_argument(
        "--rebuild-email-cache",
        action="store_true",
        help="Force re-scanning the Takeout archive even if cache exists.",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for output CSVs (default: current directory).",
    )

    args = parser.parse_args()

    # 1. Read contacts to gather all target emails for fast scanning
    raw_contacts = pd.read_csv(args.csv, dtype=str, keep_default_na=False)
    target_emails: set[str] = set()
    for col in [c for c in raw_contacts.columns if EMAIL_COLUMN_RE.match(c)]:
        for val in raw_contacts[col]:
            if val and "@" in val:
                target_emails.add(val.strip().lower())
    log.info("Found %d unique email addresses to cross-reference across contacts", len(target_emails))

    # 2. Extract WhatsApp messages + calls
    number_dates = extract_all_whatsapp_numbers(args.whatsapp_db, args.call_db)

    # 3. Extract Email interactions
    takeout_path = args.takeout_archive or find_takeout_archive()
    email_dates = extract_email_interactions(
        takeout_path,
        target_emails,
        cache_path=args.email_cache,
        rebuild_cache=args.rebuild_email_cache,
    )

    # 4. Classify contacts
    contacts = classify_contacts(
        args.csv, number_dates, email_dates, args.min_digits, args.years
    )

    keep = contacts[contacts["_keep"]]
    archive = contacts[~contacts["_keep"]]

    # 5. Reporting
    print_year_breakdown(contacts)

    # Source breakdown of kept contacts
    source_counts = keep["_source"].value_counts().to_dict()
    log.info("Kept contacts breakdown (--years %d):", args.years)
    log.info("  Total KEPT:       %d contacts (%.1f%%)", len(keep), 100 * len(keep) / len(contacts))
    log.info("    via WhatsApp:   %d", source_counts.get("whatsapp", 0))
    log.info("    via Email:      %d", source_counts.get("email", 0))
    log.info("    via Both:       %d", source_counts.get("both", 0))
    log.info("  Total ARCHIVED:   %d contacts (%.1f%%)", len(archive), 100 * len(archive) / len(contacts))
    log.info("")

    print_archive_sample(archive)

    # 6. Write output files
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    internal_cols = ["_latest_interaction", "_interaction_year", "_source", "_keep"]
    cleaned_path = out_dir / "cleaned_contacts.csv"
    archived_path = out_dir / "archived_contacts.csv"

    keep.drop(columns=internal_cols).to_csv(cleaned_path, index=False)
    archive.drop(columns=internal_cols).to_csv(archived_path, index=False)

    log.info("")
    log.info("Files written:")
    log.info("  %s  (%d contacts to re-import into Google)", cleaned_path, len(keep))
    log.info("  %s  (%d contacts — upload to Drive as backup)", archived_path, len(archive))
    log.info("")
    log.info("NEXT STEPS:")
    log.info("  1. Review archived_contacts.csv to confirm no important contacts are missed")
    log.info("  2. Upload archived_contacts.csv to Google Drive for safekeeping")
    log.info("  3. contacts.google.com -> select all -> delete (held in Trash for 30 days)")
    log.info("  4. contacts.google.com -> Import -> cleaned_contacts.csv")


if __name__ == "__main__":
    main()
