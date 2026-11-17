"""Interactive Telegram setup. Run in your own SSH terminal; never share the token."""

from __future__ import annotations

import getpass
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


class SetupError(RuntimeError):
    pass


def call(token, method, **params):
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        try:
            response = urllib.request.urlopen(request, timeout=25)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            payload = json.loads(response.read(1_000_000))
        if payload.get("ok") is not True:
            raise SetupError(
                f"Telegram rejected {method} (code {payload.get('error_code', 'unknown')}). Check the token or group permissions."
            )
        return payload["result"]
    except SetupError:
        raise
    except Exception as exc:
        # Network errors may contain the token-bearing URL. Do not print them.
        raise SetupError(
            f"Telegram request failed ({type(exc).__name__}); token was not displayed."
        ) from None


def groups_from_updates(updates):
    groups = {}
    for update in updates:
        for key in ("message", "my_chat_member", "chat_member"):
            chat = update.get(key, {}).get("chat", {})
            if chat.get("type") in ("group", "supergroup") and type(chat.get("id")) is int:
                groups[str(chat["id"])] = chat.get("title", "Unnamed group")
    return groups


def save_settings(path, token, group_id):
    path = Path(path)
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    keys = {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_IDS": str(group_id)}
    kept = [line for line in lines if line.split("=", 1)[0].strip() not in keys]
    content = "\n".join([*kept, *(f"{key}={value}" for key, value in keys.items())]) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".telegram-setup-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def clean_title(value):
    return "".join(c for c in str(value) if ord(c) >= 32)[:100]


def main():
    print("Telegram setup — enter your bot token here, not in chat or a shell command.")
    token = getpass.getpass("Paste BotFather token (hidden), then press Enter: ").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise SetupError(
            "That is not a valid-looking bot token. Copy the complete token from BotFather."
        )
    bot = call(token, "getMe")
    username = bot["username"]
    print(f"Bot verified: @{username}")
    if call(token, "getWebhookInfo").get("url"):
        raise SetupError(
            "This bot already has a webhook configured. Use a new dedicated bot; this helper will not change another integration."
        )
    print(f"Add @{username} to your group, then send /setup@{username} IN THE GROUP.")
    while True:
        input("After sending that group command, press Enter here: ")
        groups = groups_from_updates(
            call(
                token,
                "getUpdates",
                timeout=0,
                limit=100,
                allowed_updates=["message", "my_chat_member"],
            )
        )
        if groups:
            break
        print(
            "No group found yet. Check that the bot was added and the command includes its exact username."
        )
    choices = sorted(groups.items())
    print("\nGroups found:")
    for index, (group_id, title) in enumerate(choices, 1):
        print(f"{index}. {clean_title(title)} — {group_id}")
    while True:
        selected = input("Enter the number of your intended group: ").strip()
        if selected.isdigit() and 1 <= int(selected) <= len(choices):
            group_id, title = choices[int(selected) - 1]
            break
        print(f"Choose a number from 1 to {len(choices)}.")
    chat = call(token, "getChat", chat_id=group_id)
    if chat.get("type") not in ("group", "supergroup") or str(chat.get("id")) != group_id:
        raise SetupError("Selected group identity could not be confirmed.")
    membership = call(token, "getChatMember", chat_id=group_id, user_id=bot["id"])
    if membership.get("status") in ("left", "kicked"):
        raise SetupError("The bot is not currently a member of this group. Add it again.")
    print(f"Chosen group: {clean_title(chat.get('title', title))} ({group_id})")
    if input("Send ONE setup test message to this group now? [y/N]: ").strip().lower() in (
        "y",
        "yes",
    ):
        call(
            token,
            "sendMessage",
            chat_id=group_id,
            text="Movie Watch setup test: Telegram delivery is working. This is a setup test, not a movie booking alert.",
            link_preview_options={"is_disabled": True},
        )
        print("Test message sent. Check your Telegram group.")
    path = Path(__file__).resolve().parents[1] / ".env"
    save_settings(path, token, group_id)
    print(f"Settings saved privately in {path}. Token not displayed.")
    print("The movie monitor has NOT been started. Start the service only when ready.")


if __name__ == "__main__":
    try:
        main()
    except SetupError as exc:
        print(f"Setup stopped: {exc}")
        raise SystemExit(1)
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.")
        raise SystemExit(1)
