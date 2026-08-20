"""
main.py
-------
Main processing engine for Provider Credential & NPI Verification.
Supports parallel processing, streaming events, thread-safe dataframe updates, and summary statistics.
Credential and NPI Finder
"""

import time
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

from utils import (
    setup_logger,
    is_organization_name,
    clean_provider_name_full,
)
from excel_handler import (
    read_input_file,
    prepare_output_dataframe,
    save_output_file,
)
from search import (
    validate_existing_npi,
    find_provider_npi1,
    verify_provider_credential,
)


INPUT_FILE = "input/sample_input.csv"
OUTPUT_FILE = "output/provider_verification_output.xlsx"


def clean_val(val):
    if val is None:
        return ""
    v = str(val).strip()
    if v.lower() in ("", "nan", "none", "null"):
        return ""
    return v


def get_column_value(row, *column_names):
    for col in column_names:
        if col in row.index:
            v = clean_val(row.get(col, ""))
            if v:
                return v
    return ""


def process_single_row(index, row_dict, df_columns):
    """
    Process a single provider row in a thread-safe manner.
    Takes a dict copy of the row and returns a result dict.
    """
    row_series = pd.Series(row_dict)
    raw_provider_name = get_column_value(row_series, "PENDINGCPNAME", "PROVIDER_NAME", "NAME")
    firstname = get_column_value(row_series, "FIRSTNAME")
    lastname = get_column_value(row_series, "LASTNAME")
    organization = get_column_value(row_series, "CONTEXT_NAME", "ORGANIZATION", "NAME")
    address = get_column_value(row_series, "ADDRESS")
    city = get_column_value(row_series, "CITY")
    state = get_column_value(row_series, "STATE")
    zip_code = get_column_value(row_series, "ZIP", "ZIPCODE")
    phone = get_column_value(row_series, "PHONE")

    existing_npi = get_column_value(row_series, "NPI")
    existing_credential = get_column_value(row_series, "Credental", "Credential")

    # Determine if row represents Organization or Individual Provider
    is_org_row = is_organization_name(raw_provider_name or organization)
    search_name, embedded_cred = clean_provider_name_full(raw_provider_name, firstname, lastname)

    npi_validation = None
    is_invalid_npi = False
    is_mismatch = False
    validation_note = ""

    # 1. Validate Existing NPI
    if existing_npi:
        npi_validation = validate_existing_npi(
            existing_npi,
            is_provider_row=not is_org_row,
            provider_name=raw_provider_name
        )

        if not npi_validation.get("valid", False):
            val_status = npi_validation.get("status", "")
            validation_note = npi_validation.get("note", "")
            if val_status == "Entity Type Mismatch":
                is_mismatch = True
            else:
                is_invalid_npi = True
        else:
            validation_note = "Valid active NPI"

    # 2. Find Missing or Correct Type 1 Provider NPI (NPI 1)
    found_npi1, npi_cred, npi1_url, confidence, npi_status, find_note, basic_data = find_provider_npi1(
        search_name=search_name,
        firstname=firstname,
        lastname=lastname,
        state=state,
        city=city,
        address=address,
        phone=phone,
        zip_code=zip_code
    )

    # If existing NPI is already a valid Type 1 NPI, ensure NPI 1 uses existing NPI
    if existing_npi and npi_validation and npi_validation.get("valid") and npi_validation.get("npi_type") == "NPI-1":
        found_npi1 = existing_npi
        npi1_url = f"https://npiregistry.cms.hhs.gov/provider-view/{existing_npi}"
        confidence = "High"
        find_note = "Valid active NPI confirmed"

    # If row is an Organization (Type 2), do NOT put an Organization NPI in NPI 1
    if is_org_row:
        found_npi1 = "NPI not found"
        confidence = "N/A"
        find_note = "Organization row — NPI 1 is reserved for Individual Providers (Type 1)"

    # 3. Credential Verification
    final_cred, cred_confirmed, cred_note = verify_provider_credential(
        basic_data=basic_data,
        input_cred=existing_credential,
        raw_name=raw_provider_name
    )

    if not final_cred or final_cred == "Credential not confirmed":
        final_cred = embedded_cred or npi_cred or "Credential not confirmed"

    # Compile validation notes
    notes = []
    if validation_note and not validation_note.startswith("Valid active"):
        notes.append(validation_note)
    if find_note and find_note not in notes and not find_note.startswith("Valid active") and not find_note.startswith("Provider NPI found"):
        notes.append(find_note)
    if cred_note and "manual review" in cred_note.lower():
        notes.append(cred_note)

    combined_notes = " | ".join(dict.fromkeys(n for n in notes if n)) or "Verified successfully"

    # Populate result dictionary for ALL column synonyms
    result_updates = {
        "NPI 1": found_npi1,
        "NPI1": found_npi1,
        "Credential": final_cred,
        "Credental": final_cred,
        "NPI Status": npi_validation.get("status") if (existing_npi and npi_validation) else npi_status,
        "NPI Entity Type": npi_validation.get("entity_type_desc") if (existing_npi and npi_validation and npi_validation.get("entity_type_desc")) else ("Type 1 — Individual/Provider" if not is_org_row else "Type 2 — Organization"),
        "NPI Validation": npi_validation.get("status") if (existing_npi and npi_validation) else ("Valid" if found_npi1 not in ["NPI not found", "Multiple possible matches — manual review required"] else "Not Found"),
        "Match Confidence": confidence,
        "Validation Notes": combined_notes,
        "Url": npi1_url if found_npi1 not in ["NPI not found", "Multiple possible matches — manual review required"] else "",
        "NPI 1 Url": npi1_url if found_npi1 not in ["NPI not found", "Multiple possible matches — manual review required"] else "",
        "NPI1 Url": npi1_url if found_npi1 not in ["NPI not found", "Multiple possible matches — manual review required"] else ""
    }

    if "COMMENTS" in df_columns:
        result_updates["COMMENTS"] = combined_notes
    if "STATUS" in df_columns:
        result_updates["STATUS"] = "Verified" if (found_npi1 not in ["NPI not found", "Multiple possible matches — manual review required"] or cred_confirmed) else "Requires Review"

    return {
        "index": index,
        "result_updates": result_updates,
        "is_invalid_npi": is_invalid_npi,
        "is_mismatch": is_mismatch,
        "validation_note": validation_note or combined_notes,
        "existing_npi": existing_npi,
        "found_npi1": found_npi1,
        "cred_confirmed": cred_confirmed,
        "confidence": confidence,
        "provider_name": raw_provider_name or search_name
    }


def process_dataframe_stream(df, logger=None):
    """
    Generator function processing DataFrame rows concurrently and updating DataFrame thread-safely.
    Yields progress dicts for web streaming / CLI UI.
    """
    if logger is None:
        logger = setup_logger()

    start_time = time.time()
    df = prepare_output_dataframe(df)
    total_rows = len(df)

    invalid_npi_rows = []
    mismatch_rows = []
    npi_notes_map = {}

    stats = {
        "total_records": total_rows,
        "valid_active_npis": 0,
        "invalid_format_npis": 0,
        "npis_not_found": 0,
        "inactive_npis": 0,
        "entity_mismatches": 0,
        "npis_found": 0,
        "credentials_confirmed": 0,
        "manual_review_required": 0,
        "processing_time_seconds": 0.0
    }

    # Store all row updates safely in memory indexed by row index
    all_row_results = {}
    completed_count = 0
    max_workers = min(12, max(2, total_rows))

    # Convert dataframe rows to list of dicts for safe thread execution
    row_dicts = [row.to_dict() for _, row in df.iterrows()]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(process_single_row, idx, r_dict, df.columns): idx
            for idx, r_dict in enumerate(row_dicts)
        }

        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            try:
                res = future.result()
                all_row_results[idx] = res

                # Tracking invalid & mismatched NPI rows
                if res["is_invalid_npi"]:
                    invalid_npi_rows.append(idx)
                    npi_notes_map[idx] = res["validation_note"]
                if res["is_mismatch"]:
                    mismatch_rows.append(idx)
                    npi_notes_map[idx] = res["validation_note"]

                # Update Stats
                res_updates = res["result_updates"]
                val_status = res_updates.get("NPI Validation", "")
                if val_status == "Active" or val_status == "Valid":
                    stats["valid_active_npis"] += 1
                elif val_status == "Invalid Format":
                    stats["invalid_format_npis"] += 1
                elif val_status == "Not Found":
                    stats["npis_not_found"] += 1
                elif val_status == "Inactive":
                    stats["inactive_npis"] += 1
                elif val_status == "Entity Type Mismatch":
                    stats["entity_mismatches"] += 1

                npi1_val = res_updates.get("NPI 1", "")
                if npi1_val and npi1_val not in ["NPI not found", "Multiple possible matches — manual review required"]:
                    stats["npis_found"] += 1

                if res["cred_confirmed"]:
                    stats["credentials_confirmed"] += 1

                conf = res_updates.get("Match Confidence", "")
                if conf in ["Manual Review", "Low"] or npi1_val.startswith("Multiple") or res["is_mismatch"]:
                    stats["manual_review_required"] += 1

                completed_count += 1

                row_summary = {
                    "row_index": int(idx),
                    "provider_name": res["provider_name"],
                    "npi": str(row_dicts[idx].get("NPI", "")),
                    "npi1": str(res_updates.get("NPI 1", "")),
                    "credential": str(res_updates.get("Credential", "")),
                    "npi_status": str(res_updates.get("NPI Status", "")),
                    "entity_type": str(res_updates.get("NPI Entity Type", "")),
                    "match_confidence": str(res_updates.get("Match Confidence", "")),
                    "notes": str(res_updates.get("Validation Notes", "")),
                    "is_invalid_npi": idx in invalid_npi_rows,
                    "is_mismatch": idx in mismatch_rows
                }

                yield {
                    "type": "progress",
                    "current": completed_count,
                    "total": total_rows,
                    "percent": round((completed_count / total_rows) * 100, 1),
                    "row_summary": row_summary,
                    "invalid_npi_count": len(invalid_npi_rows),
                    "mismatch_count": len(mismatch_rows),
                    "stats": stats
                }

            except Exception as e:
                logger.error(f"Error processing row {idx}: {e}")
                completed_count += 1

    # Thread-safe deterministic bulk update of DataFrame in main thread
    for idx, res in all_row_results.items():
        res_updates = res["result_updates"]
        for col, val in res_updates.items():
            if col in df.columns:
                df.at[idx, col] = val

    stats["processing_time_seconds"] = round(time.time() - start_time, 2)

    yield {
        "type": "complete",
        "total": total_rows,
        "df": df,
        "invalid_npi_rows": invalid_npi_rows,
        "mismatch_rows": mismatch_rows,
        "npi_notes_map": npi_notes_map,
        "stats": stats
    }


def main():
    logger = setup_logger()
    logger.info("Reading input file...")

    df = read_input_file(INPUT_FILE)
    logger.info(f"Total Providers / Rows: {len(df)}")

    final_df = None
    invalid_npi_rows = []
    mismatch_rows = []
    npi_notes_map = {}
    stats = {}

    pbar = tqdm(total=len(df), desc="Verifying Providers")
    for event in process_dataframe_stream(df, logger=logger):
        if event["type"] == "progress":
            pbar.update(1)
        elif event["type"] == "complete":
            final_df = event["df"]
            invalid_npi_rows = event["invalid_npi_rows"]
            mismatch_rows = event["mismatch_rows"]
            npi_notes_map = event["npi_notes_map"]
            stats = event["stats"]

    pbar.close()

    if final_df is not None:
        save_output_file(
            final_df,
            OUTPUT_FILE,
            invalid_npi_rows=invalid_npi_rows,
            mismatch_rows=mismatch_rows,
            npi_notes_map=npi_notes_map
        )
        logger.info(f"Provider verification completed in {stats.get('processing_time_seconds', 0)}s.")


if __name__ == "__main__":
    main()