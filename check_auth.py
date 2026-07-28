"""Exit 0 if user.session is authorized, else 2. Used by start.bat."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
SESSION = ROOT / "data" / "user"


def load_env() -> dict[str, str]:
    data: dict[str, str] = {}
    if not ENV.exists():
        return data
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


async def main() -> int:
    if not Path(str(SESSION) + ".session").exists():
        print("NO_SESSION")
        return 2
    env = load_env()
    api_id = env.get("API_ID", "")
    api_hash = env.get("API_HASH", "")
    if not api_id.isdigit() or not api_hash:
        print("NO_API")
        return 2
    client = TelegramClient(str(SESSION), int(api_id), api_hash)
    await client.connect()
    try:
        ok = await client.is_user_authorized()
        if not ok:
            print("NOT_AUTHORIZED")
            return 2
        me = await client.get_me()
        if getattr(me, "bot", False):
            print("IS_BOT")
            return 2
        name = me.username or me.first_name or me.id
        print(f"OK {name}")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception as e:
        print(f"ERROR {type(e).__name__}: {e}")
        raise SystemExit(2)
