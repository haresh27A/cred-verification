"""
scraper.py
----------
Google-first provider search.

Searches Google using provider details and extracts:
- Provider NPI
- Credential
- Provider profile URL
"""

import re
import logging
import requests
import urllib.parse
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeout,
)

logger = logging.getLogger(
    "provider_verification"
)


# ---------------------------------------------------------
# Credential patterns
# ---------------------------------------------------------

CREDENTIAL_PATTERNS = [
    r"\bPA-C\b",
    r"\bFNP-C\b",
    r"\bLCSW\b",
    r"\bLCPC\b",
    r"\bLMFT\b",
    r"\bAPRN\b",
    r"\bCRNA\b",
    r"\bCNP\b",
    r"\bFNP\b",
    r"\bDNP\b",
    r"\bMSW\b",
    r"\bRDN\b",
    r"\bLPC\b",
    r"\bMD\b",
    r"\bDO\b",
    r"\bNP\b",
    r"\bPA\b",
    r"\bRN\b",
    r"\bDDS\b",
    r"\bDMD\b",
    r"\bOD\b",
    r"\bDC\b",
    r"\bPhD\b",
    r"\bRD\b",
]


# ---------------------------------------------------------
# Search Queries
# ---------------------------------------------------------

def build_search_queries(
    provider_name,
    organization="",
    address="",
    city="",
    state="",
    zip_code="",
    phone=""
):
    """
    Build multiple Google search queries.
    """

    queries = []

    provider_name = str(
        provider_name or ""
    ).strip()

    organization = str(
        organization or ""
    ).strip()

    address = str(
        address or ""
    ).strip()

    city = str(
        city or ""
    ).strip()

    state = str(
        state or ""
    ).strip()

    zip_code = str(
        zip_code or ""
    ).strip()

    phone = str(
        phone or ""
    ).strip()

    location = " ".join(
        value
        for value in [
            city,
            state,
            zip_code
        ]
        if value
    )

    if provider_name and organization:

        queries.append(
            f'"{provider_name}" '
            f'"{organization}"'
        )

    if provider_name and address:

        queries.append(
            f'"{provider_name}" '
            f'"{address}"'
        )

    if provider_name and location:

        queries.append(
            f'"{provider_name}" '
            f'"{location}"'
        )

    if provider_name and phone:

        queries.append(
            f'"{provider_name}" '
            f'"{phone}"'
        )

    if provider_name:

        queries.append(
            f'"{provider_name}" '
            f'NPI credential'
        )

    if organization and provider_name:

        queries.append(
            f'"{organization}" '
            f'"{provider_name}" NPI'
        )

    # Remove duplicates
    unique_queries = []

    for query in queries:

        query = query.strip()

        if (
            query
            and query not in unique_queries
        ):

            unique_queries.append(
                query
            )

    return unique_queries


# ---------------------------------------------------------
# Extract NPI
# ---------------------------------------------------------

def extract_npi(text):
    """
    Extract 10-digit NPI from page text.
    """

    if not text:
        return "Not Found"

    labelled_patterns = [
        r"NPI\s*(?:Number|#|No\.?)?"
        r"\s*[:\-]?\s*(\d{10})",

        r"National Provider Identifier"
        r"\s*[:\-]?\s*(\d{10})",
    ]

    for pattern in labelled_patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            return match.group(1)

    # Fallback
    match = re.search(
        r"\b\d{10}\b",
        text
    )

    if match:

        return match.group(0)

    return "Not Found"


# ---------------------------------------------------------
# Extract Credential
# ---------------------------------------------------------

def extract_credential(text):
    """
    Extract professional credential.
    """

    if not text:

        return "Unable to verify"

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    for pattern in CREDENTIAL_PATTERNS:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            return match.group(0)

    return "Unable to verify"


# ---------------------------------------------------------
# Provider Name Check
# ---------------------------------------------------------

def provider_name_found(
    text,
    provider_name
):
    """
    Check whether provider name appears
    on the page.
    """

    if not text or not provider_name:

        return False

    normalized_text = text.lower()

    name_parts = [
        part.lower()
        for part in provider_name.split()
        if len(part) > 2
    ]

    if not name_parts:

        return False

    matches = sum(
        1
        for part in name_parts
        if part in normalized_text
    )

    return matches >= max(
        1,
        len(name_parts) // 2
    )


# ---------------------------------------------------------
# Google Result Extraction
# ---------------------------------------------------------

def extract_google_results(page):
    """
    Extract candidate URLs from Google.
    """

    results = []

    soup = BeautifulSoup(
        page.content(),
        "lxml"
    )

    for anchor in soup.select("a"):

        href = anchor.get(
            "href",
            ""
        )

        title = anchor.get_text(
            " ",
            strip=True
        )

        if not href or not title:
            continue

        if href.startswith(
            "/url?q="
        ):

            href = href.split(
                "/url?q=",
                1
            )[1]

            href = href.split(
                "&",
                1
            )[0]

        if not href.startswith(
            "http"
        ):

            continue

        if "google.com" in href.lower():

            continue

        results.append({
            "title": title,
            "url": href
        })

    return results


# ---------------------------------------------------------
# Inspect Provider Page
# ---------------------------------------------------------

def inspect_provider_page(
    page,
    provider_name
):
    """
    Inspect a candidate page and extract
    provider information.
    """

    html = page.content()

    soup = BeautifulSoup(
        html,
        "lxml"
    )

    text = soup.get_text(
        " ",
        strip=True
    )

    if not provider_name_found(
        text,
        provider_name
    ):

        return None

    npi = extract_npi(
        text
    )

    credential = extract_credential(
        text
    )

    if (
        npi == "Not Found"
        and
        credential == "Unable to verify"
    ):

        return None

    return {
        "status": "Verified",

        "npi": npi,

        "credential": credential,

        "source_url": page.url,

        "remarks": (
            "Information found through "
            "Google search."
        )
    }


# ---------------------------------------------------------
# Main Google Scraper
# ---------------------------------------------------------

def scrape_provider_information(
    provider_name,
    organization="",
    address="",
    city="",
    state="",
    zip_code="",
    phone=""
):
    """
    Search for provider information and official facility profile URL.
    """
    if not provider_name:
        return {
            "status": "Not Verified",
            "npi": "Not Found",
            "credential": "Unable to verify",
            "source_url": "",
            "remarks": "Provider name is missing."
        }

    # 1. Fast HTTP search via DuckDuckGo / Requests
    query = f"{provider_name} {organization} {city} {state}".strip()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    }

    try:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            for a in soup.select('a.result__url'):
                href = a.get('href', '')
                if 'uddg=' in href:
                    href = urllib.parse.unquote(href.split('uddg=')[1].split('&')[0])
                title = a.get_text(strip=True)

                if any(b in href.lower() for b in ['facebook', 'youtube', 'linkedin', 'instagram', 'yelp', 'duckduckgo']):
                    continue

                for cred in ['PA-C', 'FNP-BC', 'FNP-C', 'APRN-CNP', 'APRN', 'APNP', 'APN', 'LCSW', 'MD', 'DO', 'NP', 'PA', 'RN', 'DNP', 'FNP', 'PT', 'MSPT']:
                    if re.search(r'\b' + re.escape(cred) + r'\b', title, re.I) or re.search(r'\b' + re.escape(cred) + r'\b', href, re.I):
                        return {
                            "status": "Verified",
                            "npi": "Not Found",
                            "credential": cred.upper(),
                            "source_url": href,
                            "remarks": "Credential verified via web search."
                        }

                if href.startswith('http'):
                    cred_found = extract_credential(title)
                    return {
                        "status": "Verified" if cred_found != "Unable to verify" else "Not Verified",
                        "npi": "Not Found",
                        "credential": cred_found,
                        "source_url": href if cred_found != "Unable to verify" else "",
                        "remarks": "Provider profile link found."
                    }
    except Exception as e:
        logger.warning(f"Fast HTTP search error: {e}")

    return {
        "status": "Not Verified",
        "npi": "Not Found",
        "credential": "Unable to verify",
        "source_url": "",
        "remarks": "NPI and credential could not be reliably found."
    }


# ---------------------------------------------------------
# Credential Wrapper
# ---------------------------------------------------------

def scrape_provider_credential(
    provider_name,
    organization="",
    address="",
    city="",
    state="",
    zip_code="",
    phone=""
):
    """
    Search specifically for provider credential.
    """

    result = scrape_provider_information(
        provider_name,
        organization,
        address,
        city,
        state,
        zip_code,
        phone
    )

    return {
        "status": result.get(
            "status",
            "Not Verified"
        ),

        "credential": result.get(
            "credential",
            "Unable to verify"
        ),

        "source_url": result.get(
            "source_url",
            ""
        ),

        "remarks": result.get(
            "remarks",
            ""
        )
    }


# ---------------------------------------------------------
# Backward Compatibility
# ---------------------------------------------------------

def scrape_npi_registry_fallback(
    provider_name,
    organization="",
    address="",
    city="",
    state="",
    zip_code="",
    phone=""
):
    """
    Backward-compatible function name.

    Uses Google search instead of direct NPI Registry
    website search.
    """

    return scrape_provider_information(
        provider_name,
        organization,
        address,
        city,
        state,
        zip_code,
        phone
    )