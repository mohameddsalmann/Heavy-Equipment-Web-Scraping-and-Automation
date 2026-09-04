"""
Helper script to fetch Telegram Chat ID and verify delivery.
1. Polls getUpdates from Telegram API.
2. Extracts chat ID from incoming user message.
3. Automatically writes TELEGRAM_CHAT_ID to .env.
4. Sends a confirmation test alert to Telegram.
"""
from __future__ import annotations

import os
import re
import sys
import httpx
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    print("Error: TELEGRAM_BOT_TOKEN is not set in .env")
    sys.exit(1)

BOT_INFO_URL = f"https://api.telegram.org/bot{TOKEN}/getMe"
UPDATES_URL = f"https://api.telegram.org/bot{TOKEN}/getUpdates"

me_res = httpx.get(BOT_INFO_URL)
me_data = me_res.json()
if not me_data.get("ok"):
    print("Error: Invalid bot token according to Telegram API.")
    sys.exit(1)

bot_username = me_data["result"]["username"]
print(f"Connected to bot: @{bot_username}")

res = httpx.get(UPDATES_URL)
data = res.json()
results = data.get("result", [])

if not results:
    print(f"\n[!] No messages received yet.")
    print(f"Please open Telegram, open https://t.me/{bot_username}")
    print(f"Click 'Start' or send any message to @{bot_username}, then re-run this script.")
    sys.exit(0)

latest = results[-1]
msg = latest.get("message") or latest.get("channel_post") or latest.get("my_chat_member", {})
chat = msg.get("chat", {})
chat_id = chat.get("id")
first_name = chat.get("first_name") or chat.get("title") or "User"

if not chat_id:
    print("Could not extract chat_id from latest update.")
    sys.exit(1)

print(f"\n[SUCCESS] Found Chat ID: {chat_id} (from: {first_name})")

# Update .env
env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
with open(env_path, "r", encoding="utf-8") as f:
    env_content = f.read()

if "TELEGRAM_CHAT_ID=" in env_content:
    env_content = re.sub(r"TELEGRAM_CHAT_ID=.*", f"TELEGRAM_CHAT_ID={chat_id}", env_content)
else:
    env_content += f"\nTELEGRAM_CHAT_ID={chat_id}\n"

with open(env_path, "w", encoding="utf-8") as f:
    f.write(env_content)

print(f"[SUCCESS] Updated .env with TELEGRAM_CHAT_ID={chat_id}")

# Send test message
send_url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
payload = {
    "chat_id": chat_id,
    "text": (
        "*KeyHub Heavy Equipment Monitor*\n\n"
        " Telegram integration test successful!\n\n"
        "Pending alert outbox and delivery pipeline are active."
    ),
    "parse_mode": "Markdown",
}

send_res = httpx.post(send_url, json=payload)
if send_res.status_code == 200:
    print("[SUCCESS] Sent test message to Telegram successfully!")
else:
    print(f"[FAILED] Sending message failed: {send_res.text}")
