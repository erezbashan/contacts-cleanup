#!/usr/bin/env python3
"""
Google Contacts Cleanup & Smart Merge Script
===========================================
1. Consolidates & deduplicates contacts across:
   - Main contacts (contacts.csv)
   - Auto-generated contacts (other_contacts.csv from Google Contacts)
   Resolves reversed names (First Last vs Last First), email-as-name entries,
   shared mobile numbers, and multiple fragmented email addresses.

2. Cross-references the consolidated contacts against:
   - macOS WhatsApp Desktop (messages + calls)
   - Gmail interaction history (via Google Takeout streamed without disk unpacking)

3. Splits contacts into:
   - cleaned_contacts.csv (active in the last N years)
   - archived_contacts.csv (inactive, for Google Drive backup)

Usage:
    python cleanup_contacts.py contacts.csv
    python cleanup_contacts.py contacts.csv --other-contacts other_contacts.csv
    python cleanup_contacts.py contacts.csv --years 3
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

MIN_DIGITS_DEFAULT = 7
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
    return re.sub(r"\D", "", jid.split("@")[0])


def merge_into(result: dict[str, datetime], key: str, dt: datetime | None) -> None:
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


def normalize_phone(raw: str) -> str:
    return re.sub(r"\D", "", str(raw))


# ---------------------------------------------------------------------------
# Smart Deduplication & Merging Engine
# ---------------------------------------------------------------------------


class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        if self.parent[i] == i:
            return i
        self.parent[i] = self.find(self.parent[i])
        return self.parent[i]

    def union(self, i: int, j: int) -> None:
        root_i = self.find(i)
        root_j = self.find(j)
        if root_i != root_j:
            self.parent[root_i] = root_j


def to_consonant_classes(s: str, is_heb: bool = False) -> str:
    s = s.lower()
    if not is_heb:
        s = re.sub(r"tz|ts", "z", s)
        s = re.sub(r"ch|kh", "k", s)
        s = re.sub(r"sh", "s", s)
    HEB_MAP = {
        'ב': '1', 'פ': '1', 'ף': '1',
        'ג': '2', 'כ': '2', 'ך': '2', 'ק': '2', 'ח': '2',
        'ד': '3', 'ט': '3', 'ת': '3',
        'ל': '4',
        'מ': '5', 'ם': '5', 'נ': '5', 'ן': '5',
        'ר': '6',
        'ז': '7', 'ס': '7', 'צ': '7', 'ץ': '7', 'ש': '7',
    }
    ENG_MAP = {
        'b': '1', 'v': '1', 'f': '1', 'p': '1', 'w': '1',
        'g': '2', 'k': '2', 'q': '2', 'c': '2', 'h': '2',
        'd': '3', 't': '3',
        'l': '4',
        'm': '5', 'n': '5',
        'r': '6',
        'z': '7', 's': '7', 'x': '7',
    }
    mapping = HEB_MAP if is_heb else ENG_MAP
    res = []
    for ch in s:
        code = mapping.get(ch, '')
        if code and (not res or res[-1] != code):
            res.append(code)
    return ''.join(res)


def is_hebrew_english_equivalent(name1: str, name2: str) -> bool:
    has_heb1 = any('\u0590' <= ch <= '\u05FF' for ch in name1)
    has_heb2 = any('\u0590' <= ch <= '\u05FF' for ch in name2)
    if (has_heb1 and not has_heb2) or (has_heb2 and not has_heb1):
        heb = name1 if has_heb1 else name2
        eng = name2 if has_heb1 else name1
        sk_eng = to_consonant_classes(eng, is_heb=False)
        sk_heb = to_consonant_classes(heb, is_heb=True)
        if sk_eng == sk_heb and len(sk_eng) >= 3:
            return True
        w_eng = [w for w in re.findall(r'[a-zA-Z]+', eng)]
        w_heb = [w for w in re.findall(r'[\u0590-\u05FF]+', heb)]
        if len(w_eng) == len(w_heb) and len(w_eng) >= 2:
            all_match = True
            for we, wh in zip(w_eng, w_heb):
                se = to_consonant_classes(we, is_heb=False)
                sh = to_consonant_classes(wh, is_heb=True)
                if not (se == sh or (len(se) >= 2 and se in sh) or (len(sh) >= 2 and sh in se)):
                    all_match = False
                    break
            if all_match:
                return True
    return False


def are_similar_names(n1: str, n2: str) -> bool:
    if not n1 or not n2:
        return True
    if is_hebrew_english_equivalent(n1, n2):
        return True
    w1 = [w.lower() for w in re.findall(r"[\w]+", n1)]
    w2 = [w.lower() for w in re.findall(r"[\w]+", n2)]
    if sorted(w1) == sorted(w2):
        return True
    s1, s2 = set(w1), set(w2)
    if s1.issubset(s2) or s2.issubset(s1):
        return True
    c1 = "".join(re.findall(r"[a-zA-Z]+", n1.lower()))
    c2 = "".join(re.findall(r"[a-zA-Z]+", n2.lower()))
    if c1 and c2 and (c1 == c2 or (len(c1) >= 5 and c1 in c2) or (len(c2) >= 5 and c2 in c1)):
        return True
    return False


def merge_contact_cluster(records: pd.DataFrame, all_cols: list[str]) -> dict:
    """
    Consolidate a cluster of duplicate/fragmented contact records into a single
    complete record.
    """
    # 1. Best Name Selection
    # Prioritize main contacts over other_contacts, and proper First+Last over emails
    best_fn, best_mn, best_ln = "", "", ""
    best_org = ""
    best_name_score = -1

    for _, r in records.iterrows():
        fn = r.get("First Name", "").strip()
        mn = r.get("Middle Name", "").strip()
        ln = r.get("Last Name", "").strip()
        org = r.get("Organization Name", "").strip()
        origin = r.get("_origin", "main")

        full = f"{fn} {ln}".strip()
        score = 0
        if origin == "main":
            score += 10
        if fn and ln:
            score += 10
        elif full and "@" not in full:
            score += 4
        # Penalize ALL-CAPS email-like names
        if "@" in full or (full.isupper() and len(full) > 4):
            score -= 5

        if score > best_name_score and (fn or ln or full):
            best_name_score = score
            best_fn, best_mn, best_ln = fn, mn, ln
        if org and not best_org:
            best_org = org

    # If no separate first/last found, fall back to whatever non-empty is available
    if not best_fn and not best_ln:
        for _, r in records.iterrows():
            best_fn = r.get("First Name", "").strip()
            best_ln = r.get("Last Name", "").strip()
            if best_fn or best_ln:
                break

    # Collect distinct substantive names across cluster for composite naming
    primary_display = f"{best_fn} {best_ln}".strip() or best_org
    distinct_names = [primary_display] if primary_display else []

    for _, r in records.iterrows():
        fn_r = r.get("First Name", "").strip()
        ln_r = r.get("Last Name", "").strip()
        org_r = r.get("Organization Name", "").strip()
        full_r = f"{fn_r} {ln_r}".strip()
        candidate = full_r if (full_r and "@" not in full_r) else org_r
        if not candidate:
            continue

        # If candidate is a Hebrew-English transliteration equivalent of an existing name:
        # Keep ONLY the English version (e.g. Adi Perl instead of Adi Perl / עדי פרל)
        handled = False
        for idx, existing in enumerate(distinct_names):
            if is_hebrew_english_equivalent(existing, candidate):
                cand_has_heb = any('\u0590' <= ch <= '\u05FF' for ch in candidate)
                if not cand_has_heb:  # candidate is English, existing was Hebrew
                    distinct_names[idx] = candidate
                handled = True
                break

        if handled:
            continue

        if not any(are_similar_names(candidate, existing) for existing in distinct_names):
            distinct_names.append(candidate)

    if len(distinct_names) == 1:
        # If Hebrew name was replaced by English name, update best_fn and best_ln accordingly
        only_name = distinct_names[0]
        parts = only_name.split(None, 1)
        if len(parts) == 2:
            best_fn, best_ln = parts[0], parts[1]
        else:
            best_fn, best_ln = only_name, ""
    elif len(distinct_names) > 1:
        secondary_part = " / ".join(distinct_names[1:])
        if best_ln:
            best_ln = f"{best_ln} / {secondary_part}"
        elif best_fn:
            best_fn = f"{best_fn} / {secondary_part}"
        else:
            best_fn = " / ".join(distinct_names)

    # 2. Collect unique phones
    unique_phones: list[tuple[str, str]] = []  # (label, value)
    seen_phone_digits = set()
    for _, r in records.iterrows():
        for i in range(1, 10):
            raw_val = r.get(f"Phone {i} - Value", "").strip()
            lbl = r.get(f"Phone {i} - Label", "").strip() or "Mobile"
            for val in raw_val.split(":::"):
                val = val.strip()
                if val:
                    digits = normalize_phone(val)
                    key = digits[-9:] if len(digits) >= 7 else digits
                    if key and key not in seen_phone_digits:
                        seen_phone_digits.add(key)
                        unique_phones.append((lbl, val))

    # 3. Collect unique emails
    unique_emails: list[tuple[str, str]] = []  # (label, value)
    seen_emails = set()
    for _, r in records.iterrows():
        for i in range(1, 10):
            raw_val = r.get(f"E-mail {i} - Value", "").strip()
            lbl = r.get(f"E-mail {i} - Label", "").strip() or "Home"
            for val in raw_val.split(":::"):
                val = val.strip()
                if val and "@" in val:
                    norm = val.lower()
                    if norm not in seen_emails:
                        seen_emails.add(norm)
                        unique_emails.append((lbl, val))

    # 4. Notes, Birthday, Labels
    merged_notes = []
    best_bday = ""
    merged_labels = set()

    for _, r in records.iterrows():
        notes = r.get("Notes", "").strip()
        if notes and notes not in merged_notes:
            merged_notes.append(notes)
        bday = r.get("Birthday", "").strip()
        if bday and not best_bday:
            best_bday = bday
        labels_str = r.get("Labels", "").strip()
        if labels_str:
            for lbl in labels_str.split(":::"):
                lbl = lbl.strip()
                if lbl and lbl != "* Other Contacts":
                    merged_labels.add(lbl)

    # 5. Collect legitimate websites (excluding dead fb://, google.com/profiles, sync.me)
    unique_websites: list[tuple[str, str]] = []
    seen_websites = set()
    for _, r in records.iterrows():
        for i in range(1, 4):
            raw_val = r.get(f"Website {i} - Value", "").strip()
            lbl = r.get(f"Website {i} - Label", "").strip() or "HomePage"
            for val in raw_val.split(":::"):
                val = val.strip()
                if not val:
                    continue
                v_lower = val.lower()
                # Discard dead links from defunct services:
                if any(x in v_lower for x in ["fb://", "google.com/profiles", "plus.google.com", "sync.me/profile", "google.com/reader"]):
                    continue
                if v_lower not in seen_websites:
                    seen_websites.add(v_lower)
                    unique_websites.append((lbl, val))

    # Build the merged row dictionary
    merged = {c: "" for c in all_cols}
    merged["First Name"] = best_fn
    merged["Middle Name"] = best_mn
    merged["Last Name"] = best_ln
    merged["Organization Name"] = best_org
    merged["Birthday"] = best_bday
    merged["Notes"] = " | ".join(merged_notes)
    merged["Labels"] = " ::: ".join(sorted(merged_labels)) if merged_labels else "* myContacts"

    # Fill phones
    for idx, (lbl, val) in enumerate(unique_phones[:10], 1):
        merged[f"Phone {idx} - Label"] = lbl
        merged[f"Phone {idx} - Value"] = val

    # Fill emails
    for idx, (lbl, val) in enumerate(unique_emails[:10], 1):
        merged[f"E-mail {idx} - Label"] = lbl
        merged[f"E-mail {idx} - Value"] = val

    # Fill legitimate websites
    for idx, (lbl, val) in enumerate(unique_websites[:3], 1):
        merged[f"Website {idx} - Label"] = lbl
        merged[f"Website {idx} - Value"] = val

    return merged


def smart_deduplicate_contacts(
    main_csv: str, other_csv: str | None
) -> tuple[pd.DataFrame, int, int]:
    """
    Load main and other contacts, cluster duplicates, and merge them.
    Returns (consolidated_df, total_raw_count, total_clusters_merged).
    """
    df_main = pd.read_csv(main_csv, dtype=str, keep_default_na=False)
    df_main["_origin"] = "main"

    if other_csv and os.path.exists(other_csv):
        df_other = pd.read_csv(other_csv, dtype=str, keep_default_na=False)
        df_other["_origin"] = "other"
        log.info(
            "Merging main contacts (%d) with other_contacts (%d)...",
            len(df_main),
            len(df_other),
        )
    else:
        df_other = pd.DataFrame()
        log.info("Deduplicating main contacts (%d)...", len(df_main))

    cols = list(df_main.columns)
    if not df_other.empty:
        for c in cols:
            if c not in df_other.columns:
                df_other[c] = ""
        for c in df_other.columns:
            if c not in cols:
                df_main[c] = ""
                cols.append(c)
        all_df = pd.concat([df_main[cols], df_other[cols]], ignore_index=True)
    else:
        all_df = df_main[cols].copy()

    # Sanitize accidental self-email contamination in contacts of other people (e.g. legacy Yigal Petreanu card)
    for col in [c for c in all_df.columns if EMAIL_COLUMN_RE.match(c)]:
        for idx in range(len(all_df)):
            val = str(all_df.at[idx, col])
            if val and "erez.bashan@gmail.com" in val.lower():
                fn = str(all_df.at[idx, "First Name"]).strip().lower()
                ln = str(all_df.at[idx, "Last Name"]).strip().lower()
                if "erez" not in fn and "bashan" not in ln:
                    new_vals = [
                        v.strip() for v in val.split(":::")
                        if v.strip().lower() != "erez.bashan@gmail.com"
                    ]
                    all_df.at[idx, col] = " ::: ".join(new_vals)

    N = len(all_df)
    uf = UnionFind(N)

    # Identify company switchboards / shared landlines (shared across 3+ distinct people)
    phone_names: dict[str, set[str]] = {}
    for i, r in all_df.iterrows():
        fn = r.get("First Name", "").strip()
        ln = r.get("Last Name", "").strip()
        full = f"{fn} {ln}".strip()
        if full and "@" not in full:
            for c in [col for col in all_df.columns if PHONE_COLUMN_RE.match(col)]:
                val = r[c]
                if val:
                    for raw in val.split(":::"):
                        d = normalize_phone(raw)
                        if len(d) >= 7:
                            phone_names.setdefault(d[-9:], set()).add(full.lower())

    switchboard_keys = {k for k, names in phone_names.items() if len(names) >= 3}
    if switchboard_keys:
        log.info(
            "Excluding %d shared company switchboards/landlines from auto-merge to prevent cross-colleague merging",
            len(switchboard_keys),
        )

    def can_merge_by_phone(r1: pd.Series, r2: pd.Series) -> bool:
        # If they share an email address, definitely same person
        e1 = {r1[c].strip().lower() for c in r1.index if EMAIL_COLUMN_RE.match(c) and r1[c] and "@" in r1[c]}
        e2 = {r2[c].strip().lower() for c in r2.index if EMAIL_COLUMN_RE.match(c) and r2[c] and "@" in r2[c]}
        if e1 and e2 and (e1 & e2):
            return True

        fn1, ln1 = r1.get("First Name", "").strip(), r1.get("Last Name", "").strip()
        fn2, ln2 = r2.get("First Name", "").strip(), r2.get("Last Name", "").strip()
        full1 = f"{fn1} {ln1}".strip()
        full2 = f"{fn2} {ln2}".strip()

        # If one record has NO personal name (e.g. only org/role title or empty), merging is safe
        if (not full1 or "@" in full1) or (not full2 or "@" in full2):
            return True

        # Both have personal names. Require commonality to merge by phone:
        w1 = [w.lower() for w in re.findall(r"[\w]+", full1)]
        w2 = [w.lower() for w in re.findall(r"[\w]+", full2)]
        if sorted(w1) == sorted(w2):
            return True

        s1, s2 = set(w1), set(w2)
        if s1.issubset(s2) or s2.issubset(s1):
            return True

        if is_hebrew_english_equivalent(full1, full2):
            return True

        # Distinct personal names with nothing in common -> Refrain from merging!
        return False

    # 1. Match by Phone Number (last 9 digits, excluding switchboards and distinct names)
    phone_map: dict[str, int] = {}
    for i, r in all_df.iterrows():
        for c in [col for col in all_df.columns if PHONE_COLUMN_RE.match(col)]:
            val = r[c]
            if val:
                for raw in val.split(":::"):
                    d = normalize_phone(raw)
                    if len(d) >= 7:
                        key = d[-9:]
                        if key in switchboard_keys:
                            continue
                        if key in phone_map:
                            target_i = phone_map[key]
                            if can_merge_by_phone(r, all_df.iloc[target_i]):
                                uf.union(i, target_i)
                        else:
                            phone_map[key] = i

    # 2. Match by Shared Email Address
    email_map: dict[str, int] = {}
    for i, r in all_df.iterrows():
        for c in [col for col in all_df.columns if EMAIL_COLUMN_RE.match(col)]:
            val = r[c]
            if val and "@" in val:
                key = val.strip().lower()
                if key in email_map:
                    uf.union(i, email_map[key])
                else:
                    email_map[key] = i

    # 3. Match by Normalized & Reversed Names
    name_map: dict[tuple, int] = {}
    for i, r in all_df.iterrows():
        fn = r.get("First Name", "").strip()
        ln = r.get("Last Name", "").strip()
        words = re.findall(r"[\w]+", f"{fn} {ln}".lower())
        if len(words) >= 2 and sum(len(w) for w in words) >= 5:
            key = tuple(sorted(words))
            if key in name_map:
                uf.union(i, name_map[key])
            else:
                name_map[key] = i

    # 4. Match Email-Username to Person Full Name
    # (e.g. yifat.meidav@elbitsystems.com -> Yifat Meidav)
    name_alphanumeric_map: dict[str, list[int]] = {}
    for i, r in all_df.iterrows():
        fn = r.get("First Name", "").strip()
        ln = r.get("Last Name", "").strip()
        if fn and ln:
            clean = "".join(re.findall(r"[a-zA-Z]+", f"{fn}{ln}".lower()))
            if len(clean) >= 6:
                name_alphanumeric_map.setdefault(clean, []).append(i)

    for i, r in all_df.iterrows():
        for c in [col for col in all_df.columns if EMAIL_COLUMN_RE.match(col)]:
            val = r[c]
            if val and "@" in val:
                user = val.split("@")[0].lower()
                clean_user = "".join(re.findall(r"[a-zA-Z]+", user))
                if len(clean_user) >= 6 and clean_user in name_alphanumeric_map:
                    for target_i in name_alphanumeric_map[clean_user]:
                        uf.union(i, target_i)

    # Group records by root parent
    clusters: dict[int, list[int]] = {}
    for i in range(N):
        clusters.setdefault(uf.find(i), []).append(i)

    clean_cols = [c for c in cols if not c.startswith("_")]
    # Ensure standard phone and email slots exist up to 5
    for i in range(1, 6):
        for prefix in ["Phone", "E-mail"]:
            for suffix in ["Label", "Value"]:
                col_name = f"{prefix} {i} - {suffix}"
                if col_name not in clean_cols:
                    clean_cols.append(col_name)

    merged_rows = []
    cluster_indices_list = []
    multi_clusters_count = 0

    for root_id, indices in clusters.items():
        records = all_df.iloc[indices]
        merged_row = merge_contact_cluster(records, clean_cols)
        merged_rows.append(merged_row)
        cluster_indices_list.append(indices)
        if len(indices) > 1:
            multi_clusters_count += 1

    consolidated_df = pd.DataFrame(merged_rows).fillna("")
    consolidated_df = consolidated_df.astype(str)
    consolidated_df["_cluster_indices"] = cluster_indices_list
    log.info(
        "Consolidation complete: %d raw records -> %d unique contacts (%d duplicate clusters merged)",
        N,
        len(consolidated_df),
        multi_clusters_count,
    )
    return consolidated_df, all_df, N, multi_clusters_count


# ---------------------------------------------------------------------------
# WhatsApp Messages Extraction (ChatStorage.sqlite)
# ---------------------------------------------------------------------------


def extract_from_messages(chat_db_path: str) -> tuple[dict[str, datetime], dict[str, str]]:
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
          AND cs.ZCONTACTJID LIKE '%@g.us'
    ) GROUP BY jid
    """

    df = pd.read_sql_query(query, conn)

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
    log.info("Extracting WhatsApp interaction data...")
    message_dates, lid_to_digits = extract_from_messages(chat_db_path)
    call_dates = extract_from_calls(call_db_path, lid_to_digits)

    combined = dict(message_dates)
    for digits, dt in call_dates.items():
        merge_into(combined, digits, dt)

    log.info("  Total WhatsApp:     %d unique numbers", len(combined))
    return combined


# ---------------------------------------------------------------------------
# Google Takeout Email Extraction
# ---------------------------------------------------------------------------


def find_takeout_archive() -> str | None:
    candidates = glob.glob("takeout-*.tgz") + glob.glob("takeout-*.tar.gz") + glob.glob("*.mbox")
    valid = [c for c in candidates if os.path.getsize(c) > 10 * 1024 * 1024]
    if valid:
        return max(valid, key=os.path.getsize)
    return None


def extract_email_interactions(
    archive_path: str | None,
    target_emails: set[str],
    cache_path: str = EMAIL_CACHE_FILE,
    rebuild_cache: bool = False,
) -> dict[str, datetime]:
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

    cmd = ["tar", "-xzOf", archive_path] if archive_path.endswith((".tgz", ".tar.gz")) else ["cat", archive_path]
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
# Matching & Classification
# ---------------------------------------------------------------------------


def find_latest_phone_interaction(
    contact_digits: str, number_dates: dict[str, datetime], min_digits: int
) -> datetime | None:
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
    contacts: pd.DataFrame,
    number_dates: dict[str, datetime],
    email_dates: dict[str, datetime],
    min_digits: int,
    years: int,
) -> pd.DataFrame:
    phone_cols = [c for c in contacts.columns if PHONE_COLUMN_RE.match(c)]
    email_cols = [c for c in contacts.columns if EMAIL_COLUMN_RE.match(c)]

    log.info("Matching against %d phone columns and %d email columns...", len(phone_cols), len(email_cols))

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


def prioritize_contact_emails(
    contacts: pd.DataFrame, email_dates: dict[str, datetime]
) -> pd.DataFrame:
    """
    For each contact with emails, re-orders email fields so that:
    1. The email with the most recent Gmail interaction date is placed at E-mail 1 (Primary).
    2. Its label is marked with the Google Contacts primary indicator ('* ' prefix, e.g. '* Work').
    3. Secondary emails follow in descending order of last interaction date, with plain labels.
    """
    df = contacts.copy()
    reordered_count = 0

    for idx in range(len(df)):
        raw_items = []
        for i in range(1, 11):
            val_col = f"E-mail {i} - Value"
            lbl_col = f"E-mail {i} - Label"
            val = str(df.at[idx, val_col]).strip() if val_col in df.columns else ""
            lbl = str(df.at[idx, lbl_col]).strip() if lbl_col in df.columns else ""
            if val and "@" in val:
                d = email_dates.get(val.lower(), datetime.min)
                raw_items.append((val, lbl, d, len(raw_items)))

        if not raw_items:
            continue

        if len(raw_items) == 1:
            lbl = raw_items[0][1] or "Home"
            clean_lbl = lbl.lstrip("* ").strip() or "Home"
            if "E-mail 1 - Label" in df.columns:
                df.at[idx, "E-mail 1 - Label"] = f"* {clean_lbl}"
            continue

        # Sort: descending by datetime, then preserve original index
        sorted_items = sorted(raw_items, key=lambda x: (x[2], -x[3]), reverse=True)

        if sorted_items[0][0] != raw_items[0][0]:
            reordered_count += 1

        for slot in range(1, 11):
            val_col = f"E-mail {slot} - Value"
            lbl_col = f"E-mail {slot} - Label"
            if slot <= len(sorted_items):
                val, lbl, _, _ = sorted_items[slot - 1]
                clean_lbl = lbl.lstrip("* ").strip() or "Home"
                if slot == 1:
                    lbl_final = f"* {clean_lbl}"
                else:
                    lbl_final = clean_lbl
                if val_col in df.columns:
                    df.at[idx, val_col] = val
                if lbl_col in df.columns:
                    df.at[idx, lbl_col] = lbl_final
            else:
                if val_col in df.columns:
                    df.at[idx, val_col] = ""
                if lbl_col in df.columns:
                    df.at[idx, lbl_col] = ""

    log.info(
        "Email prioritization complete: Promoted most recent email to E-mail 1 (Primary) for %d contacts",
        reordered_count,
    )
    return df



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
    log.info("  Total consolidated contacts:    %d", total)
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


def print_archive_sample(archive: pd.DataFrame, n: int = 20) -> None:
    if archive.empty:
        return
    log.info("Sample contacts being archived (first %d of %d):", min(n, len(archive)), len(archive))
    for _, row in archive.head(n).iterrows():
        fn = row.get("First Name", "").strip()
        ln = row.get("Last Name", "").strip()
        org = row.get("Organization Name", "").strip()
        name = f"{fn} {ln}".strip() or org or "(no name)"
        year = row["_interaction_year"]
        tag = f"last seen {int(year)}" if pd.notna(year) else "no interaction"
        p = row.get("Phone 1 - Value", "").strip()
        e = row.get("E-mail 1 - Value", "").strip()
        log.info("  - %-35s  [%s]  %s", name, tag, p or e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deduplicate, merge, and clean Google Contacts using WhatsApp and Gmail interactions."
    )
    parser.add_argument("csv", help="Path to main Google Contacts CSV file.")
    parser.add_argument(
        "--other-contacts",
        default="other_contacts.csv",
        help="Path to other_contacts.csv (auto-detected if present).",
    )
    parser.add_argument(
        "--years",
        type=int,
        default=5,
        help="Years of interaction history to keep (default: 5).",
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
        help=f"Path to cache file for email interaction dates (default: {EMAIL_CACHE_FILE}).",
    )
    parser.add_argument(
        "--rebuild-email-cache",
        action="store_true",
        help="Force re-scanning the Takeout archive even if cache exists.",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for output CSV files.",
    )

    args = parser.parse_args()

    # Step 1: Smart Deduplication & Merging
    other_file = args.other_contacts if (args.other_contacts and os.path.exists(args.other_contacts)) else None
    contacts, all_df, total_raw, clusters_merged = smart_deduplicate_contacts(args.csv, other_file)

    # Step 2: Extract all target emails from consolidated contacts
    target_emails: set[str] = set()
    for col in [c for c in contacts.columns if EMAIL_COLUMN_RE.match(c)]:
        for val in contacts[col]:
            if val and "@" in val:
                target_emails.add(val.strip().lower())
    log.info("Total unique emails across consolidated contacts: %d", len(target_emails))

    # Step 3: Extract WhatsApp interactions
    number_dates = extract_all_whatsapp_numbers(args.whatsapp_db, args.call_db)

    # Step 4: Extract Email interactions
    takeout_path = args.takeout_archive or find_takeout_archive()
    email_dates = extract_email_interactions(
        takeout_path,
        target_emails,
        cache_path=args.email_cache,
        rebuild_cache=args.rebuild_email_cache,
    )

    # Step 5: Classify Contacts (5-Year Active Window)
    contacts = classify_contacts(
        contacts, number_dates, email_dates, args.min_digits, args.years
    )

    # Step 5b: Reorder emails so the most recently interacted email is E-mail 1 (Primary)
    contacts = prioritize_contact_emails(contacts, email_dates)

    keep = contacts[contacts["_keep"]]
    archive = contacts[~contacts["_keep"]]

    # Write merge audit log ONLY for contacts that are actually KEPT in cleaned_contacts.csv
    log_path = "merged_contacts_log.txt"
    merge_log_lines = [
        "=" * 80 + "\n",
        "CONTACTS SMART MERGE AUDIT LOG (KEPT CONTACTS ONLY)\n",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
        f"Showing merged duplicate clusters for active contacts retained in cleaned_contacts.csv (--years {args.years})\n",
        "=" * 80 + "\n\n",
    ]

    kept_clusters_count = 0
    for _, row in keep.iterrows():
        indices = row.get("_cluster_indices")
        if isinstance(indices, (list, tuple)) and len(indices) > 1:
            kept_clusters_count += 1
            name = (
                f"{row.get('First Name', '')} {row.get('Last Name', '')}".strip()
                or row.get("Organization Name", "")
                or "(no name)"
            )
            merge_log_lines.append("-" * 80 + "\n")
            merge_log_lines.append(
                f"MERGED CLUSTER #{kept_clusters_count}: '{name}' ({len(indices)} records combined)\n"
            )
            merge_log_lines.append("  Original records:\n")
            for idx in indices:
                rec = all_df.iloc[idx]
                orig = rec.get("_origin", "main")
                r_fn = rec.get("First Name", "").strip()
                r_ln = rec.get("Last Name", "").strip()
                r_org = rec.get("Organization Name", "").strip()
                r_name = f"{r_fn} {r_ln}".strip() or r_org or "(no name)"
                r_phones = [
                    rec.get(f"Phone {k} - Value", "").strip()
                    for k in range(1, 5)
                    if rec.get(f"Phone {k} - Value", "").strip()
                ]
                r_emails = [
                    rec.get(f"E-mail {k} - Value", "").strip()
                    for k in range(1, 5)
                    if rec.get(f"E-mail {k} - Value", "").strip()
                ]
                merge_log_lines.append(
                    f"    * [{orig:5s}] Name: '{r_name}' | Phones: {r_phones} | Emails: {r_emails}\n"
                )

            m_phones = [
                row.get(f"Phone {k} - Value", "")
                for k in range(1, 10)
                if row.get(f"Phone {k} - Value", "")
            ]
            m_emails = [
                row.get(f"E-mail {k} - Value", "")
                for k in range(1, 10)
                if row.get(f"E-mail {k} - Value", "")
            ]
            merge_log_lines.append("  Resulting consolidated contact:\n")
            merge_log_lines.append(
                f"    Name:   {name}\n    Phones: {m_phones}\n    Emails: {m_emails}\n\n"
            )

    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.writelines(merge_log_lines)
        log.info("Wrote audit log for kept merged contacts to %s (%d clusters)", log_path, kept_clusters_count)
    except Exception as e:
        log.warning("Could not write merge audit log: %s", e)

    # Step 6: Reporting
    print_year_breakdown(contacts)

    source_counts = keep["_source"].value_counts().to_dict()
    log.info("Consolidated contacts retention (--years %d):", args.years)
    log.info("  Total KEPT:       %d contacts (%.1f%%)", len(keep), 100 * len(keep) / len(contacts))
    log.info("    via WhatsApp:   %d", source_counts.get("whatsapp", 0))
    log.info("    via Email:      %d", source_counts.get("email", 0))
    log.info("    via Both:       %d", source_counts.get("both", 0))
    log.info("  Total ARCHIVED:   %d contacts (%.1f%%)", len(archive), 100 * len(archive) / len(contacts))
    log.info("")

    print_archive_sample(archive)

    # Step 7: Write Output Files
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    internal_cols = ["_latest_interaction", "_interaction_year", "_source", "_keep", "_cluster_indices"]
    cleaned_path = out_dir / "cleaned_contacts.csv"
    archived_path = out_dir / "archived_contacts.csv"

    keep_out = keep.drop(columns=internal_cols)
    archive_out = archive.drop(columns=internal_cols)

    keep_out.to_csv(cleaned_path, index=False)
    archive_out.to_csv(archived_path, index=False)

    # Also generate split batches for convenient web import
    mid = len(keep_out) // 2
    keep_out.iloc[:mid].to_csv(out_dir / "cleaned_contacts_part1.csv", index=False)
    keep_out.iloc[mid:].to_csv(out_dir / "cleaned_contacts_part2.csv", index=False)

    log.info("")
    log.info("Files written:")
    log.info("  %s  (%d clean, deduplicated contacts)", cleaned_path, len(keep_out))
    log.info("  %s  (%d archived contacts)", archived_path, len(archive_out))
    log.info("  cleaned_contacts_part1.csv (%d contacts)", len(keep_out.iloc[:mid]))
    log.info("  cleaned_contacts_part2.csv (%d contacts)", len(keep_out.iloc[mid:]))
    log.info("")
    log.info("NEXT STEPS:")
    log.info("  1. Upload archived_contacts.csv to Google Drive for safekeeping")
    log.info("  2. In Google Contacts -> Select all -> Delete (in Trash for 30 days)")
    log.info("  3. Import cleaned_contacts.csv (or part1 and part2)")


if __name__ == "__main__":
    main()
