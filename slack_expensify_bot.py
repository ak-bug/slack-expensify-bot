#!/usr/bin/env python3
"""
Slack → Expensify Reimbursement Bot (v3.1)
=========================================

*This iteration simply fills in the code that was truncated, so the file is
syntactically complete and runnable.*

Main changes vs. v3
-------------------
* Completed the `handle_message_events` function (saving the file, submitting
  to Expensify, spawning the poller, cleanup).
* Added the `if __name__ == "__main__"` entry‑point.
* No behavioural change—just restores the missing tail.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Final, Optional

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

# ─── Configuration ────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN: Final[str] = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN: Final[str] = os.environ["SLACK_APP_TOKEN"]
CATEGORY: Final[str] = "Travel (Candidates, Advisors, Sales, HQ, etc)"

EXPENSIFY_USER_ID: Final[str] = os.environ["EXPENSIFY_USER_ID"]
EXPENSIFY_USER_SECRET: Final[str] = os.environ["EXPENSIFY_USER_SECRET"]
EXPENSIFY_POLICY_ID: Final[str] = os.environ["EXPENSIFY_POLICY_ID"]
EXPENSIFY_EMPLOYEE_EMAIL: Final[str] = os.environ["EXPENSIFY_EMPLOYEE_EMAIL"]
EXPENSIFY_URL: Final[str] = (
    "https://integrations.expensify.com/Integration-Server/ExpensifyIntegrations"
)

POLL_INTERVAL_SEC: Final[int] = int(os.getenv("POLL_INTERVAL_SEC", "15"))
MAX_POLLS: Final[int] = int(os.getenv("MAX_POLLS", "10"))

VALID_FILETYPES = {"png", "jpg", "jpeg", "pdf"}
TMP_DIR = Path("/tmp/slack_expensify")
TMP_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("SlackExpensifyBot")

app = App(token=SLACK_BOT_TOKEN)

# ─── Helper: upload receipt ───────────────────────────────────────────────────

def submit_to_expensify(filepath: Path) -> None:
    """Upload *filepath* to Expensify and create a zero‑amount expense."""

    filename = filepath.name
    logger.info("Submitting %s to Expensify", filename)

    req = {
        "type": "create",
        "credentials": {
            "partnerUserID": EXPENSIFY_USER_ID,
            "partnerUserSecret": EXPENSIFY_USER_SECRET,
        },
        "onFinish": {"action": "markSubmitted"},
    }

    expense = {
        "created": int(time.time()),
        "merchant": "Slack Receipt",
        "amount": 0,
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
                "requestJobDescription": json.dumps(req),
                "data": json.dumps(data),
            },
            timeout=60,
        )

    if resp.status_code != 200:
        logger.error("Expensify error for %s: %s", filename, resp.text)
        raise RuntimeError(resp.text)

    logger.info("Expensify accepted %s", filename)

# ─── Helper: look up expense by externalID ─────────────────────────────────────

def fetch_expense(external_id: str) -> Optional[dict]:
    """Return the first expense dict keyed by *external_id*, or None if absent."""

    job = {
        "type": "download",
        "credentials": {
            "partnerUserID": EXPENSIFY_USER_ID,
            "partnerUserSecret": EXPENSIFY_USER_SECRET,
        },
        "inputSettings": {
            "type": "expenses",
            "filters": {"externalID": external_id},
            "dateRange": "all",
        },
        "outputSettings": {"fileExtension": "json"},
    }

    resp = requests.post(
        EXPENSIFY_URL,
        data={"requestJobDescription": json.dumps(job)},
        timeout=60,
    )

    if resp.status_code != 200:
        raise RuntimeError(resp.text)

    try:
        blob = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Non‑JSON download result: %s", exc)
        return None

    expenses = blob.get("expenses", [])
    return expenses[0] if expenses else None

# ─── Poller thread ────────────────────────────────────────────────────────────

def poll_smarts_scan(external_id: str, channel: str, thread_ts: str):
    """Poll Expensify and report `transactionStatus` back to Slack."""

    for attempt in range(1, MAX_POLLS + 1):
        time.sleep(POLL_INTERVAL_SEC)
        try:
            exp = fetch_expense(external_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("Lookup failed: %s", exc)
            app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"⚠️ Expensify lookup error: {exc}",
            )
            return

        if not exp:
            logger.info("Expense %s not yet visible", external_id)
            app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=(
                    f"⌛ SmartScan status = *NOT_YET_SYNCED* "
                    f"(attempt {attempt}/{MAX_POLLS})"
                ),
            )
            continue

        status = exp.get("transactionStatus") or exp.get("receiptState")
        amount_cents = exp.get("amount", 0)

        # Fallback if API doesn't expose status
        if not status:
            status = "PROCESSING" if amount_cents == 0 else "COMPLETED"

        if status.upper() not in {"COMPLETED", "ERROR"}:
            app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=(
                    f"⌛ SmartScan status = *{status}* "
                    f"(attempt {attempt}/{MAX_POLLS})"
                ),
            )
            continue

        if status.upper() == "ERROR":
            err_msg = exp.get("comment", "Unknown error")
            app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"⚠️ SmartScan failed: {err_msg}",
            )
            return

        # COMPLETED
        merchant = exp.get("merchant", "[merchant unknown]")
        created_unix = exp.get("created", 0)
        date_str = time.strftime("%Y-%m-%d", time.localtime(created_unix))
        dollars = amount_cents / 100.0

        app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                f"✅ SmartScan complete → *{merchant}* “${dollars:,.2f}” on "
                f"{date_str}. Expense is now in Expensify."
            ),
        )
        return

    # Timed out
    app.client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=(
            "⚠️ SmartScan hasn’t finished after several minutes. It will still "
            "complete in Expensify eventually, but I’ve stopped polling."
        ),
    )

# ─── Slack event handler ──────────────────────────────────────────────────────

@app.event("message")
def handle_message_events(body, say, client):  # noqa: ANN001
    """Triggered on every message that includes a file upload."""

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

        # Get the private download URL
        file_meta = client.files_info(file=file_id)
        download_url = file_meta["file"]["url_private_download"]

        headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
        resp = requests.get(download_url, headers=headers, timeout=60)
        if resp.status_code != 200:
            say(
                channel=event["channel"],
                thread_ts=event.get("ts"),
                text=f"⚠️ Could not download *{file_name}*: {resp.text}",
            )
            continue

        tmp_path = TMP_DIR / file_name
        tmp_path.write_bytes(resp.content)

        thread_ts = event.get("thread_ts") or event.get("ts")
        channel_id = event["channel"]

        try:
            submit_to_expensify(tmp_path)
            say(
                channel=channel_id,
                thread_ts=thread_ts,
                text="📤 Uploaded receipt to Expensify. Waiting for SmartScan…",
            )
            # Start poller
            threading.Thread(
                target=poll_smarts_scan,
                args=(file_name, channel_id, thread_ts),
                daemon=True,
            ).start()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Submission failed")
            say(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"⚠️ Failed to submit *{file_name}*: {exc}",
            )
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass

# ─── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting Slack → Expensify bot v3.1 …")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
