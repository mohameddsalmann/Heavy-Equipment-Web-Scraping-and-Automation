from __future__ import annotations

import re
from typing import Optional, Set
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Common tracking / click attribution parameters across digital marketplaces
TRACKING_PARAMS: Set[str] = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "msclkid",
    "dclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "ref_url",
    "_ga",
    "_gl",
    "trk",
    "tracking",
    "source",
}


def canonicalize_url(url: str, strip_all_query: bool = False) -> str:
    """
    Canonicalizes a listing URL by:
    1. Normalizing scheme and host to lowercase.
    2. Stripping URL fragments/anchors (#...).
    3. Stripping marketing/tracking parameters (e.g. utm_*, fbclid, gclid, ref).
    4. Stripping redundant trailing slashes on the path.
    5. Alphabetically sorting any remaining functional query parameters.
    """
    if not url or not url.strip():
        return ""

    raw_url = url.strip()
    parsed = urlparse(raw_url)

    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()

    # Normalize path: preserve root '/' but remove trailing slashes on sub-paths
    path = parsed.path
    if path and path != "/":
        path = path.rstrip("/")

    if strip_all_query:
        query = ""
    else:
        # Filter tracking and marketing parameters
        query_pairs = parse_qsl(parsed.query, keep_blank_values=False)
        clean_pairs = [
            (k, v)
            for k, v in query_pairs
            if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
        ]
        # Sort remaining query parameters deterministically
        clean_pairs.sort(key=lambda item: (item[0], item[1]))
        query = urlencode(clean_pairs)

    # Reconstruct clean canonical URL without fragment
    return urlunparse((scheme, netloc, path, "", query, ""))


def extract_stable_external_id(
    url: str, raw_external_id: Optional[str] = None
) -> str:
    """
    Provider contract requirement:
    Ensures that every marketplace listing external_id is stable and independent of:
    - query parameters
    - tracking tokens
    - URL fragments

    If raw_external_id is provided, it is cleaned of query parameters or fragments.
    If not provided, the external_id is derived from the canonicalized URL path.
    """
    if raw_external_id and raw_external_id.strip():
        cleaned_id = raw_external_id.strip()
        # If the provider mistakenly passed a full URL or param string as external_id
        if "?" in cleaned_id or "#" in cleaned_id:
            cleaned_id = cleaned_id.split("?")[0].split("#")[0].rstrip("/")
        # If external_id was a full URL, get the last path segment or canonical URL
        if cleaned_id.startswith("http://") or cleaned_id.startswith("https://"):
            canon = canonicalize_url(cleaned_id, strip_all_query=True)
            # Use last meaningful segment if present
            segments = [s for s in urlparse(canon).path.split("/") if s]
            return segments[-1] if segments else canon
        return cleaned_id

    # Fallback: derive stable external ID from canonical URL path
    canon = canonicalize_url(url, strip_all_query=True)
    segments = [s for s in urlparse(canon).path.split("/") if s]
    if segments:
        return segments[-1]
    return canon
