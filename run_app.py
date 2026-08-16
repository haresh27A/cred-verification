"""
run_app.py
----------
Launcher script for Provider NPI & Credential Verification Suite.
Starts the Flask server and opens the web app in the browser.
"""

import sys
import time
import webbrowser
import threading
from app import app

def open_browser():
    """Wait briefly for server start and open browser."""
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:5000")

if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("  Provider NPI & Credential Verification Web Application  ")
    print("  App URL: http://127.0.0.1:5000                           ")
    print("=" * 70 + "\n")
    
    # Launch browser thread
    threading.Thread(target=open_browser, daemon=True).start()
    
    # Start Flask server
    app.run(host="127.0.0.1", port=5000, debug=False)
