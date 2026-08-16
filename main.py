"""
main.py
-------
Main entry point for Provider Credential & NPI Verification.
"""

import pandas as pd
from tqdm import tqdm
from utils import (
    setup_logger,
    is_organization_name,
    clean_provider_name_full,
    extract_credential_from_text,
)
from excel_handler import (
    read_input_file,
    prepare_output_dataframe,
    save_output_file,
)
from search import (
    validate_existing_npi,
    find_provider_npi1,
)
from scraper import scrape_provider_information


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


def process_dataframe_stream(df, logger=None):
    """
    Generator function that verifies providers row by row,
    yielding progress dicts for web streaming / CLI UI.
    """
    if logger is None:
        logger = setup_logger()

    df = prepare_output_dataframe(df)
    total_rows = len(df)
    invalid_npi_rows = []
    mismatch_rows = []

    for index, row in df.iterrows():
        raw_provider_name = get_column_value(row, "PENDINGCPNAME", "PROVIDER_NAME", "NAME")
        firstname = get_column_value(row, "FIRSTNAME")
        lastname = get_column_value(row, "LASTNAME")
        organization = get_column_value(row, "CONTEXT_NAME", "NAME", "ORGANIZATION")
        address = get_column_value(row, "ADDRESS")
        city = get_column_value(row, "CITY")
        state = get_column_value(row, "STATE")
        zip_code = get_column_value(row, "ZIP", "ZIPCODE")
        phone = get_column_value(row, "PHONE")

        existing_npi = get_column_value(row, "NPI")
        existing_credential = get_column_value(row, "Credental", "Credential")

        is_org_row = is_organization_name(raw_provider_name)
        search_name, embedded_cred = clean_provider_name_full(raw_provider_name, firstname, lastname)

        # -------------------------------------------------
        # 1. Validate Existing NPI (NPPES Registry Check)
        # -------------------------------------------------
        npi_validation = None
        if existing_npi:
            try:
                npi_validation = validate_existing_npi(
                    existing_npi,
                    is_provider_row=not is_org_row
                )

                if not npi_validation.get("valid", False):
                    status_code = npi_validation.get("status", "")
                    if "MISMATCH" in status_code or status_code in ["ORGANIZATION_NPI", "PROVIDER_NPI"]:
                        mismatch_rows.append(index)
                    else:
                        invalid_npi_rows.append(index)

            except Exception as e:
                logger.error(f"NPI validation error for row {index}: {e}")
                invalid_npi_rows.append(index)

        # -------------------------------------------------
        # 2. Find Provider NPI (NPI 1 Column)
        # -------------------------------------------------
        found_npi1, npi_cred, npi1_url = find_provider_npi1(
            search_name,
            firstname=firstname,
            lastname=lastname,
            state=state,
            city=city,
            address=address,
            phone=phone
        )

        # If existing NPI is already a valid provider NPI-1, ensure NPI1 is set
        if (found_npi1 == "Not Found" or not found_npi1) and existing_npi and (npi_validation and npi_validation.get("npi_type") == "NPI-1"):
            found_npi1 = existing_npi
            npi1_url = f"https://npiregistry.cms.hhs.gov/provider-view/{existing_npi}"

        df.at[index, "NPI1"] = found_npi1 if found_npi1 else "Not Found"
        df.at[index, "NPI 1 Url"] = npi1_url if (found_npi1 != "Not Found" and found_npi1) else ""

        # -------------------------------------------------
        # 3. Credential & Profile URL Verification
        # -------------------------------------------------
        if existing_credential:
            final_credential = existing_credential
            final_url = get_column_value(row, "Url")
        else:
            web_result = scrape_provider_information(
                provider_name=search_name,
                organization=organization,
                address=address,
                city=city,
                state=state,
                zip_code=zip_code,
                phone=phone
            )
            web_cred = web_result.get("credential", "")
            web_url = web_result.get("source_url", "")

            if web_cred == "Unable to verify":
                web_cred = ""

            final_credential = web_cred or embedded_cred or npi_cred
            if not final_credential:
                final_credential = "Unable to verify"
                final_url = ""
            else:
                final_url = web_url or npi1_url

            col_cred = "Credental" if "Credental" in df.columns else "Credential"
            df.at[index, col_cred] = final_credential
            df.at[index, "Url"] = final_url if final_credential != "Unable to verify" else ""

        # -------------------------------------------------
        # 4. Remarks & Status
        # -------------------------------------------------
        remarks = []
        if npi_validation and not npi_validation.get("valid", True):
            remarks.append(npi_validation.get("remarks", ""))

        if df.at[index, "NPI1"] != "Not Found":
            remarks.append(f"Provider NPI1: {df.at[index, 'NPI1']}")

        col_cred = "Credental" if "Credental" in df.columns else "Credential"
        cur_cred = df.at[index, col_cred]
        if cur_cred == "Unable to verify":
            remarks.append("Credential could not be reliably verified.")

        if "COMMENTS" in df.columns:
            df.at[index, "COMMENTS"] = " | ".join(dict.fromkeys(r for r in remarks if r))
        if "STATUS" in df.columns:
            df.at[index, "STATUS"] = "Verified" if (df.at[index, "NPI1"] != "Not Found" or cur_cred != "Unable to verify") else "Not Verified"

        row_status = str(df.at[index, "STATUS"]) if "STATUS" in df.columns else "Verified"
        row_dict = df.loc[index].to_dict()
        row_summary = {
            "row_index": int(index),
            "provider_name": raw_provider_name or search_name,
            "npi": existing_npi,
            "npi1": df.at[index, "NPI1"],
            "credential": cur_cred,
            "status": row_status,
            "is_invalid_npi": index in invalid_npi_rows,
            "is_mismatch_npi": index in mismatch_rows,
            "url": df.at[index, "Url"],
            "npi1_url": df.at[index, "NPI 1 Url"],
            "row_data": {k: str(v) for k, v in row_dict.items()}
        }



        yield {
            "type": "progress",
            "current": int(index) + 1,
            "total": total_rows,
            "percent": round(((index + 1) / total_rows) * 100, 1),
            "row_summary": row_summary,
            "invalid_npi_count": len(invalid_npi_rows),
            "mismatch_npi_count": len(mismatch_rows)
        }

    yield {
        "type": "complete",
        "total": total_rows,
        "df": df,
        "invalid_npi_rows": invalid_npi_rows,
        "mismatch_rows": mismatch_rows
    }


def main():
    logger = setup_logger()
    logger.info("Reading input file...")

    df = read_input_file(INPUT_FILE)
    logger.info(f"Total Providers / Rows: {len(df)}")

    final_df = None
    invalid_npi_rows = []
    mismatch_rows = []

    pbar = tqdm(total=len(df), desc="Verifying Providers")
    for event in process_dataframe_stream(df, logger=logger):
        if event["type"] == "progress":
            pbar.update(1)
        elif event["type"] == "complete":
            final_df = event["df"]
            invalid_npi_rows = event["invalid_npi_rows"]
            mismatch_rows = event["mismatch_rows"]

    pbar.close()

    if final_df is not None:
        save_output_file(
            final_df,
            OUTPUT_FILE,
            invalid_npi_rows=invalid_npi_rows,
            mismatch_rows=mismatch_rows
        )
        logger.info("Provider verification completed successfully.")


if __name__ == "__main__":
    main()