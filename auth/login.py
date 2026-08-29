"""
Fyers API Login Script
======================
1. Starts a local callback server on http://127.0.0.1:5000
2. Opens Fyers login page in your browser
3. After login, captures the auth_code from the redirect
4. Exchanges auth_code for an access_token
5. Saves the access_token to fyers_token.json for later use

Features:
- Automatic token refresh: checks if saved token is still valid
  and re-authenticates via browser if expired or invalid
- Callable as a module: use get_fyers_client() from other scripts

Usage:
    # Interactive login
    python login.py

    # As a module (auto-refreshes if needed)
    from login import get_fyers_client
    fyers = get_fyers_client()
    print(fyers.get_profile())
"""

import hashlib
import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from fyers_apiv3 import fyersModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CLIENT_ID = os.environ.get("FYERS_CLIENT_ID", "")
SECRET_KEY = os.environ.get("FYERS_SECRET_KEY", "")
REDIRECT_URI = os.environ.get("FYERS_REDIRECT_URI", "http://127.0.0.1:5000/callback")
GRANT_TYPE = "authorization_code"
RESPONSE_TYPE = "code"
STATE = "fyers_login"

TOKEN_FILE = os.path.join(os.path.dirname(__file__), "fyers_token.json")

# Absolute directory for Fyers SDK debug logs. log_path="" makes the SDK
# write logs to the current working directory, which is C:\Windows\System32
# when launched by Windows Task Scheduler — causing a PermissionError.
# Always use an absolute path based on this file's location.
FYERS_LOG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Fyers tokens typically expire at the end of the trading day (~24 hours).
# We use 22 hours as a safe threshold before the market close.
TOKEN_MAX_AGE_SECONDS = 22 * 60 * 60  # 22 hours


def load_env():
    """Load .env file if it exists."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


def _resolve_credentials():
    """Return (client_id, secret_key, redirect_uri) from env or globals."""
    return (
        os.environ.get("FYERS_CLIENT_ID", CLIENT_ID),
        os.environ.get("FYERS_SECRET_KEY", SECRET_KEY),
        os.environ.get("FYERS_REDIRECT_URI", REDIRECT_URI),
    )


# ---------------------------------------------------------------------------
# Callback server for browser-based login
# ---------------------------------------------------------------------------
class CallbackHandler(BaseHTTPRequestHandler):
    """Handles the redirect from Fyers after login."""

    auth_code = None

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "auth_code" in params:
            CallbackHandler.auth_code = params["auth_code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Login successful!</h2>"
                b"<p>You can close this tab and return to the terminal.</p>"
                b"</body></html>"
            )
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h2>Missing auth_code</h2></body></html>")

    def log_message(self, format, *args):
        pass


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------
def _load_saved_token() -> dict | None:
    """Load saved token data from disk, or None if unavailable."""
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        if "access_token" not in data:
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _token_age_seconds(token_data: dict) -> float | None:
    """Return the age of the saved token in seconds, or None if unknown."""
    created = token_data.get("created_at")
    if not created:
        return None
    try:
        created_dt = datetime.fromisoformat(created)
        return (datetime.now(timezone.utc) - created_dt).total_seconds()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------
def is_token_valid(access_token: str, client_id: str) -> bool:
    """Check if the access token is still valid by calling get_profile()."""
    try:
        fyers = fyersModel.FyersModel(
            token=access_token, is_async=False, client_id=client_id, log_path=FYERS_LOG_DIR
        )
        profile = fyers.get_profile()
        # Fyers returns {"s": "ok", ...} on success, or {"s": "error", ...} on failure
        return isinstance(profile, dict) and profile.get("s") == "ok"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Refresh token flow
# ---------------------------------------------------------------------------
def _refresh_access_token(client_id: str, secret_key: str) -> str | None:
    """
    Try to refresh the access token using the saved refresh_token.
    Returns a new access_token on success, or None on failure.

    Fyers refresh endpoint: POST /api/v3/validate-refresh-token
    Requires: appIdHash (SHA256 of app_id:secret_id), refresh_token, pin
    """
    import requests

    saved = _load_saved_token()
    if not saved or "refresh_token" not in saved:
        print("No refresh_token available.")
        return None

    pin = os.environ.get("FYERS_PIN", "")
    if not pin:
        print("FYERS_PIN not set in .env — cannot refresh token.")
        return None

    # Compute appIdHash = SHA256(app_id:secret_id)
    app_id_hash = hashlib.sha256(f"{client_id}:{secret_key}".encode()).hexdigest()

    payload = {
        "grant_type": "refresh_token",
        "appIdHash": app_id_hash,
        "refresh_token": saved["refresh_token"],
        "pin": pin,
    }

    try:
        resp = requests.post(
            "https://api-t1.fyers.in/api/v3/validate-refresh-token",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        data = resp.json()

        if data.get("s") == "ok" and "access_token" in data:
            print("Refresh token succeeded — new access_token obtained.")
            return data["access_token"]
        else:
            print(f"Refresh token failed: {data.get('message', 'Unknown error')} (code: {data.get('code')})")
            return None
    except Exception as e:
        print(f"Refresh token request failed: {e}")
        return None


def _save_token(access_token: str, client_id: str, refresh_token: str | None = None) -> None:
    """Save the access token with metadata to disk."""
    token_data = {
        "access_token": access_token,
        "client_id": client_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if refresh_token:
        token_data["refresh_token"] = refresh_token
    else:
        # Preserve existing refresh_token if not provided
        saved = _load_saved_token()
        if saved and "refresh_token" in saved:
            token_data["refresh_token"] = saved["refresh_token"]
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)


# ---------------------------------------------------------------------------
# Browser-based login flow
# ---------------------------------------------------------------------------
def _interactive_login(client_id: str, secret_key: str, redirect_uri: str, auth_code: str | None = None) -> str:
    """
    Run the full browser-based login flow and return a fresh access_token.

    Starts a local callback server, opens the Fyers login page in the browser,
    waits for the redirect with the auth_code, and exchanges it for a token.
    """
    print("Starting Fyers login flow...")
    print(f"  Client ID : {client_id}")
    print(f"  Redirect  : {redirect_uri}")
    print()

    # If an auth_code was provided directly, skip the browser flow
    if auth_code:
        print(f"Using provided auth_code: {auth_code[:8]}...")
        session = fyersModel.SessionModel(
            client_id=client_id,
            redirect_uri=redirect_uri,
            response_type=RESPONSE_TYPE,
            state=STATE,
            secret_key=secret_key,
            grant_type=GRANT_TYPE,
        )
        session.set_token(auth_code)
        response = session.generate_token()
        try:
            return response["access_token"]
        except (KeyError, TypeError):
            print(f"ERROR: Failed to generate access token.\nResponse: {response}")
            sys.exit(1)

    session = fyersModel.SessionModel(
        client_id=client_id,
        redirect_uri=redirect_uri,
        response_type=RESPONSE_TYPE,
        state=STATE,
        secret_key=secret_key,
        grant_type=GRANT_TYPE,
    )

    login_url = session.generate_authcode()
    print(f"Login URL: {login_url}")
    print()

    host, port = "127.0.0.1", int(urlparse(redirect_uri).port or 5000)
    server = HTTPServer((host, port), CallbackHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"Callback server listening on {host}:{port}")

    webbrowser.open(login_url)
    print("Browser opened — please log in to your Fyers account.")
    print("Waiting for callback (or paste auth_code manually)...")

    # Wait up to 120 seconds for callback, then fall back to manual input
    deadline = time.time() + 120
    while not CallbackHandler.auth_code and time.time() < deadline:
        time.sleep(1)

    if not CallbackHandler.auth_code:
        server.shutdown()
        print("\nNo automatic callback received.")
        print(f"Open this URL in your browser and log in:\n  {login_url}\n")
        print("After login, the browser will redirect to a URL like:")
        print(f"  {redirect_uri}?auth_code=XXXXXX")
        print("Copy the auth_code value and paste it below.")
        auth_code = input("\nauth_code: ").strip()
        if not auth_code:
            print("ERROR: No auth_code provided. Login failed.")
            sys.exit(1)
        CallbackHandler.auth_code = auth_code
    else:
        # Stop the server — we got the code from the callback
        server.shutdown()

    print("\nAuth code received.")

    session.set_token(CallbackHandler.auth_code)
    response = session.generate_token()

    try:
        access_token = response["access_token"]
    except (KeyError, TypeError):
        print(f"ERROR: Failed to generate access token.\nResponse: {response}")
        sys.exit(1)

    return access_token


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_fyers_client(*, force_login: bool = False, auth_code: str | None = None) -> fyersModel.FyersModel:
    """
    Return an authenticated FyersModel instance.

    Behaviour:
    1. Loads saved token from fyers_token.json (if it exists).
    2. Checks age — if older than 22 hours, treats it as expired.
    3. Validates the token with a live get_profile() call.
    4. If invalid/expired, triggers the browser login flow and saves the new token.
    5. Returns a ready-to-use FyersModel.

    Args:
        force_login: If True, always run the browser login regardless of saved token.
    """
    load_env()
    client_id, secret_key, redirect_uri = _resolve_credentials()

    if not client_id or not secret_key:
        raise RuntimeError(
            "Missing FYERS_CLIENT_ID or FYERS_SECRET_KEY. "
            "Create a .env file — see .env.example for reference."
        )

    # --- Attempt to use saved token ---
    if not force_login:
        saved = _load_saved_token()
        if saved:
            age = _token_age_seconds(saved)
            age_str = f"{age / 3600:.1f}h" if age is not None else "unknown"
            print(f"Found saved token (age: {age_str})")

            # Quick age check before making an API call
            if age is not None and age > TOKEN_MAX_AGE_SECONDS:
                print("Token is older than 22 hours — likely expired.")
            elif is_token_valid(saved["access_token"], saved.get("client_id", client_id)):
                print("Token is valid")
                return fyersModel.FyersModel(
                    token=saved["access_token"],
                    is_async=False,
                    client_id=saved.get("client_id", client_id),
                    log_path=FYERS_LOG_DIR,
                )
            else:
                print("Token is invalid (server rejected it).")

        # --- Try refresh token (may be disabled by SEBI) ---
        if saved and saved.get("refresh_token") and not auth_code:
            print("Attempting token refresh via refresh_token...")
            refreshed = _refresh_access_token(client_id, secret_key)
            if refreshed:
                _save_token(refreshed, client_id)
                if is_token_valid(refreshed, client_id):
                    print("Refreshed token is valid.")
                    return fyersModel.FyersModel(
                        token=refreshed, is_async=False, client_id=client_id, log_path=FYERS_LOG_DIR,
                    )
                else:
                    print("Refreshed token was rejected — falling back to browser login.")
            else:
                print("Refresh unavailable (SEBI restriction) — using browser login.")

    # --- Re-authenticate ---
    print("Launching browser login flow...\n")
    access_token = _interactive_login(client_id, secret_key, redirect_uri, auth_code=auth_code)
    _save_token(access_token, client_id)
    print(f"Access token saved to {TOKEN_FILE}")

    # Verify and return
    fyers = fyersModel.FyersModel(
        token=access_token, is_async=False, client_id=client_id, log_path=FYERS_LOG_DIR
    )
    profile = fyers.get_profile()
    print(f"\nLogged in successfully!")
    print(f"Profile: {json.dumps(profile, indent=2)}")
    return fyers


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    """Run the interactive login flow from the command line."""
    import argparse
    parser = argparse.ArgumentParser(description="Fyers API Login")
    parser.add_argument("--code", help="Auth code to exchange for token (skip browser flow)")
    args = parser.parse_args()

    load_env()
    client_id, secret_key, redirect_uri = _resolve_credentials()

    if not client_id or not secret_key:
        print("ERROR: Missing credentials.")
        print("Create a .env file with FYERS_CLIENT_ID and FYERS_SECRET_KEY.")
        print("See .env.example for reference.")
        sys.exit(1)

    fyers = get_fyers_client(force_login=True, auth_code=args.code)
    print("\nReady to trade! You can now import this module and use get_fyers_client().")


if __name__ == "__main__":
    main()
