"""
Name translation module for contacts.
1. Translates Latin/English names to standard Hebrew with high confidence, preserving original English name in 'Nickname'.
2. Translates Hebrew names to English transliterations and populates 'Nickname' for contacts with Hebrew display names,
   allowing them to be searched with an English keyboard in WhatsApp, Spotlight, and Google Contacts.
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
    "arnon": "ארנון",
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

# Reverse mapping for Hebrew -> English transliterations (Title Cased)
ENG_FIRST_MAP: dict[str, str] = {v: k.title() for k, v in HEB_FIRST_MAP.items()}
# Add specific native Hebrew first names
ENG_FIRST_MAP.update({
    "אבשלום": "Avshalom",
    "אורי": "Uri",
    "אורן": "Oren",
    "אורנה": "Orna",
    "אידי": "Iddy",
    "אייל": "Eyal",
    "אילן": "Ilan",
    "אילת": "Ayelet",
    "אלון": "Alon",
    "אלירן": "Eliran",
    "אמיר": "Amir",
    "אסף": "Assaf",
    "אסתי": "Esti",
    "אפרת": "Efrat",
    "בועז": "Boaz",
    "בני": "Benny",
    "ברוך": "Baruch",
    "ברק": "Barak",
    "גולן": "Golan",
    "גיא": "Guy",
    "גלית": "Galit",
    "גלעד": "Gilad",
    "דורון": "Doron",
    "דניאל": "Daniel",
    "זואי": "Zoe",
    "זיו": "Ziv",
    "חביבה": "Haviva",
    "חגי": "Hagai",
    "חי": "Hai",
    "חנה": "Hana",
    "טיראן": "Tiran",
    "טל": "Tal",
    "יגאל": "Yigal",
    "יהודה": "Yehuda",
    "יוכי": "Yochi",
    "יוסי": "Yossi",
    "יוסף": "Yosef",
    "יניב": "Yaniv",
    "יעל": "Yael",
    "יעקב": "Yaacov",
    "יערה": "Yaara",
    "יערי": "Yaari",
    "ירדן": "Yarden",
    "ירון": "Yaron",
    "ישראל": "Israel",
    "ליאור": "Lior",
    "לימור": "Limor",
    "מוריה": "Moria",
    "מיכאל": "Michael",
    "מירי": "Miri",
    "מני": "Meny",
    "מתן": "Matan",
    "נורית": "Nurit",
    "נטלי": "Natali",
    "ניר": "Nir",
    "נמרוד": "Nimrod",
    "נתי": "Nati",
    "סהר": "Sahar",
    "סיגל": "Sigal",
    "סמדר": "Smadar",
    "עדיאל": "Adiel",
    "עדינה": "Adina",
    "עומר": "Omer",
    "עומרי": "Omri",
    "עזריה": "Azaria",
    "עמוס": "Amos",
    "עמית": "Amit",
    "עמרי": "Omri",
    "ענבל": "Inbal",
    "עפרי": "Ofri",
    "ערן": "Eran",
    "פנינה": "Pnina",
    "צביקה": "Tzvika",
    "צחי": "Tsahi",
    "ציפי": "Tzipi",
    "קובי": "Kobi",
    "קרני": "Karni",
    "רביב": "Raviv",
    "רון": "Ron",
    "רונן": "Ronen",
    "רועי": "Roi",
    "רות": "Ruth",
    "ריטה": "Rita",
    "רן": "Ran",
    "שגיב": "Sagiv",
    "שחר": "Shahar",
    "שי": "Shai",
    "שיר": "Shir",
    "שלומי": "Shlomi",
    "שלומית": "Shlomit",
    "שלמה": "Shlomo",
    "שמואל": "Shmuel",
    "שמעון": "Shimon",
    "תום": "Tom",
    "תומר": "Tomer",
    "תמי": "Tami",
    "תמר": "Tamar",
})

# Reverse mapping for Hebrew -> English last names (Title Cased)
ENG_LAST_MAP: dict[str, str] = {v: k.title() for k, v in HEB_LAST_MAP.items()}
# Add specific native Hebrew last names
ENG_LAST_MAP.update({
    "אגוזי": "Egozi",
    "אופק": "Ofek",
    "אורנשטיין": "Orenstein",
    "איזינגר": "Eisinger",
    "אלרום": "Elrom",
    "בורוכוב": "Borochov",
    "בז'רנו": "Bejerano",
    "בירנבאום": "Birnbaum",
    "בן דרור": "Ben Dror",
    "בן חמו": "Ben Hamo",
    "בן יוסף": "Ben Yosef",
    "בר": "Bar",
    "גבריאל": "Gabriel",
    "גדות": "Gadot",
    "גולדין": "Goldin",
    "גור": "Gur",
    "גילאור": "Gilor",
    "גלאור": "Galor",
    "גרבר": "Gerber",
    "דוד": "David",
    "הרצג": "Herzog",
    "ויזניצר": "Viznitzer",
    "וייזל": "Weisel",
    "וייסר": "Weisser",
    "וינר": "Weiner",
    "זגורי": "Zaguri",
    "זלמנוב": "Zalmanov",
    "טל": "Tal",
    "יוסף": "Yosef",
    "יצחקי": "Itzhaki",
    "כהן": "Cohen",
    "כהנא": "Kahana",
    "כידן": "Kidan",
    "לגיל": "Lagil",
    "לוינסון": "Levinson",
    "מגידס": "Megides",
    "מרוז": "Meroz",
    "מרטון": "Marton",
    "סייף": "Seif",
    "סמט": "Semet",
    "סרי": "Sari",
    "פוסטולסקי": "Postolsky",
    "פרצק": "Partzok",
    "צובארי": "Tzuberi",
    "קורן": "Koren",
    "קצב": "Katzav",
    "קציר": "Katzir",
    "קריסטל": "Crystal",
    "רויז": "Royz",
    "רם": "Ram",
    "שחר": "Shahar",
    "שליט": "Shalit",
    "שיראי": "Shirai",
})

# Dictionary of Hebrew descriptor words and service phrases commonly used in contact names
HEB_DESCRIPTOR_MAP: dict[str, str] = {
    "כדורסל": "Basketball",
    "שיפוצים": "Renovations",
    "שירותי נקיון": "Cleaning Services",
    "ערבית": "Arabic",
    "צרעה": "Tzora",
    "טויוטא": "Toyota",
    "אינסטלטור": "Plumber",
    "אינסטלטור ממליץ מהעוגן": "Plumber",
    "שירותי אינסטלציה מדרג": "Plumbing",
    "שירותי אינסטלציה": "Plumbing",
    "יוצאים ללמוד": "Yotzim Lilmod",
    "מזגנים מעולה מומלץ על שמוליק": "AC",
    "מזגנים": "AC",
    "מיזוג אוויר": "AC",
    "טכנאי מיזוג אוויר": "AC Technician",
    "שער": "Gate",
    "פעמונים": "Paamonim",
    "פיקוח וליווי פרויקטים": "Project Management",
    "יפן": "Japan",
    "הנגר": "Carpenter",
    "דשא": "Lawn",
    "מתקן דוד הזית": "Boiler Repair",
    "דודי שמש או טל סחר ?": "Solar Boilers",
    "תמ״א דיירים סנהדרין": "TAMA Sanhedrin",
    "סוכן מיטב": "Meitav Agent",
    "סוכן הביטוח שלך": "Insurance Agent",
    "-סוכן הביטוח שלך": "Insurance Agent",
    "פסיכולוג": "Psychologist",
    "קבלן": "Contractor",
    "קבלן האופק 3": "Contractor",
    "מנהלת מוסך מאיר": "Meir Garage Manager",
    "מוסך": "Garage",
    "קיאקים מכמורת": "Mikhmoret Kayaks",
    "קיאקים הרצליה": "Herzliya Kayaks",
    "קייאקים": "Kayaks",
    "קיאקים": "Kayaks",
    "חיים גרר": "Towing",
    "גרר": "Towing",
    "שכנה אריק 6": "Neighbor",
    "שכנה": "Neighbor",
    "פילטר למקרר": "Fridge Filter",
    "חשמלאי לוי": "Electrician",
    "חשמלאי": "Electrician",
    "השכרת מקרן": "Projector Rental",
    "מקרן": "Projector",
    "בואונדבר": "BoNedaber",
    "שיננית": "Dental Hygienist",
    "מתווך בבלי": "Realtor",
    "מתווך": "Realtor",
    "פאזלים": "Puzzles",
    "רשיון אקדח שוהם": "Gun License",
    "דיגיטל": "Digital",
    "מחסן חלפים טויוטה": "Toyota Parts",
    "Wooden Dreams": "Wooden Dreams",
    "STW": "STW",
}

# Known surnames embedded inside compound descriptor notes
KNOWN_EXTRA_SURNAMES: dict[str, str] = {
    "סבאח": "Sabah",
    "שנאן": "Shanan",
    "משה": "Moshe",
    "גור": "Gur",
    "דויטשר": "Deutscher",
    "רובינשטיין": "Rubinstein",
    "אביטל": "Avital",
    "לוינסון": "Levinson",
    "גולדנברג": "Goldenberg",
    "מלניק": "Melnik",
    "רווח": "Revach",
    "באר": "Beer",
    "מנור": "Manor",
    "סאיג": "Saig",
    "להב אבנשטיין": "Lahav Evenstein",
    "לוי": "Levy",
    "דרוקר": "Drucker",
    "וולאנה": "Wellana",
}


def translate_descriptor_part(text: str) -> str:
    """Translate a Hebrew note or service descriptor (e.g. 'כדורסל' -> 'Basketball')."""
    text = text.strip()
    if not text:
        return ""
    if text in HEB_DESCRIPTOR_MAP:
        return HEB_DESCRIPTOR_MAP[text]

    # Check compound patterns like 'סבאח כדורסל' or 'משה ערבית'
    for s_heb, s_eng in KNOWN_EXTRA_SURNAMES.items():
        if s_heb in text:
            rest = text.replace(s_heb, "").strip().strip("-").strip()
            desc_eng = ""
            for d_heb, d_eng in sorted(HEB_DESCRIPTOR_MAP.items(), key=lambda x: len(x[0]), reverse=True):
                if d_heb in rest:
                    desc_eng = d_eng
                    break
            return f"{s_eng} {desc_eng}".strip()

    # Search for known descriptor substrings
    for d_heb, d_eng in sorted(HEB_DESCRIPTOR_MAP.items(), key=lambda x: len(x[0]), reverse=True):
        if d_heb in text:
            return d_eng
    return ""


def is_latin_name(first: str, last: str) -> bool:
    """Return True if the name contains Latin letters and no Hebrew characters."""
    full = f"{first} {last}".strip()
    if not full:
        return False
    has_heb = any("\u0590" <= ch <= "\u05FF" for ch in full)
    has_lat = any("a" <= ch.lower() <= "z" for ch in full)
    return has_lat and not has_heb


def has_hebrew_characters(first: str, last: str) -> bool:
    """Return True if the name contains Hebrew characters."""
    full = f"{first} {last}".strip()
    return any("\u0590" <= ch <= "\u05FF" for ch in full)


def translate_contact_names(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, list[dict[str, str]]]]:
    """
    Bidirectional translation:
    1. English -> Hebrew: Translates English names to standard Hebrew with high confidence,
       preserving the original English name in the 'Nickname' field.
    2. Hebrew -> English Nickname: For contacts with Hebrew display names, translates their
       name to English transliteration and sets it as 'Nickname' (if Nickname is empty),
       enabling English keyboard searches in WhatsApp, Spotlight, and Google Contacts.
    Returns:
        (modified_df, {
            "english_to_hebrew": [...],
            "hebrew_to_english_nickname": [...]
        })
    """
    df = df.copy()
    e2h_log: list[dict[str, str]] = []
    h2e_log: list[dict[str, str]] = []

    for idx, row in df.iterrows():
        fn_raw = str(row.get("First Name", "")).strip() if pd.notna(row.get("First Name")) else ""
        ln_raw = str(row.get("Last Name", "")).strip() if pd.notna(row.get("Last Name")) else ""
        curr_nick = str(row.get("Nickname", "")).strip() if pd.notna(row.get("Nickname")) else ""
        p1 = str(row.get("Phone 1 - Value", "")).strip() if pd.notna(row.get("Phone 1 - Value")) else ""
        org = str(row.get("Organization Name", "")).strip() if pd.notna(row.get("Organization Name")) else ""
        is_keep = bool(row.get("_keep", True))

        # Direction 1: English -> Hebrew
        if is_latin_name(fn_raw, ln_raw):
            # Check inverted name pattern: e.g. "Storfer" (First) "Dalia" (Last)
            if fn_raw.lower() == "storfer" and ln_raw.lower() == "dalia":
                heb_first = "דליה"
                heb_last = "שטורפר"
            else:
                h_fn = HEB_FIRST_MAP.get(fn_raw.lower())
                h_ln = HEB_LAST_MAP.get(ln_raw.lower()) if ln_raw else ""

                # Only translate if recognized with high confidence
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

            # Preserve original English full name in Nickname
            if not curr_nick:
                df.at[idx, "Nickname"] = orig_full

            df.at[idx, "First Name"] = heb_first
            df.at[idx, "Last Name"] = heb_last

            e2h_log.append({
                "original_name": orig_full,
                "translated_name": heb_full,
                "heb_first": heb_first,
                "heb_last": heb_last,
                "phone": p1,
                "org": org,
                "is_keep": is_keep,
            })

        # Direction 2: Hebrew -> English Nickname (including service/role descriptors)
        elif has_hebrew_characters(fn_raw, ln_raw):
            e_fn = ENG_FIRST_MAP.get(fn_raw) or (fn_raw if is_latin_name(fn_raw, "") else "")
            e_ln = ENG_LAST_MAP.get(ln_raw) if ln_raw else ""

            eng_nick = ""
            match_type = ""
            if fn_raw and ln_raw and e_fn and e_ln:
                eng_nick = f"{e_fn} {e_ln}"
                match_type = "Full Name"
            elif fn_raw and e_fn and ln_raw:
                desc = translate_descriptor_part(ln_raw)
                if desc:
                    eng_nick = f"{e_fn} {desc}"
                    match_type = "Name + Descriptor"
                else:
                    eng_nick = e_fn
                    match_type = "First Name Only"
            elif fn_raw and e_fn:
                eng_nick = e_fn
                match_type = "First Name Only"

            if eng_nick and not curr_nick:
                df.at[idx, "Nickname"] = eng_nick
                h2e_log.append({
                    "hebrew_name": f"{fn_raw} {ln_raw}".strip(),
                    "english_nickname": eng_nick,
                    "match_type": match_type,
                    "phone": p1,
                    "org": org,
                    "is_keep": is_keep,
                })

    return df, {
        "english_to_hebrew": e2h_log,
        "hebrew_to_english_nickname": h2e_log,
    }


def write_translation_report(
    results: dict[str, list[dict[str, str]]],
    output_path: Path,
) -> None:
    """Write a formatted Markdown document of all name translations and nicknames for human review."""
    e2h = results.get("english_to_hebrew", [])
    h2e = results.get("hebrew_to_english_nickname", [])

    lines = [
        "# Contact Name Translations & Nickname Mapping\n",
        f"- English → Hebrew display name translations: **{len(e2h)}**",
        f"- Hebrew → English search nicknames added: **{len(h2e)}**\n",
        "> **Note:** English names are stored in the `Nickname` field of contacts so that ",
        "> phone dialers, Apple Spotlight, WhatsApp, and Google Contacts can search in English.\n",
        "\n",
        "## 1. English → Hebrew Translations (Display Name Changed)\n",
        "The following contacts had their display names converted from English to Hebrew with high confidence, ",
        "and their original English name preserved in `Nickname`:\n",
        "\n",
        "| # | Original English Name | Translated Hebrew Name | Primary Phone | Organization |",
        "|---|---|---|---|---|",
    ]

    for i, t in enumerate(e2h, 1):
        orig = t["original_name"]
        heb = t["translated_name"]
        phone = t["phone"]
        org = t["org"] or "—"
        lines.append(f"| {i} | {orig} | {heb} | {phone} | {org} |")

    lines.append("\n\n## 2. Hebrew Contacts Given English Nicknames\n")
    lines.append(
        "The following Hebrew contacts retained their Hebrew display names, while an English transliteration "
        "was added to their `Nickname` field so you can find them by typing in English:\n\n"
    )
    lines.append("| # | Hebrew Display Name | Added English Nickname | Match Type | Primary Phone | Organization |")
    lines.append("|---|---|---|---|---|---|")

    for i, t in enumerate(h2e, 1):
        heb = t["hebrew_name"]
        eng = t["english_nickname"]
        mtype = t["match_type"]
        phone = t["phone"]
        org = t["org"] or "—"
        lines.append(f"| {i} | {heb} | {eng} | {mtype} | {phone} | {org} |")

    lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")
