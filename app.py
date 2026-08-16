"""
app.py
------
Flask Web Application for Provider NPI & Credential Verification Suite.
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
from search import validate_existing_npi, verify_provider

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
app.url_map.strict_slashes = False
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB max upload limit

logger = setup_logger("web_app", "web_app.log")


@app.route("/static/<path:filename>")
def serve_static_assets(filename):
    return send_from_directory(STATIC_DIR, filename)

# In-memory session cache for active and processed files
# Format: session_id -> { "df_input": df, "df_output": df, "filename": str, "invalid_npi_rows": [], "mismatch_rows": [], "status": str }
SESSIONS = {}


@app.route("/")
@app.route("/index")
def index():
    """Render main application UI."""
    return render_template("index.html")


@app.errorhandler(404)
def page_not_found(e):
    api_endpoints = ("/api/upload", "/api/sample", "/api/verify", "/api/process", "/api/download")
    if any(request.path.startswith(prefix) for prefix in api_endpoints):
        return jsonify({"error": f"Endpoint not found: {request.path}"}), 404
    return render_template("index.html"), 200


@app.route("/api/upload", methods=["POST"])
def upload_file():
    """Handle XLSX / CSV file uploads."""
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
            "status": "ready",
            "created_at": time.time()
        }

        # First 5 preview rows
        preview_rows = df.head(5).to_dict(orient="records")

        return jsonify({
            "success": True,
            "session_id": session_id,
            "filename": filename,
            "row_count": len(df),
            "columns": list(df.columns),
            "preview": preview_rows
        })

    except Exception as e:
        logger.error(f"File upload processing error: {e}")
        return jsonify({"error": f"Failed to process file: {str(e)}"}), 500


@app.route("/api/sample", methods=["GET", "POST"])
def load_sample():
    """Load sample dataset into session for quick testing."""
    sample_path = os.path.join(BASE_DIR, "input", "sample_input.csv")
    if not os.path.exists(sample_path):
        return jsonify({"error": "Sample file not found on server."}), 404

    session_id = str(uuid.uuid4())
    try:
        df = read_input_file(sample_path)
        df = df.fillna("")

        SESSIONS[session_id] = {
            "session_id": session_id,
            "filename": "sample_input.csv",
            "df_input": df,
            "df_output": None,
            "invalid_npi_rows": [],
            "mismatch_rows": [],
            "status": "ready",
            "created_at": time.time()
        }

        return jsonify({
            "success": True,
            "session_id": session_id,
            "filename": "sample_input.csv",
            "row_count": len(df),
            "columns": list(df.columns),
            "preview": df.head(5).to_dict(orient="records")
        })
    except Exception as e:
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
        yield f"data: {json.dumps({'type': 'start', 'message': 'Starting provider verification engine...'})}\n\n"

        final_df = None
        invalid_rows = []
        mismatch_rows = []

        try:
            for event in process_dataframe_stream(df_input, logger=logger):
                if event["type"] == "progress":
                    yield f"data: {json.dumps(event)}\n\n"
                elif event["type"] == "complete":
                    final_df = event["df"]
                    invalid_rows = event["invalid_npi_rows"]
                    mismatch_rows = event["mismatch_rows"]

            session_data["df_output"] = final_df
            session_data["invalid_npi_rows"] = invalid_rows
            session_data["mismatch_rows"] = mismatch_rows
            session_data["status"] = "completed"

            yield f"data: {json.dumps({'type': 'finished', 'session_id': session_id, 'invalid_count': len(invalid_rows), 'mismatch_count': len(mismatch_rows)})}\n\n"

        except Exception as e:
            import traceback
            traceback.print_exc()
            logger.error(f"Error during verification stream: {e}")
            session_data["status"] = "error"
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"


    return Response(generate_events(), mimetype="text/event-stream")


@app.route("/api/download/<session_id>", methods=["GET"])
def download_file(session_id):
    """Download processed verification output (xlsx, csv, or json)."""
    if session_id not in SESSIONS:
        return jsonify({"error": "Session not found."}), 404

    session_data = SESSIONS[session_id]
    df_output = session_data.get("df_output")

    if df_output is None:
        return jsonify({"error": "No output available for download yet. Run processing first."}), 400

    file_format = request.args.get("format", "xlsx").lower()
    base_name = os.path.splitext(session_data["filename"])[0]

    if file_format == "csv":
        csv_buffer = io.StringIO()
        df_output.to_csv(csv_buffer, index=False)
        csv_bytes = io.BytesIO(csv_buffer.getvalue().encode("utf-8"))
        return send_file(
            csv_bytes,
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"{base_name}_verified.csv"
        )
    elif file_format == "json":
        json_str = df_output.to_json(orient="records", indent=2)
        json_bytes = io.BytesIO(json_str.encode("utf-8"))
        return send_file(
            json_bytes,
            mimetype="application/json",
            as_attachment=True,
            download_name=f"{base_name}_verified.json"
        )
    else:  # default XLSX
        excel_bytes = generate_excel_bytes(
            df_output,
            invalid_npi_rows=session_data.get("invalid_npi_rows", []),
            mismatch_rows=session_data.get("mismatch_rows", [])
        )
        return send_file(
            io.BytesIO(excel_bytes),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=f"{base_name}_verified.xlsx"
        )


@app.route("/api/verify_single", methods=["POST"])
def verify_single():
    """Interactive single-provider lookup endpoint."""
    data = request.json or {}

    provider_name = data.get("provider_name", "").strip()
    firstname = data.get("firstname", "").strip()
    lastname = data.get("lastname", "").strip()
    organization = data.get("organization", "").strip()
    address = data.get("address", "").strip()
    city = data.get("city", "").strip()
    state = data.get("state", "").strip()
    zip_code = data.get("zip_code", "").strip()
    phone = data.get("phone", "").strip()
    existing_npi = data.get("existing_npi", "").strip()

    npi_validation = None
    if existing_npi:
        npi_validation = validate_existing_npi(existing_npi, is_provider_row=True)

    verification_res = verify_provider(
        provider_name=provider_name,
        organization=organization,
        address=address,
        city=city,
        state=state,
        zip_code=zip_code,
        phone=phone,
        firstname=firstname,
        lastname=lastname
    )

    return jsonify({
        "success": True,
        "input": data,
        "npi_validation": npi_validation,
        "verification": verification_res
    })


if __name__ == "__main__":
    print("\n" + "=" * 65)
    print("  Provider NPI & Credential Verification Suite - Web Server  ")
    print("  Running on: http://127.0.0.1:5000                          ")
    print("=" * 65 + "\n")
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)

