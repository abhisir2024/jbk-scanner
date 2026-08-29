"""
Daily Fyers Login — simple browser + paste auth_code flow.
No callback server, no complexity.

Usage:
    python daily_login.py                  # opens browser, paste auth_code
    python daily_login.py --code CODE      # skip browser, use code directly
"""

import json
import os
import sys
import time
import webbrowser
from datetime import datetime, timezone

from fyers_apiv3 import fyersModel
from login import load_env, _resolve_credentials, _save_token, TOKEN_FILE, FYERS_LOG_DIR

# Redirect URI must match what's registered in Fyers app
REDIRECT_URI = "https://trade.fyers.in/api-login/redirect-uri/index.html"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fyers Daily Login")
    parser.add_argument("--code", help="Auth code (skip browser)")
    args = parser.parse_args()

    load_env()
    client_id, secret_key, _ = _resolve_credentials()

    if not client_id or not secret_key:
        print("ERROR: Missing credentials in .env")
        sys.exit(1)

    auth_code = args.code

    if not auth_code:
        # Step 1: Generate login URL
        session = fyersModel.SessionModel(
            client_id=client_id,
            redirect_uri=REDIRECT_URI,
            response_type="code",
            state="fyers_login",
            secret_key=secret_key,
            grant_type="authorization_code",
        )
        login_url = session.generate_authcode()

        print("=" * 60)
        print("  FYERS DAILY LOGIN")
        print("=" * 60)
        print()
        print("Step 1: Browser will open Fyers login page")
        print("Step 2: Log in with your Fyers account")
        print("Step 3: You will be redirected to a page")
        print("Step 4: Copy the auth_code from the URL bar")
        print()
        print("The URL will look like:")
        print("  ...?auth_code=eyJhbGciOi...")
        print()
        print("Copy ONLY the value after auth_code=")
        print("=" * 60)
        print()

        webbrowser.open(login_url)
        print("Browser opened!")
        print()

        auth_code = input("Paste auth_code here: ").strip()

        # Clean up: remove any URL prefix the user might have copied
        if "auth_code=" in auth_code:
            auth_code = auth_code.split("auth_code=")[-1]
        if "&" in auth_code:
            auth_code = auth_code.split("&")[0]
        if "?" in auth_code:
            auth_code = auth_code.split("?")[-1]

    if not auth_code:
        print("ERROR: No auth_code provided")
        sys.exit(1)

    # Step 2: Exchange auth_code for access_token
    print()
    print(f"Auth code: {auth_code[:20]}...")
    print("Exchanging for access token...")

    session = fyersModel.SessionModel(
        client_id=client_id,
        redirect_uri=REDIRECT_URI,
        response_type="code",
        state="fyers_login",
        secret_key=secret_key,
        grant_type="authorization_code",
    )
    session.set_token(auth_code)
    response = session.generate_token()

    try:
        access_token = response["access_token"]
    except (KeyError, TypeError):
        print(f"ERROR: Failed to generate token.")
        print(f"Response: {response}")
        print()
        print("Possible causes:")
        print("  - Auth code expired (takes too long to paste)")
        print("  - Auth code already used")
        print("  - Wrong auth code copied")
        sys.exit(1)

    # Step 3: Save token
    _save_token(access_token, client_id)
    print(f"Token saved to {TOKEN_FILE}")

    # Step 4: Verify
    fyers = fyersModel.FyersModel(
        token=access_token, is_async=False, client_id=client_id, log_path=FYERS_LOG_DIR
    )
    profile = fyers.get_profile()

    if profile.get("s") == "ok":
        name = profile.get("data", {}).get("name", "Unknown")
        fy_id = profile.get("data", {}).get("fy_id", "")
        print()
        print("=" * 60)
        print(f"  Login successful!")
        print(f"  Name: {name}")
        print(f"  ID: {fy_id}")
        print("=" * 60)
        print()
        print("You can now run: python start_scanner.bat")
    else:
        print(f"Login verification failed: {profile}")


if __name__ == "__main__":
    main()
