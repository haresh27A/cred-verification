"""
utils.py
--------
Shared utility functions for Provider Verification & Normalization.
Credential and NPI Finder
"""

import logging
import re
import socket
import difflib
from pathlib import Path
import requests

# ----------------------------------------
# DNS Fallback for NPPES API
# ----------------------------------------

orig_getaddrinfo = socket.getaddrinfo

def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return orig_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror:
        if host == 'npiregistry.cms.hhs.gov':
            try:
                r = requests.get('https://dns.google/resolve?name=npiregistry.cms.hhs.gov', timeout=5).json()
                ips = [ans['data'] for ans in r.get('Answer', []) if ans.get('type') == 1]
                if ips:
                    return orig_getaddrinfo(ips[0], port, family, type, proto, flags)
            except Exception:
                pass
        raise

socket.getaddrinfo = patched_getaddrinfo


# ----------------------------------------
# Constants
# ----------------------------------------

NPI_API_BASE_URL = "https://npiregistry.cms.hhs.gov/api/"
NPI_REGISTRY_SEARCH_URL = "https://npiregistry.cms.hhs.gov/search"

MATCH_THRESHOLD = 0.60
REQUEST_TIMEOUT = 10
MAX_RESULTS = 20

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0 Safari/537.36"
)

KNOWN_CREDENTIALS = [
    'PA-C', 'FNP-BC', 'FNP-C', 'APRN-CNP', 'APRN', 'APNP', 'APN', 'LCSW', 'LCPC', 'LMFT',
    'CRNA', 'CNP', 'FNP', 'DNP', 'MSW', 'RDN', 'LPC', 'MD', 'DO', 'NP', 'PA', 'RN',
    'DDS', 'DMD', 'OD', 'DC', 'PhD', 'RD', 'PT', 'MSPT', 'OCS', 'CMTPT', 'BCBA', 'AUD', 'CRNP'
]

ORG_KEYWORDS = [
    'INC', 'LLC', 'CLINIC', 'HEALTHCARE', 'HEALTH', 'PC', 'ASSOCIATES', 'CENTER', 'GROUP',
    'HOSPITAL', 'SYSTEM', 'CARE', 'MEDICAL', 'SERVICES', 'PRACTICE', 'USA', 'CORP',
    'CORPORATION', 'PARTNERS', 'FOUNDATION', 'SPECIALISTS', 'FACILITY', 'COMMUNITY', 'PHYSICIANS',
    'THERAPY', 'MEDICINE', 'URGENT', 'DIAGNOSTICS', 'LABORATORY', 'LABS', 'SOLUTIONS'
]


# ----------------------------------------
# Logger
# ----------------------------------------

def setup_logger(name="provider_verification", log_file="verification.log"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s"
        )

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        try:
            file_handler = logging.FileHandler(
                log_file,
                encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except (PermissionError, OSError):
            pass

    return logger


# ----------------------------------------
# Text Normalization & Provider Cleaning
# ----------------------------------------

def normalize_text(text):
    if text is None:
        return ""
    text = str(text).strip().lower()
    if text in ("", "nan", "none", "null"):
        return ""
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_phone(phone):
    """
    Remove punctuation and spaces from phone number, returning clean digits only.
    """
    if not phone:
        return ""
    digits = re.sub(r"\D", "", str(phone))
    return digits


def phone_matches(phone1, phone2):
    """
    Compare two phone numbers using normalized digits.
    Returns True if last 10 digits match or last 7 digits match.
    """
    p1 = normalize_phone(phone1)
    p2 = normalize_phone(phone2)
    if not p1 or not p2:
        return False
    if p1 == p2:
        return True
    if len(p1) >= 10 and len(p2) >= 10:
        return p1[-10:] == p2[-10:]
    if len(p1) >= 7 and len(p2) >= 7:
        return p1[-7:] == p2[-7:]
    return False


ADDRESS_ABBR = {
    r'\bST\b': 'STREET',
    r'\bAVE\b': 'AVENUE',
    r'\bDR\b': 'DRIVE',
    r'\bRD\b': 'ROAD',
    r'\bBLVD\b': 'BOULEVARD',
    r'\bPKWY\b': 'PARKWAY',
    r'\bSTE\b': 'SUITE',
    r'\bSUITE\b': 'SUITE',
    r'\bCT\b': 'COURT',
    r'\bLN\b': 'LANE',
    r'\bHWY\b': 'HIGHWAY',
    r'\bCIR\b': 'CIRCLE',
    r'\bBLDG\b': 'BUILDING',
    r'\bFL\b': 'FLOOR',
    r'\bPL\b': 'PLACE',
    r'\bTER\b': 'TERRACE',
    r'\bN\b': 'NORTH',
    r'\bS\b': 'SOUTH',
    r'\bE\b': 'EAST',
    r'\bW\b': 'WEST',
}

def normalize_address(address):
    """
    Standardize address capitalization, whitespace, and common abbreviations.
    """
    if not address:
        return ""
    addr = str(address).upper().strip()
    addr = re.sub(r"[^\w\s#\-]", " ", addr)
    for pattern, replacement in ADDRESS_ABBR.items():
        addr = re.sub(pattern, replacement, addr)
    addr = re.sub(r"\s+", " ", addr).strip()
    return addr


def is_organization_name(name):
    """
    Check whether name refers to an Organization rather than an Individual Provider.
    """
    if not name:
        return False
    upper_name = str(name).upper()
    words = re.findall(r'\b[A-Z0-9]+\b', upper_name)
    for kw in ORG_KEYWORDS:
        if kw in words:
            return True
    return False


def clean_provider_name_full(raw_name, firstname="", lastname=""):
    """
    Clean provider name by removing database noise (e.g. ', 8042 (new)'),
    reordering 'LASTNAME, FIRSTNAME', and detecting embedded credentials.
    """
    raw_name = str(raw_name or '').strip()
    firstname = str(firstname or '').strip()
    lastname = str(lastname or '').strip()

    # Extract embedded credential from raw name if present
    embedded_cred = extract_credential_from_text(raw_name)

    # Strip Athena/EMR tags like ', 8042 (new)' or ', 8042'
    cleaned = re.sub(r',\s*\d+\s*\([^\)]+\)', '', raw_name).strip()
    cleaned = re.sub(r',\s*\d+$', '', cleaned).strip()

    # Check if firstname and lastname exist in separate columns
    if firstname and lastname and len(firstname) > 1 and len(lastname) > 1:
        clean_name = f"{firstname} {lastname}"
    else:
        if ',' in cleaned:
            parts = [p.strip() for p in cleaned.split(',')]
            name_parts = [p for p in parts if p.upper() not in [c.upper() for c in KNOWN_CREDENTIALS]]
            if len(name_parts) >= 2:
                clean_name = f"{name_parts[1]} {name_parts[0]}"
            elif len(name_parts) == 1:
                clean_name = name_parts[0]
            else:
                clean_name = cleaned
        else:
            clean_name = cleaned

    # Strip credential tokens from search name
    pattern = r'\b(' + '|'.join([re.escape(c) for c in KNOWN_CREDENTIALS]) + r')\b'
    search_name = re.sub(pattern, '', clean_name, flags=re.I)
    search_name = re.sub(r'\s+', ' ', search_name).strip(' ,')

    return search_name, embedded_cred


def split_provider_name(full_name):
    full_name = normalize_text(full_name)
    if not full_name:
        return "", ""
    if "," in full_name:
        parts = [part.strip() for part in full_name.split(",")]
        return parts[1] if len(parts) > 1 else "", parts[0]
    words = full_name.split()
    if len(words) == 1:
        return words[0], ""
    return words[0], words[-1]


def extract_credential_from_text(text):
    """
    Extract provider credential from string, returning standardized uppercase token.
    """
    if not text:
        return ""
    # Sort known credentials by length descending to match longer tokens first (e.g. APRN-CNP before APRN)
    sorted_creds = sorted(KNOWN_CREDENTIALS, key=len, reverse=True)
    for cred in sorted_creds:
        # Match with boundary, allowing optional periods like M.D. or D.O.
        cred_pattern = r'\b' + r'\.?'.join(list(cred.replace('-', ''))) + r'\.?\b'
        if re.search(cred_pattern, text, re.I):
            return cred.upper()
    return ""


def similarity_score(text1, text2):
    text1 = normalize_text(text1)
    text2 = normalize_text(text2)
    if not text1 or not text2:
        return 0.0
    return round(difflib.SequenceMatcher(None, text1, text2).ratio(), 3)


def is_valid_npi(npi):
    """
    Validate that NPI contains exactly 10 numeric digits.
    """
    if npi is None:
        return False
    npi_str = str(npi).strip()
    if npi_str.endswith(".0"):
        npi_str = npi_str[:-2]
    return npi_str.isdigit() and len(npi_str) == 10


def ensure_dir(folder):
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        pass