"""
app.py
------
Flask Web Server for Credential and NPI Finder.
Supports bulk file processing (.csv, .xlsx, .xls), real-time SSE streaming,
single provider lookups, and styled Excel exports with cell comments and highlights.
"""

import os
import io
import json
import uuid
import time
import logging
from flask import Flask, render_template, request, jsonify, Response, send_file, send_from_directory
import pandas as pd

from utils import setup_logger
from excel_handler import read_input_file, generate_excel_bytes
from main import process_dataframe_stream
from search import validate_existing_npi, verify_single_provider

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
app.url_map.strict_slashes = False
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB max upload limit

logger = setup_logger("credential_npi_finder", "web_app.log")


@app.route("/static/<path:filename>")
def serve_static_assets(filename):
    return send_from_directory(STATIC_DIR, filename)


# In-memory session store
# session_id -> { "df_input": df, "df_output": df, "filename": str, "invalid_npi_rows": [], "mismatch_rows": [], "npi_notes_map": {}, "stats": {}, "status": str }
SESSIONS = {}


@app.route("/")
@app.route("/index")
def index():
    """Render main application UI."""
    return render_template("index.html")


@app.errorhandler(404)
def page_not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": f"API endpoint not found: {request.path}"}), 404
    return render_template("index.html"), 200


@app.route("/api/upload", methods=["POST"])
def upload_file():
    """Handle XLSX, XLS, and CSV file uploads."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    filename = file.filename
    session_id = str(uuid.uuid4())

    try:
        df = read_input_file(file.stream, filename=filename)
        if df.empty:
            return jsonify({"error": "The uploaded file is empty."}), 400

        # Replace NaN/null with empty strings for clean JSON serialization
        df = df.fillna("")

        SESSIONS[session_id] = {
            "session_id": session_id,
            "filename": filename,
            "df_input": df,
            "df_output": None,
            "invalid_npi_rows": [],
            "mismatch_rows": [],
            "npi_notes_map": {},
            "stats": {},
            "status": "ready",
            "created_at": time.time()
        }

        # Priority preview columns
        all_cols = list(df.columns)
        priority_cols = ["PENDINGCPNAME", "PROVIDER_NAME", "NAME", "NPI", "FIRSTNAME", "LASTNAME", "ADDRESS", "CITY", "STATE", "PHONE"]
        front_cols = [c for c in priority_cols if c in all_cols]
        other_cols = [c for c in all_cols if c not in front_cols]
        ordered_cols = front_cols + other_cols

        preview_rows = df[ordered_cols].head(5).to_dict(orient="records")

        return jsonify({
            "success": True,
            "session_id": session_id,
            "filename": filename,
            "row_count": len(df),
            "columns": ordered_cols,
            "preview": preview_rows
        })

    except Exception as e:
        logger.error(f"File upload error: {e}")
        return jsonify({"error": f"Failed to process file: {str(e)}"}), 500


EMBEDDED_SAMPLE_CSV = """UID\tInput Date\tALLOTED TO\tOutput Status\tOutput Comments\tPENDINGCPNAME\tTYPE\tCredental\tUrl\tNPI1\tNPI 1 Url\tUPDATETOCLINICALPROVIDERID\tSTATUS\tCOMMENTS\tREVIEW STATUS\tPending CPID URL\tSuggested Admin URL\tCONTEXT_ID\tCONTEXT_NAME\tSTACKID\tCREATED\tCREATEDBY\tPENDINGCPID\tPENDINGCPNAME\tNAME\tPRACTICEID\tPROVIDERID\tNOTES\tADDRESS\tCITY\tSTATE\tPHONE\tFAX\tCPDETAILS\tTYPE\tUPDATETOCLINICALPROVIDERID\tEXISTINGCPDETAILS\tCLINICALPROVIDERRECEIVERID\tELECTRONIC_RECEIVER_NAME\tORDER_CODES_FROM\tNCPDPID\tLINKED_WITH_INTERFACE\tDUPLICATE_RECORDS\tFIRSTNAME\tLASTNAME\tMIDDLENAME\tNPI\tZIP\tID\tLINKED_WITH_DOCUMENT\tNAME_LIST\tMARKED\t
771\t29-07-2026\tSanthiya\tAdded\t\tKRISTY GOODNOUGH, 8042 (new)\tNEW\t\t\t\t\t\t\t\t\t\t\t8042\tVA - Privia Health\t65\t28-07-2026\tatobon1\t-153509C8042\tKRISTY GOODNOUGH, 8042 (new)\tKRISTY GOODNOUGH\t\t\tCreated from CPSW\t3414 OLANDWOOD COURT \tOLNEY\tMD\t(301) 774-0500\t(301) 774-7378\t3414 OLANDWOOD COURT  OLNEY MD (301) 774-0500 (301) 774-7378\tNEW\t\t\t\t\t\t\t\t\tKRISTY\tGOODNOUGH\t\t1649495409\t20832\t-153509\t-153509\tCONSULT\t\t\t\t
2612\t29-07-2026\tSanthiya\tAdded\t\tKYLE CORBIN, 32523 (new)\tNEW\t\t\t\t\t\t\t\t\t\t\t32523\tMA - Patient Focused Primary Care\t5\t27-07-2026\temedeiros12\t-1121C32523\tKYLE CORBIN, 32523 (new)\tKYLE CORBIN\t\t\tCreated from CPSW\t664 TAUNTON AVE \tSEEKONK\tMA\t(508) 336-4114\t(508) 557-0261\t664 TAUNTON AVE  SEEKONK MA (508) 336-4114 (508) 557-0261\tNEW\t\t\t\t\t\t\t\t\tKYLE\tCORBIN\t\t1962951087\t02771\t-1121\t-1121\tCONSULT\t\t\t\t
1050\t29-07-2026\tSanthiya\tAdded\t\tLEE, JAMES, 29786 (new)\tNEW\t\t\t\t\t\t\t\t\t\t\t29786\tCA - Alignment Healthcare USA, LLC\t36\t27-07-2026\tpdo20\t-8061C29786\tLEE, JAMES, 29786 (new)\tLEE, JAMES\t\t\tCreated from CPSW\t7248 S LAND PARK DR STE 205 \tSACRAMENTO\tCA\t(916) 392-4000\t(916) 392-2722\t7248 S LAND PARK DR STE 205  SACRAMENTO CA (916) 392-4000 (916) 392-2722\tNEW\t\t\t\t\t\t\t\t\t\t\t\t\t95831\t-8061\t-8061\tCONSULT\t\t\t\t
995\t29-07-2026\tSanthiya\tAdded\t\tKYLA DIESNER DNP, FNP-C, 1576 (new)\tNEW\t\t\t\t\t\t\t\t\t\t\t1576\tAZ - CHS - NW Allied Physicians, LLC\t27\t28-07-2026\tmmorita\t-8640C1576\tKYLA DIESNER DNP, FNP-C, 1576 (new)\tKYLA DIESNER DNP, FNP-C\t\t\t\t13395 N MARANA MAIN STREET \tMARANA\tAZ\t(520) 682-4111\t(520) 825-6841\t13395 N MARANA MAIN STREET  MARANA AZ (520) 682-4111 (520) 825-6841\tNEW\t\t\t\t\t\t\t\t\t\t\t\t1366476970\t85653\t-8640\t-8640\tALL\t\t\t\t
552\t29-07-2026\tSanthiya\tAdded\t\tKRYSTEN ASHLEY MESCAN, APRN-CNP, FNP-BC, 27322 (new)\tNEW\t\t\t\t\t\t\t\t\t\t\t27322\tOK - RESTORATIVE HEALTH SOLUTIONS, LLC\t35\t28-07-2026\tralspach5\t-281C27322\tKRYSTEN ASHLEY MESCAN, APRN-CNP, FNP-BC, 27322 (new)\tKRYSTEN ASHLEY MESCAN, APRN-CNP, FNP-BC\t\t\t\t900 N PORTER AVE STE 209\tNORMAN\tOK\t(405) 217-9997\t(405) 307-8520\t900 N PORTER AVE STE 209 NORMAN OK (405) 217-9997 (405) 307-8520\tNEW\t\t\t\t\t\t\t\t\t\t\t\t\t73071\t-281\t\tALL\t\t\t\t
108\t29-07-2026\tSanthiya\tAdded\t\tLAJOS TOTH MD, 4399 (new)\tNEW\t\t\t\t\t\t\t\t\t\t\t4399\tAL - Upperline Health\t17\t27-07-2026\tssayiner\t-21772C4399\tLAJOS TOTH MD, 4399 (new)\tLAJOS TOTH MD\t\t\tCreated from CPSW\t725 JESSE JEWELL PKWY SE \tGAINESVILLE\tGA\t(770) 535-3611\t(770) 297-5630\t725 JESSE JEWELL PKWY SE  GAINESVILLE GA (770) 535-3611 (770) 297-5630\tNEW\t\t\t\t\t\t\t\t\tLAJOS\tTOTH\t\t1306816467\t30501\t-21772\t-21772\tCONSULT\t\t\t\t
"""


@app.route("/api/sample", methods=["GET", "POST"])
def load_sample():
    """Load sample dataset into session for quick testing."""
    sample_path = os.path.join(BASE_DIR, "input", "sample_input.csv")
    session_id = str(uuid.uuid4())
    try:
        if os.path.exists(sample_path):
            df = read_input_file(sample_path)
        else:
            df = read_input_file(io.StringIO(EMBEDDED_SAMPLE_CSV), filename="sample_input.csv")

        df = df.fillna("")

        SESSIONS[session_id] = {
            "session_id": session_id,
            "filename": "sample_input.csv",
            "df_input": df,
            "df_output": None,
            "invalid_npi_rows": [],
            "mismatch_rows": [],
            "npi_notes_map": {},
            "stats": {},
            "status": "ready",
            "created_at": time.time()
        }

        all_cols = list(df.columns)
        priority_cols = ["PENDINGCPNAME", "PROVIDER_NAME", "NAME", "NPI", "FIRSTNAME", "LASTNAME", "ADDRESS", "CITY", "STATE", "PHONE"]
        front_cols = [c for c in priority_cols if c in all_cols]
        other_cols = [c for c in all_cols if c not in front_cols]
        ordered_cols = front_cols + other_cols

        return jsonify({
            "success": True,
            "session_id": session_id,
            "filename": "sample_input.csv",
            "row_count": len(df),
            "columns": ordered_cols,
            "preview": df[ordered_cols].head(5).to_dict(orient="records")
        })
    except Exception as e:
        logger.error(f"Error loading sample dataset: {e}")
        return jsonify({"error": f"Error loading sample dataset: {str(e)}"}), 500


@app.route("/api/process_stream/<session_id>", methods=["GET"])
def process_stream(session_id):
    """Real-time Server-Sent Events (SSE) endpoint for batch provider verification."""
    if session_id not in SESSIONS:
        return jsonify({"error": "Session not found or expired."}), 404

    session_data = SESSIONS[session_id]
    df_input = session_data["df_input"].copy()

    def generate_events():
        session_data["status"] = "processing"
        yield f"data: {json.dumps({'type': 'start', 'message': 'Starting Credential and NPI Verification Engine...'})}\n\n"

        final_df = None
        invalid_rows = []
        mismatch_rows = []
        npi_notes_map = {}
        final_stats = {}

        try:
            for event in process_dataframe_stream(df_input, logger=logger):
                if event["type"] == "progress":
                    yield f"data: {json.dumps(event)}\n\n"
                elif event["type"] == "complete":
                    final_df = event["df"]
                    invalid_rows = event["invalid_npi_rows"]
                    mismatch_rows = event["mismatch_rows"]
                    npi_notes_map = event["npi_notes_map"]
                    final_stats = event["stats"]

            session_data["df_output"] = final_df
            session_data["invalid_npi_rows"] = invalid_rows
            session_data["mismatch_rows"] = mismatch_rows
            session_data["npi_notes_map"] = npi_notes_map
            session_data["stats"] = final_stats
            session_data["status"] = "completed"

            yield f"data: {json.dumps({'type': 'finished', 'session_id': session_id, 'invalid_count': len(invalid_rows), 'mismatch_count': len(mismatch_rows), 'stats': final_stats})}\n\n"

        except Exception as e:
            import traceback
            traceback.print_exc()
            logger.error(f"Error during verification stream: {e}")
            session_data["status"] = "error"
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return Response(generate_events(), mimetype="text/event-stream")


@app.route("/api/results/<session_id>", methods=["GET"])
def get_results(session_id):
    """Return completed verification results DataFrame with key result columns placed at the front."""
    if session_id not in SESSIONS:
        return jsonify({"error": "Session not found."}), 404

    session_data = SESSIONS[session_id]
    df_output = session_data.get("df_output")

    if df_output is None:
        return jsonify({"error": "No output processed yet."}), 400

    df_clean = df_output.fillna("")

    # Reorder columns to place key results FIRST so user sees them immediately without scrolling
    all_cols = list(df_clean.columns)
    priority_order = [
        "PENDINGCPNAME", "PROVIDER_NAME", "NAME",
        "NPI 1", "NPI1",
        "Credential", "Credental",
        "NPI Status", "Match Confidence", "Validation Notes",
        "NPI Entity Type", "NPI Validation", "NPI",
        "Url", "NPI 1 Url", "NPI1 Url"
    ]

    front_cols = [c for c in priority_order if c in all_cols]
    other_cols = [c for c in all_cols if c not in front_cols]
    ordered_cols = front_cols + other_cols

    df_reordered = df_clean[ordered_cols]

    return jsonify({
        "success": True,
        "filename": session_data.get("filename", "output"),
        "columns": ordered_cols,
        "rows": df_reordered.to_dict(orient="records"),
        "invalid_npi_rows": session_data.get("invalid_npi_rows", []),
        "mismatch_rows": session_data.get("mismatch_rows", []),
        "npi_notes_map": session_data.get("npi_notes_map", {}),
        "stats": session_data.get("stats", {})
    })


@app.route("/api/download/<session_id>", methods=["GET"])
def download_file(session_id):
    """Download processed verification output (.xlsx, .csv, or .json)."""
    if session_id not in SESSIONS:
        return jsonify({"error": "Session not found."}), 404

    session_data = SESSIONS[session_id]
    df_output = session_data.get("df_output")

    if df_output is None:
        return jsonify({"error": "No output available for download yet. Run verification first."}), 400

    file_format = request.args.get("format", "xlsx").lower()
    raw_name = session_data.get("filename", "provider_output")
    base_name = os.path.splitext(raw_name)[0] or "provider_output"

    if file_format == "csv":
        csv_buffer = io.StringIO()
        df_output.to_csv(csv_buffer, index=False)
        csv_bytes = io.BytesIO(csv_buffer.getvalue().encode("utf-8"))
        download_filename = f"{base_name}_verified.csv"
        response = send_file(
            csv_bytes,
            mimetype="text/csv",
            as_attachment=True,
            download_name=download_filename
        )
        response.headers["Content-Disposition"] = f'attachment; filename="{download_filename}"'
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response
    elif file_format == "json":
        json_str = df_output.to_json(orient="records", indent=2)
        json_bytes = io.BytesIO(json_str.encode("utf-8"))
        download_filename = f"{base_name}_verified.json"
        response = send_file(
            json_bytes,
            mimetype="application/json",
            as_attachment=True,
            download_name=download_filename
        )
        response.headers["Content-Disposition"] = f'attachment; filename="{download_filename}"'
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response
    else:  # default styled XLSX with openpyxl cell comments
        excel_bytes = generate_excel_bytes(
            df_output,
            invalid_npi_rows=session_data.get("invalid_npi_rows", []),
            mismatch_rows=session_data.get("mismatch_rows", []),
            npi_notes_map=session_data.get("npi_notes_map", {})
        )
        download_filename = f"{base_name}_verified.xlsx"
        response = send_file(
            io.BytesIO(excel_bytes),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=download_filename
        )
        response.headers["Content-Disposition"] = f'attachment; filename="{download_filename}"'
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response


@app.route("/api/verify_single", methods=["POST"])
def verify_single():
    """Endpoint for Single Provider search queries."""
    data = request.json or {}

    try:
        result = verify_single_provider(data)
        return jsonify({
            "success": True,
            "input": data,
            "result": result
        })
    except Exception as e:
        logger.error(f"Single provider verification error: {e}")
        return jsonify({"error": f"Failed to perform search: {str(e)}"}), 500


if __name__ == "__main__":
    print("\n" + "=" * 65)
    print("  Credential and NPI Finder - Web Server                  ")
    print("  Running on: http://127.0.0.1:5000                          ")
    print("=" * 65 + "\n")
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
