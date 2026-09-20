"""Shared utility functions for the PFE Job Automation system."""

import re
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlunparse


# Query parameters that are known to be tracking-only and safe to strip.
# These do NOT affect which job a URL points to.
_TRACKING_PARAMS: set[str] = {
    # Google Analytics / UTM family
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    # Generic referral / tracking
    "ref",
    "refId",
    "ref_id",
    # Lever-specific tracking (NOT identity — Lever identity is in the path UUID)
    "lever-origin",
    "lever-source",
    # LinkedIn tracking
    "trk",
    "trackingId",
    "refId",
    # Miscellaneous analytics
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}

# NOTE: gh_jid is intentionally NOT included. For Greenhouse custom-domain
# boards (e.g. careers.datadoghq.com), gh_jid is the primary job identifier
# in the URL and removing it could change which job the URL resolves to.


def canonicalize_url(url: str) -> str:
    """Return a deterministic canonical form of *url* for deduplication.

    Rules applied:
    1. Normalize scheme to ``https`` for http/https URLs.
    2. Lowercase the hostname.
    3. Normalize percent-encoding in path (decode unreserved characters).
    4. Remove a single trailing ``/`` from the path (unless the path is ``/``).
    5. Remove known tracking-only query parameters (see ``_TRACKING_PARAMS``).
    6. Sort remaining query parameters for deterministic ordering.
    7. Strip empty fragment.

    The function is intentionally conservative — it does NOT strip all query
    parameters and preserves provider-specific identity parameters such as
    ``gh_jid``.
    """
    if not url:
        return url

    url = url.strip()
    parsed = urlparse(url)

    # 1. Normalize scheme
    scheme = parsed.scheme.lower()
    if scheme == "http":
        scheme = "https"

    # 2. Lowercase hostname
    netloc = parsed.netloc.lower()

    # 3. Normalize percent-encoding in path (decode then re-encode)
    path = _normalize_path_encoding(parsed.path)

    # 4. Remove trailing slash (but keep bare "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # 5 & 6. Filter tracking params and sort remaining
    query = _filter_query(parsed.query)

    # 7. Strip empty fragment
    fragment = parsed.fragment if parsed.fragment else ""

    return urlunparse((scheme, netloc, path, parsed.params, query, fragment))


def _normalize_path_encoding(path: str) -> str:
    """Decode then re-encode path to normalize percent-encoding differences.

    e.g. ``/jobs/%2F123`` and ``/jobs//123`` remain distinct,
    but ``/jobs/stage%20pfe`` and ``/jobs/stage pfe`` unify.
    """
    if not path:
        return path
    # Decode first, then re-encode with safe characters preserved
    decoded = unquote(path)
    # Re-encode only the characters that must be encoded in a URL path
    return quote(decoded, safe="/:@!$&'()*+,;=-._~")


def _filter_query(query_string: str) -> str:
    """Remove tracking parameters and sort remaining for deterministic output."""
    if not query_string:
        return ""

    params = parse_qs(query_string, keep_blank_values=True)
    filtered: dict[str, list[str]] = {}

    for key, values in params.items():
        key_lower = key.lower()
        # Skip any parameter matching a known tracking param (case-insensitive)
        if key_lower in {p.lower() for p in _TRACKING_PARAMS}:
            continue
        # Skip utm_* wildcard
        if key_lower.startswith("utm_"):
            continue
        filtered[key] = values

    if not filtered:
        return ""

    # Sort by key for deterministic output; preserve multi-value order
    sorted_items: list[tuple[str, str]] = []
    for key in sorted(filtered.keys()):
        for val in filtered[key]:
            sorted_items.append((key, val))

    return urlencode(sorted_items)
