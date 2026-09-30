# Contacts Cleanup

Automated cleanup of a large Google Contacts list by cross-referencing against:
1. **WhatsApp Desktop (macOS)** messages and calls history
2. **Gmail interactions** via Google Takeout mbox archives (streamed directly without exhausting disk space)

## Problem

Large contact lists cause sync issues with car Bluetooth/multimedia systems. This tool identifies contacts with **no active interaction in the last N years** and safely archives them to a separate CSV.

## Prerequisites

- **macOS** with WhatsApp Desktop installed
- **Python 3.10+** (virtual environment with `pandas` and `python-dateutil`)
- A **Google Contacts CSV export** (`contacts.csv` from [Google Contacts](https://contacts.google.com))
- *(Optional)* **Google Takeout Gmail archive** (`takeout-*.tgz` or `.mbox`) for email interaction history

## Usage

```bash
# Setup virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install pandas python-dateutil

# Run cleanup (default: 5 years)
python cleanup_contacts.py contacts.csv

# Custom time window (e.g. 3 years)
python cleanup_contacts.py contacts.csv --years 3

# Custom output directory
python cleanup_contacts.py contacts.csv --output-dir ./output
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--years N` | `5` | Years of interaction history to consider |
| `--min-digits N` | `7` | Minimum digit overlap for phone suffix matching |
| `--whatsapp-db PATH` | Auto-detected | Path to WhatsApp `ChatStorage.sqlite` |
| `--call-db PATH` | Auto-detected | Path to WhatsApp `CallHistory.sqlite` |
| `--takeout-archive PATH` | Auto-detected | Path to Takeout `.tgz` or `.mbox` archive |
| `--email-cache PATH` | `email_interactions.json` | Cache file storing parsed email interaction dates |
| `--rebuild-email-cache` | off | Force re-scanning Takeout archive even if cache exists |
| `--output-dir DIR` | `.` | Directory for output CSV files |

## Output Files

| File | Description |
|------|-------------|
| `cleaned_contacts.csv` | Contacts to keep (had WhatsApp or Email interaction within the window) |
| `archived_contacts.csv` | Contacts to archive (upload to Google Drive for safekeeping) |

## How It Works

1. **WhatsApp Messages & Calls**:
   - Queries `ChatStorage.sqlite` (direct messages, group messages, active group members)
   - Queries `CallHistory.sqlite` (resolves `@lid` identifiers to actual phone numbers)
2. **Gmail Takeout**:
   - Streams the compressed `.tgz` archive directly using `tar -xzOf` without unpacking 20+ GB to disk
   - Ignores spam (`X-Gmail-Labels: Spam`)
   - Caches matched email dates into `email_interactions.json` for instant subsequent runs
3. **Contact Matching**:
   - Matches across all `Phone N - Value` columns using phone suffix matching (min 7 digits)
   - Matches across all `E-mail N - Value` columns
   - Determines the most recent interaction date per contact across all channels
4. **Safety & Archiving**:
   - Retains active contacts in `cleaned_contacts.csv`
   - Keeps everything else safely in `archived_contacts.csv` for upload to Google Drive

## Re-importing Cleaned Contacts

1. Go to [Google Contacts](https://contacts.google.com)
2. Select all contacts and delete (they remain in Google Trash for 30 days as a safety net)
3. Import `cleaned_contacts.csv`
4. Upload `archived_contacts.csv` to Google Drive for backup
