from __future__ import annotations

import re

REGION_PROFILE_FALLBACKS = {
    "arab": "novakid_mena",
    "cz": "novakid_czech",
    "de": "novakid_de",
    "es": "novakidespana",
    "fr": "novakid_france",
    "global": "novakid_global",
    "he": "novakid_israel",
    "il": "novakid_israel",
    "it": "novakiditalia",
    "jp": "novakid_jp",
    "kr": "novakid_korea",
    "pl": "novakidpolska",
    "ro": "novakid_romania",
    "school": "novakidschool",
    "tr": "novakidturkiye",
    "ww": "novakid_global",
}
REGION_PROFILE_RE = re.compile(r"\br:([a-z0-9_-]+)\s*-\s*@?(novakid[a-z0-9_.]*)\b", re.IGNORECASE)
REGION_CODE_RE = re.compile(r"\[r:([a-z0-9_-]+)\]", re.IGNORECASE)


def region_profile_from_ad_names(*names: object) -> tuple[str, str | None] | None:
    for value in names:
        text = str(value or "").strip()
        explicit = REGION_PROFILE_RE.search(text)
        if explicit:
            return explicit.group(1).lower(), explicit.group(2).lower()
    for value in names:
        region = REGION_CODE_RE.search(str(value or ""))
        if region:
            region_code = region.group(1).lower()
            return region_code, REGION_PROFILE_FALLBACKS.get(region_code)
    return None


def profile_from_ad_names(*names: object) -> str | None:
    """Resolve a Novakid Instagram username from the ad naming convention."""
    match = region_profile_from_ad_names(*names)
    return match[1] if match else None
