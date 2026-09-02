from __future__ import annotations

import re
import unicodedata
from typing import Optional


def normalize_text(text: Optional[str]) -> str:
    """Normalize text by lowercasing, removing extra whitespace, and standardizing characters."""
    if not text:
        return ""

    # Normalize unicode characters
    normalized = unicodedata.normalize("NFKD", text)

    # Convert to lowercase
    normalized = normalized.lower()

    # Replace special dashes/hyphens with standard hyphen
    normalized = re.sub(r"[\u2010-\u2015\u2212\uFF0D]", "-", normalized)

    # Replace multiple spaces/newlines with single space
    normalized = re.sub(r"\s+", " ", normalized).strip()

    return normalized


def extract_year(text: str) -> Optional[int]:
    """Extract a plausible machine manufacturing year (1970 to 2028)."""
    matches = re.finditer(r"\b(19[7-9]\d|20[0-2]\d)\b", text)
    for m in matches:
        val = int(m.group(1))
        start = m.start()
        prefix = text[max(0, start - 3):start]
        if ":" in prefix or "/" in prefix or "$" in prefix:
            continue
        return val
    return None


def extract_hours(text: str) -> Optional[float]:
    """Extract operating hours from text (e.g., '12,000 hours', '8500 hrs', 'hours: 4200')."""
    pattern1 = re.search(
        r"\b(\d{1,2}[,\.]?\d{3}|\d{1,5}(?:\.\d+)?)\s*(?:hours|hrs|hr|operating hours|machine hours|act\. hours)\b",
        text,
        re.IGNORECASE,
    )
    if pattern1:
        raw_val = pattern1.group(1).replace(",", "")
        try:
            val = float(raw_val)
            if 0 <= val <= 100000:
                return val
        except ValueError:
            pass

    pattern2 = re.search(
        r"\b(?:hours|hrs|meter|usage)\s*[:=-]?\s*(\d{1,2}[,\.]?\d{3}|\d{1,5}(?:\.\d+)?)\b",
        text,
        re.IGNORECASE,
    )
    if pattern2:
        raw_val = pattern2.group(1).replace(",", "")
        try:
            val = float(raw_val)
            if 0 <= val <= 100000:
                return val
        except ValueError:
            pass

    return None


def extract_price(text: str) -> Optional[float]:
    """Extract price in USD/currency from text if present."""
    pattern = re.search(
        r"(?:\$|usd\s*|\bus\s*\$)\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d{4,8})",
        text,
        re.IGNORECASE,
    )
    if pattern:
        raw_val = pattern.group(1).replace(",", "")
        try:
            return float(raw_val)
        except ValueError:
            pass
    return None


def build_model_regex(model_name: str) -> re.Pattern:
    r"""
    Build a robust boundary-aware regex pattern for matching model numbers.
    Prevents false matches on prices, larger numbers, and substring models.
    """
    cleaned = model_name.strip()

    # Pure numeric models (e.g. 930, 920, 936)
    if cleaned.isdigit():
        pattern_str = (
            r"(?<![\$€£\w,\.])"
            rf"{cleaned}"
            r"(?![a-zA-Z\d]|,\d|\.\d)"
        )
        return re.compile(pattern_str, re.IGNORECASE)

    # Alphanumeric models (e.g. 966H, 580 Super L, 580SL, 936E)
    escaped = re.escape(cleaned)
    transformed = re.sub(r"(\d+)([a-zA-Z]+)", r"\1[\\s\\-]*\2", escaped)
    transformed = re.sub(r"([a-zA-Z]+)(\d+)", r"\1[\\s\\-]*\2", transformed)
    transformed = re.sub(r"\\ ", r"[\\s\\-]+", transformed)

    pattern_str = (
        r"(?<![\$€£\w])"
        rf"{transformed}"
        r"(?![a-zA-Z\d])"
    )
    return re.compile(pattern_str, re.IGNORECASE)
