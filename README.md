# Contacts Cleanup

Automated cleanup of a large Google Contacts list by cross-referencing against WhatsApp Desktop interaction history on macOS.

## Problem

Large contact lists cause sync issues with car Bluetooth/multimedia systems. This tool identifies contacts with **no WhatsApp activity in the last N years** and archives them to a separate CSV.

## Prerequisites

- **macOS** with WhatsApp Desktop installed (uses the local SQLite database)
- **Python 3.10+**
- **pandas** (`pip install pandas`)
- A **Google Contacts CSV export** ([export here](https://contacts.google.com) → Export → Google CSV)

## Usage

```bash
# Install dependency
pip install pandas

# Dry run — see stats without writing files
python cleanup_contacts.py contacts.csv --dry-run

# Full run — writes cleaned_contacts.csv and archived_contacts.csv
python cleanup_contacts.py contacts.csv

# Custom time window (e.g. 3 years instead of 5)
python cleanup_contacts.py contacts.csv --years 3

# Specify output directory
python cleanup_contacts.py contacts.csv --output-dir ./output
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--years N` | `5` | Years of interaction history to consider |
| `--min-digits N` | `7` | Minimum digit overlap for phone matching |
| `--whatsapp-db PATH` | Auto-detected | Path to WhatsApp `ChatStorage.sqlite` |
| `--dry-run` | off | Print stats without writing output files |
| `--output-dir DIR` | `.` | Output directory for CSV files |

## Output Files

| File | Description |
|------|-------------|
| `cleaned_contacts.csv` | Contacts to keep (had WhatsApp interaction within the window) |
| `archived_contacts.csv` | Contacts to archive (upload to Google Drive for safekeeping) |

## How It Works

1. Queries the macOS WhatsApp Desktop SQLite database for all phone numbers with activity in the specified time window
2. Includes **direct messages** (from/to JIDs) and **group members** from recently-active groups
3. Matches against **all phone columns** in the Google Contacts CSV using suffix matching (handles varying country code prefixes)
4. Enforces a minimum digit overlap to prevent false positives

## Re-importing Cleaned Contacts

1. Go to [Google Contacts](https://contacts.google.com)
2. Delete all contacts (or use a label-based approach)
3. Import → select `cleaned_contacts.csv`
4. Upload `archived_contacts.csv` to Google Drive for safekeeping
