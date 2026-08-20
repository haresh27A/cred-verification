"""
search.py
---------
Core provider and NPI verification logic using CMS NPPES NPI Registry API.
Credential and NPI Finder
"""

import logging
import requests
import re
import functools
from urllib.parse import urlencode
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import utils
from utils import (
    NPI_API_BASE_URL,
    REQUEST_TIMEOUT,
    is_valid_npi,
    is_organization_name,
    clean_provider_name_full,
    normalize_text,
    normalize_phone,
    phone_matches,
    normalize_address,
    extract_credential_from_text,
    similarity_score,
)

logger = logging.getLogger("provider_verification")

# ---------------------------------------------------------
# HTTP Session with Connection Reuse & Exponential Backoff
# ---------------------------------------------------------

def create_nppes_session():
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

HTTP_SESSION = create_nppes_session()


@functools.lru_cache(maxsize=1024)
def cached_nppes_get(url_params_tuple):
    """
    LRU Cache wrapper for NPPES API requests.
    url_params_tuple is a sorted tuple of (key, value) pairs.
    """
    params = dict(url_params_tuple)
    try:
        response = HTTP_SESSION.get(
            NPI_API_BASE_URL,
            params=params,
            timeout=REQUEST_TIMEOUT
        )
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        logger.warning(f"NPPES API request failed for params {params}: {e}")
    return {}


# ---------------------------------------------------------
# Validate Existing NPI (NPPES Registry Check)
# ---------------------------------------------------------

def validate_existing_npi(existing_npi, is_provider_row=True, provider_name=""):
    """
    Validate existing NPI against official NPPES Registry API.
    Confirms:
    1. Exactly 10 digits.
    2. Exists in NPPES.
    3. Status is Active.
    4. Entity Type: Type 1 (Individual/Provider) vs Type 2 (Organization).
    5. Compare entity type with expectation (from PENDINGCPNAME / is_provider_row).
    """
    if existing_npi is None:
        return {
            "valid": True,
            "status": "Blank",
            "npi_type": "",
            "entity_type_desc": "",
            "remarks": "NPI is blank.",
            "note": ""
        }

    npi_str = str(existing_npi).strip()
    if npi_str.endswith(".0"):
        npi_str = npi_str[:-2]

    if not npi_str:
        return {
            "valid": True,
            "status": "Blank",
            "npi_type": "",
            "entity_type_desc": "",
            "remarks": "NPI is blank.",
            "note": ""
        }

    # 1. Format check: Exactly 10 numeric digits
    if not is_valid_npi(npi_str):
        note = "Invalid format — NPI must contain exactly 10 digits"
        return {
            "valid": False,
            "status": "Invalid Format",
            "npi_type": "",
            "entity_type_desc": "Invalid",
            "remarks": note,
            "note": note
        }

    params = (("version", "2.1"), ("number", npi_str))
    data = cached_nppes_get(params)

    results = data.get("results", [])
    if not results:
        note = f"NPI {npi_str} not found in NPPES"
        return {
            "valid": False,
            "status": "Not Found",
            "npi_type": "",
            "entity_type_desc": "Unlisted",
            "remarks": note,
            "note": note
        }

    result = results[0]
    enumeration_type = result.get("enumeration_type", "")  # NPI-1 or NPI-2
    entity_desc = "Type 1 — Individual/Provider" if enumeration_type == "NPI-1" else "Type 2 — Organization"
    basic = result.get("basic", {})
    raw_status = str(basic.get("status", "Active")).strip().upper()
    is_active = raw_status in ["A", "ACTIVE"]

    if not is_active:
        note = f"NPI {npi_str} is inactive/deactivated"
        return {
            "valid": False,
            "status": "Inactive",
            "npi_type": enumeration_type,
            "entity_type_desc": entity_desc,
            "remarks": note,
            "note": note
        }

    # 4. Check Provider vs Organization entity mismatch
    if is_provider_row and enumeration_type == "NPI-2":
        org_name = basic.get("organization_name", basic.get("name", "Organization"))
        note = f"Entity mismatch — this is an Organization (Type 2: {org_name}) NPI"
        return {
            "valid": False,
            "status": "Entity Type Mismatch",
            "npi_type": enumeration_type,
            "entity_type_desc": entity_desc,
            "remarks": note,
            "note": note,
            "org_name": org_name
        }

    if (not is_provider_row) and enumeration_type == "NPI-1":
        prov_name = f"{basic.get('first_name', '')} {basic.get('last_name', '')}".strip()
        note = f"Entity mismatch — this is an Individual/Provider (Type 1: {prov_name}) NPI"
        return {
            "valid": False,
            "status": "Entity Type Mismatch",
            "npi_type": enumeration_type,
            "entity_type_desc": entity_desc,
            "remarks": note,
            "note": note,
            "provider_name": prov_name
        }

    return {
        "valid": True,
        "status": "Active",
        "npi_type": enumeration_type,
        "entity_type_desc": entity_desc,
        "remarks": f"Valid active {entity_desc}.",
        "note": "Valid active NPI"
    }


# ---------------------------------------------------------
# Find Missing or Correct Provider NPI (Type 1 Only)
# ---------------------------------------------------------

def find_provider_npi1(
    search_name,
    firstname="",
    lastname="",
    state="",
    city="",
    address="",
    phone="",
    zip_code=""
):
    """
    Search official NPPES Registry API specifically for Individual Provider (Type 1) NPI.
    Evaluates candidate matches and calculates Match Confidence (High, Medium, Low, Manual Review).
    Returns (npi1, credential, url, confidence, status, note, basic_data).
    """
    clean_search, embedded_cred = clean_provider_name_full(search_name, firstname, lastname)

    parts = clean_search.split()
    fname = firstname or (parts[0] if parts else "")
    lname = lastname or (parts[-1] if len(parts) > 1 else "")

    if not fname or not lname or len(lname) < 2:
        return "NPI not found", "", "", "Low", "Not Found", "Insufficient provider name to perform lookup", {}

    results = []

    # Strategy 1: First name + Last name + State (with alias matching enabled)
    query_params_1 = [
        ("version", "2.1"),
        ("limit", "15"),
        ("enumeration_type", "NPI-1"),  # STRICTLY TYPE 1 PROVIDER ONLY
        ("first_name", fname),
        ("last_name", lname),
        ("use_first_name_alias", "True")
    ]
    if state:
        query_params_1.append(("state", state.upper()))

    data = cached_nppes_get(tuple(sorted(query_params_1)))
    results = data.get("results", [])

    # Strategy 2: First name wildcard (e.g. KRISTY*) + Last name
    if not results:
        query_params_2 = [
            ("version", "2.1"),
            ("limit", "15"),
            ("enumeration_type", "NPI-1"),
            ("first_name", f"{fname}*"),
            ("last_name", lname),
            ("use_first_name_alias", "True")
        ]
        if state:
            query_params_2.append(("state", state.upper()))

        data = cached_nppes_get(tuple(sorted(query_params_2)))
        results = data.get("results", [])

    # Strategy 3: Last name + State (or City) if first name didn't match directly in NPPES
    if not results and lname:
        query_params_3 = [
            ("version", "2.1"),
            ("limit", "20"),
            ("enumeration_type", "NPI-1"),
            ("last_name", lname),
            ("use_first_name_alias", "True")
        ]
        if state:
            query_params_3.append(("state", state.upper()))
        if city:
            query_params_3.append(("city", city.upper()))

        data = cached_nppes_get(tuple(sorted(query_params_3)))
        results = data.get("results", [])

    if not results:
        return "NPI not found", "", "", "Low", "Not Found", "NPI not found in NPPES", {}

    # Score candidates based on Name, Address, City, State, ZIP, Phone
    scored_candidates = []
    norm_input_phone = normalize_phone(phone)
    norm_input_address = normalize_address(address)
    norm_input_city = city.upper().strip()
    norm_input_state = state.upper().strip()
    norm_input_zip = zip_code.strip()[:5]

    for res in results:
        npi = res.get("number", "")
        basic = res.get("basic", {})
        res_fname = basic.get("first_name", "")
        res_lname = basic.get("last_name", "")
        res_cred = basic.get("credential", "")
        res_status = basic.get("status", "A")

        # Skip deactivated records unless no active option exists
        if res_status.upper() not in ["A", "ACTIVE"]:
            continue

        # Name similarity score
        res_full = f"{res_fname} {res_lname}"
        name_sim = similarity_score(clean_search, res_full)
        
        # Also check initial / alias match if fname initial matches res_fname initial
        fname_initial_match = (fname and res_fname and fname[0].upper() == res_fname[0].upper())

        addresses = res.get("addresses", [])
        phone_matched = False
        address_matched = False
        city_state_matched = False

        for addr_item in addresses:
            addr_line1 = normalize_address(addr_item.get("address_1", ""))
            addr_line2 = normalize_address(addr_item.get("address_2", ""))
            full_addr_str = f"{addr_line1} {addr_line2}".strip()
            res_city = addr_item.get("city", "").upper().strip()
            res_state = addr_item.get("state", "").upper().strip()
            res_zip = addr_item.get("postal_code", "")[:5]
            res_phone = addr_item.get("telephone_number", "")

            if norm_input_phone and res_phone and phone_matches(norm_input_phone, res_phone):
                phone_matched = True

            if norm_input_address and full_addr_str and (similarity_score(norm_input_address, full_addr_str) > 0.65 or norm_input_address in full_addr_str or full_addr_str in norm_input_address):
                address_matched = True

            if (norm_input_city and res_city == norm_input_city) and (norm_input_state and res_state == norm_input_state):
                city_state_matched = True
            elif norm_input_state and res_state == norm_input_state:
                city_state_matched = True

        # Calculate Confidence Level
        if (name_sim >= 0.80 or fname_initial_match) and (address_matched or (city_state_matched and phone_matched)):
            confidence = "High" if (phone_matched and address_matched) else "High" if address_matched else "Medium"
        elif (name_sim >= 0.70 or fname_initial_match) and (city_state_matched or phone_matched):
            confidence = "Medium"
        elif name_sim >= 0.60 or fname_initial_match:
            confidence = "Low"
        else:
            confidence = "Low"

        score = (name_sim * 40) + (35 if address_matched else 0) + (25 if phone_matched else 0) + (15 if city_state_matched else 0)
        scored_candidates.append({
            "npi": npi,
            "cred": res_cred,
            "basic": basic,
            "confidence": confidence,
            "score": score,
            "name_sim": name_sim,
            "phone_matched": phone_matched,
            "address_matched": address_matched,
            "url": f"https://npiregistry.cms.hhs.gov/provider-view/{npi}"
        })

    if not scored_candidates:
        return "NPI not found", "", "", "Low", "Not Found", "No active matching individual provider found in NPPES", {}

    # Sort candidates by score descending
    scored_candidates.sort(key=lambda x: x["score"], reverse=True)
    top = scored_candidates[0]

    # Check for tie / ambiguity among top candidates
    if len(scored_candidates) > 1:
        second = scored_candidates[1]
        if top["score"] - second["score"] < 10 and top["confidence"] != "High":
            note = "Multiple possible matches — manual review required"
            return note, "", "", "Manual Review", "Multiple Matches", note, {}

    if top["confidence"] == "Low":
        note = "Low confidence match — manual review required"
        return "Multiple possible matches — manual review required", "", "", "Manual Review", "Manual Review Required", note, {}

    # Confident match
    npi1 = top["npi"]
    npi1_cred = top["cred"]
    url = top["url"]
    conf = top["confidence"]
    note = f"Provider NPI found ({conf} confidence match)"

    return npi1, npi1_cred, url, conf, "Active", note, top["basic"]


# ---------------------------------------------------------
# Credential Verification
# ---------------------------------------------------------

def verify_provider_credential(basic_data, input_cred="", raw_name=""):
    """
    Retrieve and verify credential associated with provider.
    Cross-references NPPES basic.credential, raw provider name, and input credential column.
    """
    nppes_cred = basic_data.get("credential", "") if isinstance(basic_data, dict) else ""
    nppes_cred_token = extract_credential_from_text(nppes_cred)
    input_cred_token = extract_credential_from_text(input_cred)
    name_cred_token = extract_credential_from_text(raw_name)

    candidates = [c for c in [input_cred_token, nppes_cred_token, name_cred_token] if c]

    if not candidates:
        return "Credential not confirmed", False, "Credential not confirmed in NPPES or input"

    distinct_creds = set(candidates)
    if len(distinct_creds) > 1:
        incompatible_pairs = [("MD", "NP"), ("MD", "RN"), ("MD", "PA"), ("DO", "NP"), ("DO", "RN"), ("DO", "PA")]
        for c1 in candidates:
            for c2 in candidates:
                if (c1, c2) in incompatible_pairs or (c2, c1) in incompatible_pairs:
                    return f"Conflict: {c1} vs {c2}", False, f"Conflicting credentials found ({c1} vs {c2}) — manual review required"

    final_cred = candidates[0]
    return final_cred, True, f"Credential {final_cred} confirmed"


# ---------------------------------------------------------
# Single Provider Search Workflow
# ---------------------------------------------------------

def verify_single_provider(data):
    """
    Full workflow for the Single Provider tab in the Web App.
    Receives search parameters, validates NPI (if provided), and queries NPPES API.
    """
    provider_name = data.get("provider_name", "").strip()
    organization = data.get("organization", "").strip()
    existing_npi = data.get("npi", "").strip()
    address = data.get("address", "").strip()
    city = data.get("city", "").strip()
    state = data.get("state", "").strip()
    phone = data.get("phone", "").strip()

    is_org_search = is_organization_name(organization or provider_name)

    # 1. Existing NPI Validation if NPI provided
    npi_validation = None
    if existing_npi:
        npi_validation = validate_existing_npi(
            existing_npi,
            is_provider_row=not is_org_search,
            provider_name=provider_name
        )

    # 2. Search for Provider NPI
    npi1, npi1_cred, npi1_url, conf, status, note, basic_data = find_provider_npi1(
        search_name=provider_name or organization,
        state=state,
        city=city,
        address=address,
        phone=phone
    )

    # 3. Verify Credential
    cred, cred_confirmed, cred_note = verify_provider_credential(
        basic_data=basic_data,
        input_cred="",
        raw_name=provider_name
    )

    npi_to_display = existing_npi if (npi_validation and npi_validation.get("valid")) else (npi1 if npi1 != "NPI not found" and not npi1.startswith("Multiple") else "")

    entity_type_desc = "Type 1 — Individual/Provider" if not is_org_search else "Type 2 — Organization"
    if npi_validation and npi_validation.get("entity_type_desc"):
        entity_type_desc = npi_validation["entity_type_desc"]

    return {
        "provider_name": provider_name or organization or "N/A",
        "organization": organization,
        "npi": npi_to_display or "Not Found",
        "npi1": npi1,
        "npi_status": npi_validation.get("status") if npi_validation else status,
        "entity_type": entity_type_desc,
        "credential": cred if cred_confirmed else "Credential not confirmed",
        "address": address or "N/A",
        "city": city or "N/A",
        "state": state or "N/A",
        "phone": phone or "N/A",
        "match_confidence": conf if npi1 != "NPI not found" else "N/A",
        "npi_validation": npi_validation,
        "validation_notes": npi_validation.get("note") if (npi_validation and not npi_validation.get("valid")) else note,
        "npi_url": npi1_url
    }