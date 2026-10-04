"""
Name translation module for Israeli contacts.
Translates Latin/English names to standard Hebrew with high confidence for contacts with Israeli phone numbers.
Preserves original English name in the 'Nickname' field for bidirectional search.
"""

from __future__ import annotations

import re
from pathlib import Path
import pandas as pd


# High-confidence mapping of Latin transliterations to standard Hebrew first names
HEB_FIRST_MAP: dict[str, str] = {
    "adi": "עדי",
    "alex": "אלכס",
    "alon": "אלון",
    "amir": "אמיר",
    "amit": "עמית",
    "amitay": "אמיתי",
    "ariel": "אריאל",
    "asaf": "אסף",
    "assaf": "אסף",
    "avi": "אבי",
    "avihai": "אביחי",
    "avital": "אביטל",
    "ayelet": "איילת",
    "benny": "בני",
    "boaz": "בועז",
    "chen": "חן",
    "dalit": "דלית",
    "daniela": "דניאלה",
    "daniella": "דניאלה",
    "danny": "דני",
    "doron": "דורון",
    "dror": "דרור",
    "dudi": "דודי",
    "ehud": "אהוד",
    "einat": "עינת",
    "ella": "אלה",
    "elon": "אלון",
    "eran": "ערן",
    "erez": "ארז",
    "eyal": "אייל",
    "gadi": "גדי",
    "gai": "גיא",
    "gal": "גל",
    "galit": "גלית",
    "gil": "גיל",
    "gila": "גילה",
    "golan": "גולן",
    "guy": "גיא",
    "hadas": "הדס",
    "hannan": "חנן",
    "harel": "הראל",
    "hila": "הילה",
    "iddo": "עידו",
    "ido": "עידו",
    "iftah": "יפתח",
    "ilan": "אילן",
    "inbal": "ענבל",
    "itai": "איתי",
    "jonathan": "יונתן",
    "karin": "קרין",
    "kati": "קטי",
    "lior": "ליאור",
    "matan": "מתן",
    "maya": "מיה",
    "meir": "מאיר",
    "meny": "מני",
    "michael": "מיכאל",
    "miki": "מיקי",
    "miri": "מירי",
    "moshe": "משה",
    "moti": "מוטי",
    "nava": "נאוה",
    "navit": "נאוית",
    "nilli": "נילי",
    "nir": "ניר",
    "noa": "נועה",
    "noam": "נועם",
    "noga": "נגה",
    "ofer": "עופר",
    "ofir": "אופיר",
    "ohad": "אוהד",
    "omri": "עמרי",
    "oren": "אורן",
    "orit": "אורית",
    "paz": "פז",
    "renana": "רננה",
    "rephael": "רפאל",
    "reut": "רעות",
    "revital": "רויטל",
    "rinat": "רינת",
    "roi": "רועי",
    "roy": "רועי",
    "ron": "רון",
    "ronen": "רונן",
    "ruti": "רותי",
    "sarit": "שרית",
    "shahaff": "שחף",
    "shai": "שי",
    "shalev": "שלו",
    "sharon": "שרון",
    "shimon": "שמעון",
    "shlomi": "שלומי",
    "shula": "שולה",
    "sivan": "סיון",
    "tal": "טל",
    "tami": "תמי",
    "tomer": "תומר",
    "tommy": "טומי",
    "tsiki": "ציקי",
    "uri": "אורי",
    "uriel": "אוריאל",
    "vered": "ורד",
    "yaacov": "יעקב",
    "yaar": "יער",
    "yael": "יעל",
    "yair": "יאיר",
    "yaron": "ירון",
    "yifat": "יפעת",
    "yigal": "יגאל",
    "yoav": "יואב",
    "yonatan\U0001f432": "יונתן\U0001f432",
    "yossi": "יוסי",
    "yuval": "יובל",
    "zippora": "ציפורה",
    "ziv": "זיו",
    "zohar": "זהר",
}

# High-confidence mapping of Latin transliterations to standard Hebrew last names
HEB_LAST_MAP: dict[str, str] = {
    "abraham": "אברהם",
    "abrahamsohn": "אברהמזון",
    "agassi": "אגסי",
    "aharonov": "אהרונוב",
    "alon": "אלון",
    "arad": "ארד",
    "arye": "אריה",
    "avida": "אבידע",
    "baider": "ביידר",
    "barsela": "ברסלע",
    "bashan": "בשן",
    "ben-shoshan": "בן-שושן",
    "berkovich": "ברקוביץ'",
    "bouhbut": "בוחבוט",
    "brandes": "ברנדס",
    "braude": "בראודה",
    "cohen": "כהן",
    "collin": "קולין",
    "curiel": "קוריאל",
    "dagan": "דגן",
    "davidi": "דוידי",
    "diener": "דינר",
    "dikman": "דיקמן",
    "disatnik": "דיזטניק",
    "doron": "דורון",
    "dvir": "דביר",
    "einav": "עינב",
    "eis": "אייס",
    "elazari": "אלעזרי",
    "elish": "אליש",
    "entin": "אנטין",
    "eshed": "אשד",
    "even-chen": "אבן-חן",
    "federovsky": "פדרובסקי",
    "feinreich": "פיינרייך",
    "fiszer": "פישר",
    "foox": "פוקס",
    "garibi": "גריבי",
    "gat": "גת",
    "gecht": "גכט",
    "gilor": "גילאור",
    "goder": "גודר",
    "goldshtein": "גולדשטיין",
    "gomer": "גומר",
    "gotliv": "גוטליב",
    "grossboim": "גרוסבוים",
    "halahmi": "הלחמי",
    "hamo": "חמו",
    "handlezaltz": "הנדלזלץ",
    "harari": "הררי",
    "hershanu": "הרשנו",
    "hollander": "הולנדר",
    "ifrach": "יפרח",
    "ishon": "אישון",
    "jaffe": "יפה",
    "kadir": "קדיר",
    "kalisky": "קליסקי",
    "karby": "קרבי",
    "kazzaz": "קזז",
    "komem": "קומם",
    "kotz": "כץ",
    "lerman": "לרמן",
    "lev": "לב",
    "levin": "לוין",
    "levinson": "לוינסון",
    "levy": "לוי",
    "libman": "ליבמן",
    "machluf": "מכלוף",
    "malin": "מלין",
    "manor": "מנור",
    "marchiano": "מרציאנו",
    "margalit": "מרגלית",
    "marton": "מרטון",
    "meidav": "מידב",
    "meiri": "מאירי",
    "moshitch": "מושיץ'",
    "or": "אור",
    "oren": "אורן",
    "oron": "אורון",
    "perets": "פרץ",
    "perez": "פרץ",
    "petreanu": "פטראנו",
    "pinhasi": "פנחסי",
    "predelski": "פרדלסקי",
    "rochev": "רוצ'ב",
    "romano": "רומנו",
    "rosenmann": "רוזנמן",
    "roth": "רוט",
    "royz": "רויז",
    "rubinstein": "רובינשטיין",
    "rudich": "רודיך",
    "samson": "שמשון",
    "sarne": "סרנה",
    "schlesinger": "שלזינגר",
    "schwartz": "שוורץ",
    "segal": "סגל",
    "semet": "סמט",
    "setton": "סתון",
    "shabtai": "שבתי",
    "shacham": "שחם",
    "shaco-levy": "שקו-לוי",
    "shafir": "שפיר",
    "shani": "שני",
    "shelef": "שלף",
    "shemesh": "שמש",
    "shinar": "שנער",
    "shmuelevitz": "שמואלביץ'",
    "sneh": "סנה",
    "stauber": "שטאובר",
    "storfer": "שטורפר",
    "tsubery": "צוברי",
    "uchovsky": "אוחובסקי",
    "vengelnik": "ונגלניק",
    "weinstein": "ויינשטיין",
    "weiss": "וייס",
    "yaniv": "יניב",
    "yaron": "ירון",
    "zadok": "צדוק",
    "zeira": "זעירא",
    "zion": "ציון",
}


def has_israeli_phone_number(row: pd.Series, phone_cols: list[str]) -> bool:
    """Check if the contact has any Israeli phone number (+972 or local 05x, 0[2-9]x)."""
    for col in phone_cols:
        val = str(row.get(col, ""))
        if val and val != "nan":
            digits = re.sub(r"\D", "", val)
            if digits.startswith("972"):
                return True
            if len(digits) in (9, 10) and digits.startswith("05"):
                return True
            if len(digits) == 9 and re.match(r"^0[23489]", digits):
                return True
    return False


def is_latin_name(first: str, last: str) -> bool:
    """Return True if the name contains Latin letters and no Hebrew characters."""
    full = f"{first} {last}".strip()
    if not full:
        return False
    has_heb = any("\u0590" <= ch <= "\u05FF" for ch in full)
    has_lat = any("a" <= ch.lower() <= "z" for ch in full)
    return has_lat and not has_heb


def translate_contact_names(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    """
    Translates English names of Israeli contacts to standard Hebrew with high confidence.
    Preserves original English name in 'Nickname' field so dual English/Hebrew search works natively.
    Returns modified DataFrame and audit list of all translations made.
    """
    df = df.copy()
    phone_cols = [c for c in df.columns if "Phone" in c and "Value" in c]
    translation_log: list[dict[str, str]] = []

    for idx, row in df.iterrows():
        if not has_israeli_phone_number(row, phone_cols):
            continue

        fn_raw = str(row.get("First Name", "")).strip() if pd.notna(row.get("First Name")) else ""
        ln_raw = str(row.get("Last Name", "")).strip() if pd.notna(row.get("Last Name")) else ""

        if not is_latin_name(fn_raw, ln_raw):
            continue

        # Check inverted name pattern: e.g. "Storfer" (First) "Dalia" (Last)
        if fn_raw.lower() == "storfer" and ln_raw.lower() == "dalia":
            heb_first = "דליה"
            heb_last = "שטורפר"
        else:
            h_fn = HEB_FIRST_MAP.get(fn_raw.lower())
            h_ln = HEB_LAST_MAP.get(ln_raw.lower()) if ln_raw else ""

            # Only translate if full name is recognized with high confidence
            if fn_raw and ln_raw and h_fn and h_ln:
                heb_first = h_fn
                heb_last = h_ln
            elif fn_raw and not ln_raw and h_fn:
                heb_first = h_fn
                heb_last = ""
            else:
                continue

        orig_full = f"{fn_raw} {ln_raw}".strip()
        heb_full = f"{heb_first} {heb_last}".strip()

        # Preserve English full name in Nickname if Nickname is empty
        curr_nick = str(row.get("Nickname", "")).strip() if pd.notna(row.get("Nickname")) else ""
        if not curr_nick:
            df.at[idx, "Nickname"] = orig_full

        df.at[idx, "First Name"] = heb_first
        df.at[idx, "Last Name"] = heb_last

        p1 = str(row.get("Phone 1 - Value", "")).strip() if pd.notna(row.get("Phone 1 - Value")) else ""
        org = str(row.get("Organization Name", "")).strip() if pd.notna(row.get("Organization Name")) else ""
        is_keep = bool(row.get("_keep", True))

        translation_log.append({
            "original_name": orig_full,
            "translated_name": heb_full,
            "heb_first": heb_first,
            "heb_last": heb_last,
            "phone": p1,
            "org": org,
            "is_keep": is_keep,
        })

    return df, translation_log


def write_translation_report(
    translations: list[dict[str, str]],
    output_path: Path,
) -> None:
    """Write a formatted Markdown table of all name translations for human review."""
    lines = [
        "# Contact Name Translations (English → Hebrew)\n",
        f"Total contacts translated with high confidence: **{len(translations)}**\n",
        "> **Note:** The original English name has been preserved in the `Nickname` field ",
        "> of each contact to ensure phone dialer, Spotlight, and WhatsApp search continue to work in English.\n",
        "\n",
        "| # | Original English Name | Translated Hebrew Name | Primary Phone | Organization |",
        "|---|---|---|---|---|",
    ]

    for i, t in enumerate(translations, 1):
        orig = t["original_name"]
        heb = t["translated_name"]
        phone = t["phone"]
        org = t["org"] or "—"
        lines.append(f"| {i} | {orig} | {heb} | {phone} | {org} |")

    lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")
