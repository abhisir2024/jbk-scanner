"""
Fyers Auto Login — Fully Automatic (TOTP)
==========================================
Uses TOTP to login fully automatically. No browser, no paste.
Token is cached per day — only re-login when needed.

The Fyers API v3 now returns the access_token directly from /api/v3/token
as a JWT. The validate-authcode step is no longer needed.

Usage:
    python auto_login.py              # auto-login (uses cached token if available)
    python auto_login.py --force      # force fresh login even if token valid

Requires in .env:
    FYERS_CLIENT_ID=J454Y5EJLV-100
    FYERS_SECRET_KEY=OPZKUKAQUN
    FYERS_REDIRECT_URI=https://trade.fyers.in/api-login/redirect-uri/index.html
    FYERS_FY_ID=XA05589
    FYERS_TOTP_KEY=IUM5V675QHIT7BPHIZOQFA7JLGFYR33X
    FYERS_PIN=2027
"""

import base64
import hashlib
import json
import os
import sys
import time
from datetime import datetime, date
from urllib.parse import urlparse, parse_qs

import pyotp
import requests
from fyers_apiv3 import fyersModel

from login import load_env, _save_token, TOKEN_FILE, FYERS_LOG_DIR

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REDIRECT_URI = "https://trade.fyers.in/api-login/redirect-uri/index.html"

# Fyers TOTP login endpoints
BASE_URL_V2 = "https://api-t2.fyers.in/vagator/v2"
BASE_URL_V3 = "https://api-t1.fyers.in/api/v3"

SEND_OTP_URL = BASE_URL_V2 + "/send_login_otp_v2"
VERIFY_OTP_URL = BASE_URL_V2 + "/verify_otp"
VERIFY_PIN_URL = BASE_URL_V2 + "/verify_pin_v2"
TOKEN_URL = BASE_URL_V3 + "/token"
VALIDATE_AUTH_URL = BASE_URL_V3 + "/validate-authcode"

# Token cache file
TOKEN_CACHE_FILE = os.path.join(os.path.dirname(__file__), "BrokerToken.json")

# Retry settings
MAX_RETRIES = 3
RETRY_DELAY = 15  # seconds — Fyers rate limits aggressively


def _encode(string: str) -> str:
    """Base64 encode a string (required by Fyers API)."""
    return base64.b64encode(str(string).encode("ascii")).decode("ascii")


def _generate_totp(totp_key: str) -> str:
    """Generate current TOTP code."""
    return pyotp.TOTP(totp_key).now()


def _load_cached_token() -> str | None:
    """Load today's cached token, or None if not available."""
    if not os.path.exists(TOKEN_CACHE_FILE):
        return None
    try:
        with open(TOKEN_CACHE_FILE, "r") as f:
            data = json.load(f)
        today = date.today().strftime("%Y-%m-%d")
        if today in data:
            return data[today]
    except (json.JSONDecodeError, OSError, KeyError):
        pass
    return None


def _save_cached_token(access_token: str) -> None:
    """Save today's token to cache file."""
    today = date.today().strftime("%Y-%m-%d")
    data = {}
    if os.path.exists(TOKEN_CACHE_FILE):
        try:
            with open(TOKEN_CACHE_FILE, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    data[today] = access_token
    with open(TOKEN_CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _is_token_valid(access_token: str, client_id: str) -> bool:
    """Quick validation — call get_profile to check token."""
    try:
        fyers = fyersModel.FyersModel(
            token=access_token, is_async=False, client_id=client_id, log_path=FYERS_LOG_DIR
        )
        profile = fyers.get_profile()
        return isinstance(profile, dict) and profile.get("s") == "ok"
    except Exception:
        return False


def _auto_login_totp(fy_id: str, totp_key: str, pin: str,
                     client_id: str, secret_key: str, redirect_uri: str) -> str:
    """
    Full TOTP login flow:
    1. Send OTP request → get request_key
    2. Verify TOTP → get new request_key
    3. Verify PIN → get access_token (bearer)
    4. Use bearer to get auth_code from /api/v3/token
    5. Try to validate auth_code OR use it directly as access_token
    """
    # Step 1: Send OTP
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"  Step 1: Sending login OTP request (attempt {attempt})...")
        try:
            res = requests.post(
                SEND_OTP_URL,
                json={"fy_id": _encode(fy_id), "app_id": "2"},
                timeout=30,
            ).json()

            if "request_key" not in res:
                if res.get("code") == 429 or res.get("s") == "error":
                    wait = RETRY_DELAY * attempt
                    print(f"         Rate limited. Waiting {wait}s...")
                    time.sleep(wait)
                    continue
                print(f"ERROR: Login OTP request failed: {res}")
                sys.exit(1)
            break
        except requests.exceptions.ConnectionError:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
            else:
                raise
    else:
        print("ERROR: Max retries exceeded")
        sys.exit(1)

    # Step 2: Verify TOTP
    print("  Step 2: Verifying TOTP...")
    totp_code = _generate_totp(totp_key)
    print(f"         TOTP: {totp_code}")

    res2 = requests.post(
        VERIFY_OTP_URL,
        json={"request_key": res["request_key"], "otp": totp_code},
        timeout=30,
    ).json()

    if "request_key" not in res2:
        print(f"ERROR: TOTP verification failed: {res2}")
        sys.exit(1)

    # Step 3: Verify PIN
    print("  Step 3: Verifying PIN...")
    ses = requests.Session()
    res3 = ses.post(
        VERIFY_PIN_URL,
        json={
            "request_key": res2["request_key"],
            "identity_type": "pin",
            "identifier": _encode(pin),
        },
        timeout=30,
    ).json()

    if "data" not in res3 or "access_token" not in res3.get("data", {}):
        print(f"ERROR: PIN verification failed: {res3}")
        sys.exit(1)

    bearer_token = res3["data"]["access_token"]
    ses.headers.update({"authorization": f"Bearer {bearer_token}"})
    print(f"  Step 3: PIN verified (bearer received)")

    # Step 4: Get token/auth_code
    print("  Step 4: Getting auth code from /api/v3/token...")
    app_id_clean = client_id.split("-")[0]

    res4 = ses.post(
        TOKEN_URL,
        json={
            "fyers_id": fy_id,
            "app_id": app_id_clean,
            "redirect_uri": redirect_uri,
            "appType": "100",
            "code_challenge": "",
            "state": "None",
            "scope": "",
            "nonce": "",
            "response_type": "code",
            "create_cookie": True,
        },
        timeout=30,
    )

    # Handle different response formats
    # Format 1: HTTP 308 with "Url" field (old format)
    # Format 2: HTTP 200 with "data.auth" field (new format — access token directly)
    # Format 3: HTTP 200 with "data.auth" that's a JWT (access token)

    if res4.status_code == 308:
        # Old format: extract auth_code from Url
        r4 = res4.json()
        if "Url" in r4:
            url = r4["Url"]
            parsed = urlparse(url)
            auth_code = parse_qs(parsed.query).get("auth_code", [""])[0]
            if auth_code:
                print(f"         Got auth_code (old format), length: {len(auth_code)}")
                # Exchange via validate-authcode
                return _validate_authcode(auth_code, client_id, secret_key)
        print(f"ERROR: Unexpected 308 response: {res4.text[:200]}")
        sys.exit(1)

    r4 = res4.json()

    # Check for data.auth (new format — direct access token)
    auth_code = r4.get("data", {}).get("auth", "")

    if auth_code:
        # The data.auth IS the access_token (JWT with sub:"access_token")
        # Try validate-authcode first (it may return a longer-lived token)
        print(f"         Got auth token (length: {len(auth_code)})")

        # Method 1: Try validate-authcode
        access_token = _validate_authcode(auth_code, client_id, secret_key)
        if access_token:
            return access_token

        # Method 2: Use auth_code directly as access_token
        print("         Using auth token directly as access_token...")
        return auth_code

    # Check for direct access_token in response
    access_token = r4.get("data", {}).get("access_token", "")
    if access_token:
        print(f"         Got access_token directly (length: {len(access_token)})")
        return access_token

    print(f"ERROR: Could not extract token from response.")
    print(f"Response: {json.dumps(r4, indent=2)[:500]}")
    sys.exit(1)


def _validate_authcode(auth_code: str, client_id: str, secret_key: str) -> str | None:
    """Try to validate auth_code via validate-authcode endpoint."""
    print("         Trying validate-authcode...")
    app_hash = hashlib.sha256(f"{client_id}:{secret_key}".encode()).hexdigest()

    try:
        r5 = requests.post(
            VALIDATE_AUTH_URL,
            json={
                "grant_type": "authorization_code",
                "appIdHash": app_hash,
                "code": auth_code,
            },
            timeout=30,
        ).json()

        if r5.get("s") == "ok" and "access_token" in r5:
            access_token = r5["access_token"]
            refresh_token = r5.get("refresh_token", "")
            print(f"         validate-authcode SUCCESS!")
            if refresh_token:
                print(f"         Got refresh_token (length: {len(refresh_token)})")
                # Save refresh_token for later use
                _save_refresh_token(refresh_token)
            return access_token
        else:
            print(f"         validate-authcode failed: {r5.get('message', 'unknown')}")
            return None
    except Exception as e:
        print(f"         validate-authcode error: {e}")
        return None


def _save_refresh_token(refresh_token: str) -> None:
    """Save refresh token for later use."""
    try:
        token_path = os.path.join(os.path.dirname(__file__), "fyers_refresh_token.json")
        with open(token_path, "w") as f:
            json.dump({"refresh_token": refresh_token, "created_at": datetime.now().isoformat()}, f, indent=2)
    except Exception:
        pass


def _load_refresh_token() -> str | None:
    """Load saved refresh token."""
    try:
        token_path = os.path.join(os.path.dirname(__file__), "fyers_refresh_token.json")
        with open(token_path, "r") as f:
            data = json.load(f)
        return data.get("refresh_token")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _try_refresh_token(client_id: str, secret_key: str) -> str | None:
    """Try to refresh the access token using saved refresh token."""
    refresh_token = _load_refresh_token()
    if not refresh_token:
        return None

    print("  Trying refresh token...")
    app_hash = hashlib.sha256(f"{client_id}:{secret_key}".encode()).hexdigest()

    try:
        r = requests.post(
            BASE_URL_V3 + "/validate-refresh-token",
            json={
                "grant_type": "refresh_token",
                "appIdHash": app_hash,
                "refresh_token": refresh_token,
            },
            timeout=30,
        ).json()

        if r.get("s") == "ok" and "access_token" in r:
            new_token = r["access_token"]
            new_refresh = r.get("refresh_token", "")
            print("  Refresh token SUCCESS!")
            if new_refresh:
                _save_refresh_token(new_refresh)
            return new_token
        else:
            print(f"  Refresh failed: {r.get('message', 'unknown')}")
            return None
    except Exception as e:
        print(f"  Refresh error: {e}")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_fyers_client_auto(*, force_login: bool = False) -> fyersModel.FyersModel:
    """
    Get an authenticated FyersModel instance.

    Priority:
    1. Cached token from today (BrokerToken.json) — fastest
    2. Saved token from fyers_token.json — check if still valid
    3. Try refresh token
    4. Fresh TOTP login — full auto-login flow
    """
    load_env()

    client_id = os.environ.get("FYERS_CLIENT_ID", "")
    secret_key = os.environ.get("FYERS_SECRET_KEY", "")
    redirect_uri = os.environ.get("FYERS_REDIRECT_URI", REDIRECT_URI)
    fy_id = os.environ.get("FYERS_FY_ID", "")
    totp_key = os.environ.get("FYERS_TOTP_KEY", "")
    pin = os.environ.get("FYERS_PIN", "")

    if not client_id or not secret_key:
        raise RuntimeError("Missing FYERS_CLIENT_ID or FYERS_SECRET_KEY in .env")

    if not all([fy_id, totp_key, pin]):
        raise RuntimeError(
            "Missing TOTP credentials in .env.\n"
            "Required: FYERS_FY_ID, FYERS_TOTP_KEY, FYERS_PIN"
        )

    # --- Check 1: Today's cached token ---
    if not force_login:
        cached = _load_cached_token()
        if cached:
            print(f"Found today's cached token — checking validity...")
            if _is_token_valid(cached, client_id):
                print("Cached token is valid! No login needed.")
                _save_token(cached, client_id)
                return fyersModel.FyersModel(
                    token=cached, is_async=False, client_id=client_id, log_path=FYERS_LOG_DIR
                )
            else:
                print("Cached token expired — trying refresh...")

    # --- Check 2: Try refresh token ---
    if not force_login:
        refreshed = _try_refresh_token(client_id, secret_key)
        if refreshed and _is_token_valid(refreshed, client_id):
            print("Refresh token worked!")
            _save_cached_token(refreshed)
            _save_token(refreshed, client_id)
            return fyersModel.FyersModel(
                token=refreshed, is_async=False, client_id=client_id, log_path=FYERS_LOG_DIR
            )

    # --- Check 3: Saved token from fyers_token.json ---
    if not force_login:
        from login import _load_saved_token, _token_age_seconds, TOKEN_MAX_AGE_SECONDS
        saved = _load_saved_token()
        if saved:
            age = _token_age_seconds(saved)
            if age is not None and age < TOKEN_MAX_AGE_SECONDS:
                if _is_token_valid(saved["access_token"], saved.get("client_id", client_id)):
                    print(f"Saved token valid (age: {age/3600:.1f}h)")
                    return fyersModel.FyersModel(
                        token=saved["access_token"],
                        is_async=False,
                        client_id=saved.get("client_id", client_id),
                        log_path=FYERS_LOG_DIR,
                    )

    # --- Fresh TOTP login ---
    print("\n" + "=" * 50)
    print("  AUTO LOGIN (TOTP)")
    print("=" * 50)
    print(f"  User: {fy_id}")
    print(f"  App:  {client_id}")
    print()

    access_token = _auto_login_totp(fy_id, totp_key, pin, client_id, secret_key, redirect_uri)

    # Save to both files
    _save_cached_token(access_token)
    _save_token(access_token, client_id)
    print(f"\nToken saved to {TOKEN_CACHE_FILE} and {TOKEN_FILE}")

    # Verify
    fyers = fyersModel.FyersModel(
        token=access_token, is_async=False, client_id=client_id, log_path=FYERS_LOG_DIR
    )
    profile = fyers.get_profile()

    if profile.get("s") == "ok":
        name = profile.get("data", {}).get("name", "Unknown")
        fy_id_out = profile.get("data", {}).get("fy_id", "")
        print()
        print("=" * 50)
        print(f"  ✅ Auto-login successful!")
        print(f"  Name: {name}")
        print(f"  ID:   {fy_id_out}")
        print("=" * 50)
    else:
        print(f"\nProfile check: {profile}")
        # The token might still work for data APIs even if profile fails
        print("Token saved — may still work for data/order APIs")

    return fyers


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fyers Auto Login (TOTP)")
    parser.add_argument("--force", action="store_true", help="Force fresh login")
    args = parser.parse_args()

    fyers = get_fyers_client_auto(force_login=args.force)
    print("\nReady! You can now run: python start_scanner.bat")


if __name__ == "__main__":
    main()
