"""
Section and theme classification for the LRFC dashboard.

Reads caption text only: no manual tagging and no Notion at run time.
Tuned against the first 101 real Instagram captions (2026-09-21).

Section list set by the Communications Manager: Minis and Juniors are one
section, Vets included, Touch and Mixed Ability not tracked separately.

When a caption names no team, stated rules place it (see classify_section)
and the dashboard reports how many posts were placed that way.
"""

import re

SECTIONS = ["Men's 1st XV", "Men's Lions (2nd XV)", "Colts (U18)", "Women's",
            "Minis & Juniors", "Vets", "Walking Rugby", "Whole Club"]
THEMES = ["Match Content", "Club Life", "Community & Values", "Heritage"]

AGE_GROUP = re.compile(r"\b(?:u|under[\s-]?)(?:[6-9]|1[0-6])s?\b|\byear\s?[1-9]\b")
SCORE = re.compile(r"(?<![\d/])\b\d{1,3}\s?[-–—]\s?\d{1,3}\b(?![\d/])")
HISTORICAL_YEAR = re.compile(r"\b(19\d{2}|200\d|201\d)\b")

SECTION_RULES = [
    ("Men's Lions (2nd XV)", ["2nd xv", "2nds", "second xv", "lions"]),
    ("Men's 1st XV", ["1st xv", "first xv", "1sts"]),
    ("Colts (U18)", ["colts", "u18", "under 18", "under-18"]),
    ("Women's", ["women", "ladies"]),
    ("Vets", ["vets", "veterans"]),
    ("Walking Rugby", ["walking rugby"]),
    ("Minis & Juniors", ["minis", "mini rugby", "junior", "girls", "your child", "kids", "children"]),
]
HISTORICAL_WORDS = ["tbt", "throwback", "on this day", "founded", "history", "centenary",
                    "100 years", "anniversary"]
SENIOR_MEN_WORDS = ["senior squad", "senior men", "men's senior", "seniors", "pre-season",
                    "preseason", "counties 1", "league", "county colours", "warwickshire"]
MATCH_WORDS = ["kick off", "kick-off", "come on leam", "away to", "away at", "on the road",
               "match action", "match report", "action shots", "full time", "final score",
               "fixture", "tries", "try time", "brace", "opponents", "result", "win over"]
COMMUNITY_WORDS = ["one club", "one leam", "community", "inclusion", "inclusive", "mental health",
                   "concussion", "awareness", "charity", "wellbeing", "welfare"]


def clean(caption):
    text = (caption or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"[#@][\w.]+", " ", text)   # hashtags and handles say nothing about the post
    for phrase in ("british and irish lions", "british & irish lions", "british lions"):
        text = text.replace(phrase, " ")
    return text


def is_historical(text):
    return any(w in text for w in HISTORICAL_WORDS) or bool(HISTORICAL_YEAR.search(text))


def is_match(text):
    return bool(SCORE.search(text)) or any(w in text for w in MATCH_WORDS)


def classify_section(caption):
    """Returns (section, named). named=False means the caption didn't name a
    team and a stated rule placed it: historical posts -> Whole Club, match
    or senior posts -> Men's 1st XV, anything else -> Whole Club."""
    text = clean(caption)
    for section, words in SECTION_RULES:
        if any(w in text for w in words):
            return section, True
    if AGE_GROUP.search(text):
        return "Minis & Juniors", True
    if is_historical(text):
        return "Whole Club", False
    if is_match(text) or any(w in text for w in SENIOR_MEN_WORDS):
        return "Men's 1st XV", False
    return "Whole Club", False


# Theme needs stricter heritage detection than section does: in the
# centenary year "centenary" and "1926" appear in event, raffle and
# recruitment posts too. Heritage here means genuine history posts.
HERITAGE_WORDS = ["tbt", "throwback", "on this day", "history", "years ago", "years since"]
CLUB_LIFE_WORDS = ["pre-season", "preseason", "raffle", "tickets", "sponsor", "partner", "join",
                   "sign up", "taster", "nominated", "awards", "golf", "fairway", "come down"]


def classify_theme(caption):
    """Returns (theme, matched). matched=False means no rule fired and the
    post defaulted to Club Life, the broad catch-all theme."""
    text = clean(caption)
    if any(w in text for w in COMMUNITY_WORDS):
        return "Community & Values", True
    if any(w in text for w in CLUB_LIFE_WORDS):
        return "Club Life", True
    if any(w in text for w in HERITAGE_WORDS) or HISTORICAL_YEAR.search(text):
        return "Heritage", True
    if is_match(text) or "counties 1" in text or "league" in text:
        return "Match Content", True
    return "Club Life", False
