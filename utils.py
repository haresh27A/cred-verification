"""
utils.py
--------
Shared utility functions for Provider Verification.
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
REQUEST_TIMEOUT = 15
REQUEST_DELAY = 0.5
MAX_RESULTS = 20

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0 Safari/537.36"
)

KNOWN_CREDENTIALS = [
    'PA-C', 'FNP-BC', 'FNP-C', 'APRN-CNP', 'APRN', 'APNP', 'APN', 'LCSW', 'LCPC', 'LMFT',
    'CRNA', 'CNP', 'FNP', 'DNP', 'MSW', 'RDN', 'LPC', 'MD', 'DO', 'NP', 'PA', 'RN',
    'DDS', 'DMD', 'OD', 'DC', 'PhD', 'RD', 'PT', 'MSPT', 'OCS', 'CMTPT'
]

ORG_KEYWORDS = ['INC', 'LLC', 'CLINIC', 'HEALTHCARE', 'HEALTH', 'PC', 'ASSOCIATES', 'CENTER', 'GROUP', 'HOSPITAL', 'SYSTEM', 'CARE']


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

        file_handler = logging.FileHandler(
            log_file,
            encoding="utf-8"
        )
        file_handler.setFormatter(formatter)

        logger.addHandler(console_handler)
        logger.addHandler(file_handler)

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


def is_organization_name(name):
    """
    Check whether name refers to an Organization rather than an Individual Provider.
    """
    if not name:
        return False
    words = re.findall(r'\b[A-Z0-9]+\b', str(name).upper())
    for kw in ['INC', 'LLC', 'CLINIC', 'HEALTHCARE', 'PC', 'ASSOCIATES', 'HOSPITAL', 'SYSTEM']:
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
    embedded_cred = ""
    for cred in ['APRN-CNP', 'FNP-BC', 'FNP-C', 'PA-C', 'APNP', 'DNP', 'APN', 'LCSW', 'MD', 'DO', 'NP', 'PA', 'RN', 'FNP']:
        if re.search(r'\b' + re.escape(cred) + r'\b', raw_name, re.I):
            embedded_cred = cred
            break
            
    # Strip Athena tags like ', 8042 (new)' or ', 8042'
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
    search_name = re.sub(r'\b(MD|DO|NP|PA-C|PA|FNP-C|FNP-BC|FNP|DNP|APRN-CNP|APRN|RN|APNP|APN|LCSW|MSW|LPC|DDS|DMD)\b', '', clean_name, flags=re.I)
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
    if not text:
        return ""
    for cred in ['PA-C', 'FNP-BC', 'FNP-C', 'APRN-CNP', 'APRN', 'APNP', 'APN', 'LCSW', 'LCPC', 'LMFT', 'CRNA', 'CNP', 'FNP', 'DNP', 'MD', 'DO', 'NP', 'PA', 'RN', 'DDS', 'DMD', 'OD', 'DC', 'PhD', 'PT', 'MSPT']:
        if re.search(r'\b' + re.escape(cred) + r'\b', text, re.I):
            return cred.upper()
    return ""


# ----------------------------------------
# Helpers
# ----------------------------------------

def similarity_score(text1, text2):
    text1 = normalize_text(text1)
    text2 = normalize_text(text2)
    if not text1 or not text2:
        return 0.0
    return round(difflib.SequenceMatcher(None, text1, text2).ratio(), 3)


def is_valid_npi(npi):
    if npi is None:
        return False
    npi_str = str(npi).strip()
    if npi_str.endswith(".0"):
        npi_str = npi_str[:-2]
    return npi_str.isdigit() and len(npi_str) == 10


def ensure_dir(folder):
    Path(folder).mkdir(parents=True, exist_ok=True)