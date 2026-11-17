from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path


def load_env(path: str = ".env"):
    if not Path(path).exists():
        return
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def destinations() -> list[str]:
    ids = [x.strip() for x in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if x.strip()]
    if any(not re.fullmatch(r"-?\d+", x) for x in ids):
        raise ValueError("TELEGRAM_CHAT_IDS must contain numeric private/group chat IDs")
    return sorted(set(ids))


class DeliveryError(RuntimeError):
    def __init__(
        self, message: str, permanent=False, retry_after: int | None = None, ambiguous=False
    ):
        super().__init__(message)
        self.permanent = permanent
        self.retry_after = retry_after
        self.ambiguous = ambiguous


class Telegram:
    def __init__(self, token: str, api_root: str = "https://api.telegram.org"):
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise ValueError("Set a valid TELEGRAM_BOT_TOKEN in .env")
        self.token = token
        # Only permit the real service or a loopback test server.
        if api_root != "https://api.telegram.org" and not re.fullmatch(
            r"http://127\.0\.0\.1:\d+", api_root
        ):
            raise ValueError(
                "Telegram API root must be the official service or a loopback test server"
            )
        self.api_root = api_root

    def send(self, target: str, text: str) -> str:
        body = json.dumps(
            {"chat_id": target, "text": text, "link_preview_options": {"is_disabled": True}}
        ).encode()
        req = urllib.request.Request(
            f"{self.api_root}/bot{self.token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            try:
                response = urllib.request.urlopen(req, timeout=20)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                data = json.loads(response.read())
            if not data.get("ok"):
                code = data.get("error_code")
                raise DeliveryError(
                    f"Telegram rejected delivery (code {code})",
                    code in (400, 401, 403),
                    data.get("parameters", {}).get("retry_after"),
                )
            return str(data["result"]["message_id"])
        except DeliveryError:
            raise
        except Exception as exc:
            # Network exceptions can contain URLs with the bot token: never log them.
            raise DeliveryError(
                f"Telegram transport failed ({type(exc).__name__}); delivery outcome may be unknown",
                ambiguous=True,
            ) from exc
