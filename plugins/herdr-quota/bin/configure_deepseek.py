#!/usr/bin/python3
"""Masked, local-only DeepSeek credential setup for herdr-quota."""

from __future__ import annotations

import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def config_path() -> Path:
    root = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if root:
        return Path(root) / ".env"
    return Path.home() / ".config" / "herdr" / "plugins" / "config" / "herdr.quota" / ".env"


def validate(token: str) -> list[str]:
    request = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "herdr-quota/0.1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    if not payload.get("is_available"):
        raise RuntimeError("The key is valid, but the account has no available API balance.")
    result = []
    for balance in payload.get("balance_infos") or []:
        result.append(f"{balance.get('total_balance', '?')} {balance.get('currency', '')}")
    return result


def save(token: str) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(".env.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("DEEPSEEK_API_KEY=" + token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def main() -> int:
    print("\033[38;2;203;166;247mDeepSeek · secure setup\033[0m")
    print()
    print("The key is entered with terminal echo disabled.")
    print("It will not appear in Herdr output, shell history, or plugin logs.")
    print()
    token = getpass.getpass("API key: ").strip()
    if not token:
        print("\nCancelled: empty key.")
        input("Press Enter to close…")
        return 1
    print("\nValidating with DeepSeek…", flush=True)
    try:
        balances = validate(token)
        path = save(token)
    except urllib.error.HTTPError as exc:
        print(f"Validation failed: DeepSeek returned HTTP {exc.code}.")
        input("Press Enter to close…")
        return 1
    except Exception as exc:
        print(f"Validation failed: {exc}")
        input("Press Enter to close…")
        return 1
    token = ""
    print("\033[38;2;166;227;161mSaved successfully.\033[0m")
    print("Balance: " + (", ".join(balances) if balances else "available"))
    print(f"File: {path} (mode 0600)")
    print("The dashboard will pick it up on the next refresh.")
    input("\nPress Enter to close…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
