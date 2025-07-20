#!/usr/bin/env python3
"""
Slack → Expensify Reimbursement Bot
===================================

Listens for any Slack message that contains a receipt file (PNG, JPG, or PDF)
and automatically creates a reimbursable expense in Expensify under the
category:
    "Travel (Candidates, Advisors, Sales, HQ, etc)"

Key features
------------
* Works in any channel (public, private, or DM) where the bot is present.
* Supports images/PDFs pasted directly into Slack.
* Uses Expensify SmartScan by submitting the receipt with amount **0** so the
  scan fills merchant, amount, and date for you.
* Replies in‐thread to confirm success or report errors.
* Minimal external dependencies (Slack Bolt, requests, python‑dotenv).

Prerequisites
-------------
1. **Slack app** with the following:
   * *Socket Mode* enabled.
   * Scopes: `app_mentions:read`, `channels:history`, `files:read`,
     `chat:write`.
   * *Bot Token* (`SLACK_BOT_TOKEN`) and *App Level Token* (`SLACK_APP_TOKEN`).
2. **Expensify Integration Server** credentials:
   * `EXPENSIFY_USER_ID`, `EXPENSIFY_USER_SECRET` (partner user creds)
   * `EXPENSIFY_POLICY_ID` – your policy ID
   * `EXPENSIFY_EMPLOYEE_EMAIL` – the employee that owns the expenses
3. Python 3.10+ and:
   ```bash
   pip install slack_bolt slack_sdk requests python-dotenv
   ```
4. A `.env` file or environment variables containing the above secrets.

Running
-------
```bash
python slack_expensify_bot.py
```
Keep the process running (e.g., PM2, systemd, Docker) so it can listen for
Slack events.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Final

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

# ─── Configuration─────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN: Final[str] = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN: Final[str] = os.environ["SLACK_APP_TOKEN"]  # for Socket Mode
CATEGORY: Final[str] = "Travel (Candidates, Advisors, Sales, HQ, etc)"

EXPENSIFY_USER_ID: Final[str] = os.environ["EXPENSIFY_USER_ID"]
EXPENSIFY_USER_SECRET: Final[str] = os.environ["EXPENSIFY_USER_SECRET"]
EXPENSIFY_POLICY_ID: Final[str] = os.environ["EXPENSIFY_POLICY_ID"]
EXPENSIFY_EMPLOYEE_EMAIL: Final[str] = os.environ["EXPENSIFY_EMPLOYEE_EMAIL"]
EXPENSIFY_URL: Final[str] = (
    "https://integrations.expensify.com/Integration-Server/ExpensifyIntegrations"
)

VALID_FILETYPES = {"png", "jpg", "jpeg", "pdf"}
TMP_DIR = Path("/tmp/slack_expensify")
TMP_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("SlackExpensifyBot")

app = App(token=SLACK_BOT_TOKEN)

# ─── Expensify helper──────────────────────────────────────────────────────────

def submit_to_expensify(filepath: Path) -> None:
    """Upload *filepath* to Expensify and create a zero‑amount expense."""

    filename = filepath.name
    logger.info("Submitting %s to Expensify", filename)

    request_job_description = {
        "type": "create",
        "credentials": {
            "partnerUserID": EXPENSIFY_USER_ID,
            "partnerUserSecret": EXPENSIFY_USER_SECRET,
        },
        # Mark the report as submitted once created so reimbursement can flow.
        "onFinish": {"action": "markSubmitted"},
    }

    expense = {
        "created": int(time.time()),
        "merchant": "Slack Receipt",
        "amount": 0,  # SmartScan fills this in after upload
        "currency": "USD",
        "category": CATEGORY,
        "externalID": filename,
        "filename": filename,
    }

    data = {
        "policyID": EXPENSIFY_POLICY_ID,
        "employeeEmail": EXPENSIFY_EMPLOYEE_EMAIL,
        "expenses": [expense],
    }

    with filepath.open("rb") as f:
        resp = requests.post(
            EXPENSIFY_URL,
            files={"receipt": (filename, f, "application/octet-stream")},
            data={
                "requestJobDescription": json.dumps(request_job_description),
                "data": json.dumps(data),
            },
            timeout=60,
        )

    if resp.status_code != 200:
        logger.error("Expensify error for %s: %s", filename, resp.text)
        raise RuntimeError(resp.text)

    logger.info("Expensify accepted %s", filename)

# ─── Slack event handlers──────────────────────────────────────────────────────

@app.event("message")
def handle_message_events(body, say, client):
    event = body.get("event", {})

    files = event.get("files")
    if not files:
        return  # Not a file‑share message

    for file_info in files:
        if file_info.get("filetype") not in VALID_FILETYPES:
            continue  # Skip non‑receipt files

        file_id = file_info["id"]
        file_name = file_info["name"]
        logger.info("Downloading %s (id=%s) from Slack", file_name, file_id)

        # Retrieve the download URL
        file_res = client.files_info(file=file_id)
        download_url = file_res["file"]["url_private_download"]

        # Download the file using the bot token for auth
        headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
        dl_resp = requests.get(download_url, headers=headers, timeout=60)
        if dl_resp.status_code != 200:
            logger.error("Slack download failed for %s: %s", file_name, dl_resp.text)
            say(channel=event["channel"], thread_ts=event.get("ts"),
                text=f"⚠️ Could not download *{file_name}* from Slack: {dl_resp.text}")
            continue

        temp_path = TMP_DIR / file_name
        temp_path.write_bytes(dl_resp.content)

        try:
            submit_to_expensify(temp_path)
            say(channel=event["channel"], thread_ts=event.get("ts"),
                text=f"✅ Submitted *{file_name}* to Expensify for reimbursement.")
        except Exception as exc:
            say(channel=event["channel"], thread_ts=event.get("ts"),
                text=f"⚠️ Failed to submit *{file_name}*: {exc}")
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

# ─── Entrypoint───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting Slack → Expensify bot …")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
