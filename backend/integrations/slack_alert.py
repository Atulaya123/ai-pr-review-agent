import requests


def notify_critical(webhook_url: str, message: str) -> None:
    """Post a Slack alert when a review comes back CRITICAL_BLOCK."""
    requests.post(webhook_url, json={"text": message})
