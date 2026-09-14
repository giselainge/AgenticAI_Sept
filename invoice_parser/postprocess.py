"""Shared semantic normalization for every extraction candidate.

The frozen July extractor and model-backed extractors intentionally remain
independent.  Their rows pass through this module before validation and
comparison so a malformed value cannot earn field-completion credit.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from invoice_parser.schema import FIELDNAMES, NULL_VALUE
from invoice_parser.text_utils import fold_text, normalize_money, normalize_space


DATE_FIELDS = {
    "invoice_date",
    "payment_due_date",
    "consumption_start_date",
    "consumption_end_date",
}
MONEY_FIELDS = {"subtotal_value", "total_vat", "total_value"}
ADDRESS_FIELDS = {"provider_address", "buyer_address"}
NAME_FIELDS = {"provider_name", "buyer_name", "service_plan_name"}
VAT_FIELDS = {"provider_vat_number", "buyer_vat_number"}

_MISSING = {"", "null", "none", "nan", "n/a", "unknown"}
_FINANCIAL_LABEL = re.compile(
    r"\b(?:total|subtotal|iva|vat|amount(?:\s+due)?|bill\s+cost|"
    r"valor(?:\s+da\s+fatura|\s+a\s+pagar)?|montante|custo|preco)\b",
    re.IGNORECASE,
)
_CURRENCY_MARKER = re.compile(r"(?:€|\$|£|₺|\b(?:EUR|USD|GBP|TRY|TL)\b)", re.IGNORECASE)
_MONEY_TOKEN = re.compile(r"(?<!\d)\d{1,3}(?:[ .]\d{3})*(?:[,.]\d{2})(?!\d)")
_DATE_TOKEN = re.compile(r"\b(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})\b")


def _missing(value: Any) -> bool:
    return normalize_space(str(value or "")).casefold() in _MISSING


def _date(value: str) -> str | None:
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def _quantity(value: str) -> str | None:
    compact = normalize_space(value)
    match = re.fullmatch(
        r"([+-]?\d[\d .]*(?:,\d+)?|[+-]?\d+(?:\.\d+)?)\s*(?:kwh|m3|m\^3|m³|gb|min|sms)?",
        compact,
        re.IGNORECASE,
    )
    if not match:
        return None
    number = match.group(1).replace(" ", "")
    if "," in number and "." in number:
        number = number.replace(".", "").replace(",", ".")
    elif "," in number:
        number = number.replace(",", ".")
    try:
        parsed = Decimal(number)
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return f"{parsed:.2f}"


def _financial_contamination(value: str) -> bool:
    folded = fold_text(value)
    return bool(_CURRENCY_MARKER.search(value) or _FINANCIAL_LABEL.search(folded))


def _identity_contamination(value: str) -> bool:
    return _financial_contamination(value) or bool(_MONEY_TOKEN.search(value) or _DATE_TOKEN.search(value))


def _vat(value: str) -> str | None:
    if _identity_contamination(value):
        return None
    compact = re.sub(r"[\s.\-/]", "", value).upper()
    if not re.fullmatch(r"(?:[A-Z]{2})?[A-Z0-9]{8,15}", compact):
        return None
    return compact if sum(character.isdigit() for character in compact) >= 6 else None


def _currency(value: str) -> str | None:
    folded = fold_text(value).replace(".", "")
    aliases = {
        "€": "EUR",
        "eur": "EUR",
        "euro": "EUR",
        "euros": "EUR",
        "$": "USD",
        "usd": "USD",
        "£": "GBP",
        "gbp": "GBP",
        "₺": "TL",
        "tl": "TL",
        "try": "TL",
    }
    return aliases.get(folded)


def _invoice_type(value: str) -> str | None:
    folded = fold_text(value).replace("_", " ").strip()
    aliases = {
        "electricity": "electricity",
        "electricidade": "electricity",
        "energia eletrica": "electricity",
        "water": "water",
        "agua": "water",
        "natural gas": "natural gas",
        "gas natural": "natural gas",
        "gas": "natural gas",
        "telecom": "telecom",
        "telecommunications": "telecom",
        "telecomunicacoes": "telecom",
        "unsupported": "unsupported",
    }
    return aliases.get(folded)


def _unit(value: str) -> str | None:
    compact = re.sub(r"\s+", "", value).casefold()
    aliases = {
        "kwh": "kWh",
        "m3": "m3",
        "m^3": "m3",
        "m³": "m3",
        "gb": "GB",
        "min": "min",
        "minute": "min",
        "minutes": "min",
        "sms": "SMS",
    }
    return aliases.get(compact)


def sanitize_invoice_row(row: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Return one normalized schema row and names of rejected fields.

    Rejection messages contain field names only, keeping invoice contents out of
    logs while making every post-processing decision auditable.
    """
    cleaned: dict[str, str] = {}
    rejected: list[str] = []

    for field in FIELDNAMES:
        raw = row.get(field)
        if _missing(raw):
            cleaned[field] = NULL_VALUE
            continue
        value = normalize_space(str(raw))
        normalized: str | None = value

        if field in DATE_FIELDS:
            normalized = _date(value)
        elif field in MONEY_FIELDS:
            normalized = None if value.lstrip().startswith("-") else normalize_money(value)
        elif field == "units_of_consumption":
            normalized = _quantity(value)
        elif field == "currency":
            normalized = _currency(value)
        elif field == "invoice_type":
            normalized = _invoice_type(value)
        elif field == "valid_invoice":
            normalized = value.casefold() if value.casefold() in {"true", "false"} else None
        elif field == "unit_type":
            normalized = _unit(value)
        elif field in VAT_FIELDS:
            normalized = _vat(value)
        elif field in ADDRESS_FIELDS and _financial_contamination(value):
            normalized = None
        elif field in NAME_FIELDS and _identity_contamination(value):
            normalized = None
        elif field == "invoice_number" and _financial_contamination(value):
            normalized = None

        if normalized is None or _missing(normalized):
            cleaned[field] = NULL_VALUE
            rejected.append(field)
        else:
            cleaned[field] = str(normalized)

    existing_warning = cleaned.get("extraction_warnings", NULL_VALUE)
    warnings = [] if _missing(existing_warning) else [part.strip() for part in existing_warning.split("|") if part.strip()]
    rejected = sorted(set(field for field in rejected if field != "extraction_warnings"))
    if rejected:
        warnings.append(f"Post-processing rejected implausible field(s): {', '.join(rejected)}.")
    cleaned["extraction_warnings"] = " | ".join(dict.fromkeys(warnings)) if warnings else NULL_VALUE
    return cleaned, rejected
