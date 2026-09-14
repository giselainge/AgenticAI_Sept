import re
import unicodedata

from invoice_parser.schema import NULL_VALUE


def fold_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return normalized.lower()


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def null_if_empty(value: str | None) -> str:
    value = normalize_space(value or "")
    return value if value else NULL_VALUE


def normalize_money(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().replace(" ", "")
    value = re.sub(r"^[^\d]+|[^\d,.]+$", "", value)
    if "." in value and "," in value:
        value = value.replace(".", "").replace(",", ".")
    elif "," in value:
        value = value.replace(",", ".")
    elif re.fullmatch(r"\d{5,}", value):
        value = f"{value[:-2]}.{value[-2:]}"
    try:
        amount = float(value)
    except ValueError:
        return None
    if amount > 100000:
        return None
    return f"{amount:.2f}"

