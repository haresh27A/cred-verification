"""
search.py
---------
Provider and NPI verification logic.
"""

import logging
import requests
import re
from utils import (
    NPI_API_BASE_URL,
    REQUEST_TIMEOUT,
    is_valid_npi,
    is_organization_name,
    clean_provider_name_full,
)
from scraper import scrape_provider_information

logger = logging.getLogger("provider_verification")


# ---------------------------------------------------------
# Validate Existing NPI (NPPES Check)
# ---------------------------------------------------------

def validate_existing_npi(existing_npi, is_provider_row=True):
    """
    Validate existing NPI against official NPPES Registry API.
    Confirms:
    1. Exactly 10 digits.
    2. Exists in NPPES.
    3. Status is Active.
    4. Type match: Provider row must have NPI-1, Organization row must have NPI-2.
    """
    if existing_npi is None:
        return {
            "valid": True,
            "status": "BLANK",
            "npi_type": "",
            "remarks": "NPI is blank."
        }

    npi_str = str(existing_npi).strip()
    if npi_str.endswith(".0"):
        npi_str = npi_str[:-2]

    if not npi_str:
        return {
            "valid": True,
            "status": "BLANK",
            "npi_type": "",
            "remarks": "NPI is blank."
        }

    if not is_valid_npi(npi_str):
        return {
            "valid": False,
            "status": "INVALID_FORMAT",
            "npi_type": "",
            "remarks": f"Invalid NPI '{npi_str}': Must contain exactly 10 digits."
        }

    params = {
        "version": "2.1",
        "number": npi_str
    }

    try:
        response = requests.get(
            NPI_API_BASE_URL,
            params=params,
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.error(f"NPI validation API error: {e}")
        return {
            "valid": False,
            "status": "API_ERROR",
            "npi_type": "",
            "remarks": f"NPPES API Error: {e}"
        }

    results = data.get("results", [])
    if not results:
        return {
            "valid": False,
            "status": "UNLISTED",
            "npi_type": "",
            "remarks": f"NPI {npi_str} not listed in official NPPES Registry."
        }

    result = results[0]
    enumeration_type = result.get("enumeration_type", "") # NPI-1 or NPI-2
    basic = result.get("basic", {})
    status = str(basic.get("status", "Active")).strip()
    is_active = status.upper() in ["A", "ACTIVE"]

    if not is_active:
        return {
            "valid": False,
            "status": "DEACTIVATED",
            "npi_type": enumeration_type,
            "remarks": f"NPI {npi_str} status is deactivated ({status})."
        }

    # Check for Provider vs Organization NPI mismatch
    if is_provider_row and enumeration_type == "NPI-2":
        org_name = basic.get("organization_name", "Organization")
        return {
            "valid": False,
            "status": "ORGANIZATION_NPI",
            "npi_type": enumeration_type,
            "remarks": f"PENDINGCPNAME is a provider name, but NPI column contains an Organization NPI (NPI-2: {org_name})."
        }

    if (not is_provider_row) and enumeration_type == "NPI-1":
        prov_name = f"{basic.get('first_name', '')} {basic.get('last_name', '')}".strip()
        return {
            "valid": False,
            "status": "PROVIDER_NPI",
            "npi_type": enumeration_type,
            "remarks": f"PENDINGCPNAME is an organization name, but NPI column contains a Provider NPI (NPI-1: {prov_name})."
        }

    return {
        "valid": True,
        "status": "Active",
        "npi_type": enumeration_type,
        "remarks": f"Valid active {enumeration_type}."
    }


# ---------------------------------------------------------
# Find Provider NPI (NPI-1 Only)
# ---------------------------------------------------------

def find_provider_npi1(search_name, firstname="", lastname="", state="", city="", address="", phone=""):
    """
    Search NPPES Registry API specifically for Provider NPI (NPI-1).
    """
    parts = search_name.split()
    first_name = firstname or (parts[0] if parts else "")
    last_name = lastname or (parts[-1] if len(parts) > 1 else "")

    if not first_name or not last_name:
        return "Not Found", "", ""

    params = {
        "version": "2.1",
        "limit": 10,
        "enumeration_type": "NPI-1", # Provider NPI only
        "first_name": first_name,
        "last_name": last_name
    }
    if state:
        params["state"] = state

    try:
        r = requests.get(NPI_API_BASE_URL, params=params, timeout=10)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                res = results[0]
                npi = res.get("number", "")
                cred = res.get("basic", {}).get("credential", "")
                url = f"https://npiregistry.cms.hhs.gov/provider-view/{npi}"
                return npi, cred, url
    except Exception as e:
        logger.warning(f"NPI-1 NPPES search error: {e}")

    # Fallback try without state restriction
    if state:
        try:
            params.pop("state", None)
            r = requests.get(NPI_API_BASE_URL, params=params, timeout=10)
            if r.status_code == 200:
                results = r.json().get("results", [])
                if results:
                    res = results[0]
                    npi = res.get("number", "")
                    cred = res.get("basic", {}).get("credential", "")
                    url = f"https://npiregistry.cms.hhs.gov/provider-view/{npi}"
                    return npi, cred, url
        except Exception:
            pass

    return "Not Found", "", ""


# ---------------------------------------------------------
# Main Provider Verification Workflow
# ---------------------------------------------------------

def verify_provider(
    provider_name,
    organization="",
    address="",
    city="",
    state="",
    zip_code="",
    phone="",
    firstname="",
    lastname=""
):
    """
    Full Provider Verification Workflow:
    - Clean Provider Name
    - Find Provider NPI (NPI-1)
    - Verify Credential & URL on Organization/Facility Web Page
    """
    if not provider_name:
        return {
            "status": "Not Verified",
            "npi": "Not Found",
            "credential": "Unable to verify",
            "source_url": "",
            "npi_url": "",
            "remarks": "Provider name is missing."
        }

    search_name, embedded_cred = clean_provider_name_full(provider_name, firstname, lastname)

    # 1. Find Provider NPI (NPI-1)
    found_npi, npi_cred, npi_url = find_provider_npi1(
        search_name,
        firstname=firstname,
        lastname=lastname,
        state=state,
        city=city,
        address=address,
        phone=phone
    )

    # 2. Web search for facility website credential verification
    result = scrape_provider_information(
        provider_name=search_name,
        organization=organization,
        address=address,
        city=city,
        state=state,
        zip_code=zip_code,
        phone=phone
    )

    web_cred = result.get("credential", "")
    web_url = result.get("source_url", "")

    if web_cred == "Unable to verify":
        web_cred = ""

    # Determine final verified credential
    final_credential = web_cred or embedded_cred or npi_cred or "Unable to verify"
    final_url = web_url if (final_credential != "Unable to verify") else ""

    if not final_url and final_credential != "Unable to verify":
        final_url = npi_url

    status = "Verified" if (found_npi != "Not Found" or final_credential != "Unable to verify") else "Not Verified"

    return {
        "status": status,
        "npi": found_npi,
        "credential": final_credential,
        "source_url": final_url,
        "npi_url": npi_url if found_npi != "Not Found" else "",
        "remarks": "Provider verification completed."
    }