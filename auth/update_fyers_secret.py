"""
Update FYERS_SECRET_KEY in .env
===============================
Run: python update_fyers_secret.py
Paste the CURRENT Secret Key shown for your app at https://myapi.fyers.in
Then run: python daily_login.py
"""

import os
import sys

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def main():
    if not os.path.exists(ENV_FILE):
        print(f"ERROR: {ENV_FILE} not found")
        sys.exit(1)

    print("=" * 60)
    print("  UPDATE FYERS SECRET KEY")
    print("=" * 60)
    print()
    print("Steps to find the current Secret Key:")
    print("  1. Open https://myapi.fyers.in in your browser")
    print("  2. Log in with your Fyers account")
    print("  3. Click your app (App ID: J454Y5EJLV)")
    print("  4. Copy the 'Secret Key' value")
    print("  5. If the key looks too short, click REGENERATE and copy the new one")
    print()

    new_secret = input("Paste the new Secret Key: ").strip()
    if not new_secret:
        print("ERROR: No secret provided.")
        sys.exit(1)

    with open(ENV_FILE, encoding="utf-8") as f:
        lines = f.read().splitlines()

    updated = False
    for i, line in enumerate(lines):
        if line.strip().startswith("FYERS_SECRET_KEY"):
            lines[i] = f"FYERS_SECRET_KEY = \"{new_secret}\""
            updated = True
            break

    if not updated:
        lines.append(f"FYERS_SECRET_KEY = \"{new_secret}\"")
        updated = True

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Updated FYERS_SECRET_KEY in {ENV_FILE}")
    print("Now run: python daily_login.py")


if __name__ == "__main__":
    main()
