"""Open provider recognition: aliases are conveniences, never an allowlist."""

import re

from invoice_parser.text_utils import fold_text, normalize_space


PROVIDER_NAMES = {
    "epal": "EPAL",
    "eamb": "EAMB - Esposende Ambiente, EM",
    "vodafone_pt": "Vodafone Portugal, Comunicações Pessoais S.A.",
    "vodafone_tr": "VODAFONE TELEKOMUNIKASYON A.S.",
    "galp": "GALP",
    "edp": "EDP",
    "eem": "EEM - Empresa de Electricidade da Madeira, S.A.",
    "arm": "ARM - Águas e Resíduos da Madeira, S.A.",
    "unknown": "Unknown provider",
}

# Match names, never a service word such as "eletricidade" on its own.
PROVIDER_ALIASES = [
    ("vodafone_tr", r"\bvodafone telekomunikasyon\b"),
    ("vodafone_pt", r"\bvodafone\b"),
    ("epal", r"\bepal\b|\bempresa portuguesa das aguas livres\b"),
    ("eamb", r"\beamb\b|\besposende ambiente\b"),
    ("galp", r"\bgalp\b"),
    ("edp", r"\bedp\b"),
    ("eem", r"\beem\b|\bempresa de elect?ricidade da madeira\b"),
    ("arm", r"\barm\b|\baguas e residuos da madeira\b"),
]
MISSING_NAMES = {"", "null", "none", "nan", "unknown", "unknown provider"}
SELLER_LABEL = re.compile(
    r"^(?:fornecedor|prestador|emitente|emissor|provider|supplier|seller)"
    r"(?:\s*\([^)]*\))?\s*:\s*(.*)$", re.I,
)
OTHER_LABEL = re.compile(
    r"^(?:cliente|customer|buyer|adquirente|destinatario|nif|nipc|morada|address|"
    r"fatura|factura|invoice|data|date|total|consumo|consumption)\b", re.I,
)
COMPANY_SUFFIX = re.compile(r"\b(?:s\.?\s*a\.?|s\.?\s*a\.?\s*u\.?|l[dt]da\.?|em|e\.?p\.?e\.?|a\.s\.|ltd\.?|inc\.?)$", re.I)
UTILITY_NAME = re.compile(
    r"\b(?:aguas (?:de|do|da|dos|das)|servicos municipalizados|sm as|smas|"
    r"empresa de elect?ricidade|water (?:supply|utility|company))\b", re.I,
)


def _known_provider(value: str) -> str | None:
    folded = fold_text(value)
    if folded in PROVIDER_NAMES and folded != "unknown":
        return folded
    return next((key for key, pattern in PROVIDER_ALIASES if re.search(pattern, folded)), None)


def extract_provider_name(text: str) -> str | None:
    lines = [normalize_space(line) for line in text.splitlines() if normalize_space(line)]
    for index, line in enumerate(lines):
        match = SELLER_LABEL.match(line)
        if match:
            candidate = match.group(1) or (lines[index + 1] if index + 1 < len(lines) else "")
            if candidate and not OTHER_LABEL.match(fold_text(candidate)):
                return candidate

    # A seller header takes precedence over incidental brands elsewhere in a bill.
    for line in lines[:12]:
        folded = fold_text(line)
        if OTHER_LABEL.match(folded):
            continue
        known = _known_provider(line)
        if known:
            return PROVIDER_NAMES[known]
        if COMPANY_SUFFIX.search(line.rstrip(" ,;")) or UTILITY_NAME.search(folded):
            return line
    return None


def canonical_provider(provider_name: str | None = "", text: str | None = "") -> str:
    """Keep existing IDs; give every newly named seller its own stable memory ID."""
    name = normalize_space(provider_name or "")
    if fold_text(name) in MISSING_NAMES:
        name = extract_provider_name(text or "") or ""
    if fold_text(name) in MISSING_NAMES:
        return "unknown"
    known = _known_provider(name)
    if known:
        return known
    # Preserve accents equivalence, punctuation/spacing equivalence, and Unicode names.
    key = re.sub(r"[\W_]+", "_", fold_text(name), flags=re.UNICODE).strip("_")
    return f"provider_{key}" if key else "unknown"


def provider_display_name(provider_id: str) -> str:
    return PROVIDER_NAMES.get(
        provider_id,
        provider_id.removeprefix("provider_").replace("_", " ").title() if provider_id else "Unknown provider",
    )
