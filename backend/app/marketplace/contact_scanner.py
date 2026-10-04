"""Detect attempts to exchange contact details before the permitted stage.

The scanner is deliberately conservative: it flags and rejects rather than
trying to rewrite user content. It is applied server-side to project text,
bids, messages, filenames and profile content.
"""

import re
import unicodedata
from dataclasses import dataclass

_EMAIL = re.compile(r"[\w.+-]+\s*@\s*[\w-]+(?:\s*\.\s*[\w-]+)+", re.I)
_OBFUSCATED_EMAIL = re.compile(
    r"[\w.+-]+\s*[\[(<{]?\s*(?:at|@)\s*[\])>}]?\s*[\w-]+\s*[\[(<{]?\s*(?:dot|\.)\s*[\])>}]?\s*(?:com|net|org|io|co|us|biz|info|edu|gov)\b",
    re.I,
)
_URL = re.compile(
    r"(?:https?://|www\.)\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|co|us|biz|info|app|dev|me|ly|link)\b(?:/\S*)?",
    re.I,
)
_SOCIAL = re.compile(
    r"\b(?:facebook|fb|instagram|insta|linkedin|twitter|x\.com|telegram|whatsapp|signal|snapchat|tiktok|skype|discord)\b",
    re.I,
)
_PAYMENT_HANDLE = re.compile(
    r"\b(?:venmo|cashapp|cash\s*app|zelle|paypal\.me|apple\s*pay|google\s*pay)\b|(?<!\w)\$[a-z][\w]{2,}",
    re.I,
)
_DIGIT_WORDS = {
    "zero": "0",
    "oh": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}
_DIGIT_WORD = re.compile(r"\b(" + "|".join(_DIGIT_WORDS) + r")\b", re.I)
_ADDRESS = re.compile(
    r"\b\d{1,6}\s+(?:[A-Za-z0-9.]+\s+){1,4}(?:street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|lane|ln|court|ct|way|place|pl|highway|hwy)\b\.?",
    re.I,
)


@dataclass(frozen=True)
class ContactFinding:
    kind: str


def _phone_like(text: str) -> bool:
    normalized = _DIGIT_WORD.sub(lambda m: _DIGIT_WORDS[m.group(1).lower()], text)
    # Collapse digits separated by common obfuscation characters.
    runs = re.findall(r"\+?\d(?:[\s().\-_*/|]*\d){6,}", normalized)
    return any(len(re.sub(r"\D", "", run)) >= 7 for run in runs)


def find_contact_info(text: str | None) -> list[ContactFinding]:
    if not text:
        return []
    text = unicodedata.normalize("NFKC", text)
    kinds: list[str] = []
    if _EMAIL.search(text) or _OBFUSCATED_EMAIL.search(text):
        kinds.append("email")
    if _phone_like(text):
        kinds.append("phone")
    if _URL.search(text):
        kinds.append("website")
    if _SOCIAL.search(text):
        kinds.append("social")
    if _PAYMENT_HANDLE.search(text):
        kinds.append("payment_handle")
    if _ADDRESS.search(text):
        kinds.append("street_address")
    return [ContactFinding(k) for k in kinds]


def contains_contact_info(*texts: str | None) -> bool:
    return any(find_contact_info(t) for t in texts)


def describe_findings(*texts: str | None) -> str | None:
    kinds = sorted({f.kind for t in texts for f in find_contact_info(t)})
    if not kinds:
        return None
    return (
        "Contact details ("
        + ", ".join(k.replace("_", " ") for k in kinds)
        + ") can't be shared until the project is unlocked. "
        "Please remove them and keep communication on SPECVision."
    )
