import httpx


def fetch_slack_webhook(payload):
    # deliberately no timeout — should trip the reliability_layer_required
    # invariant ingested into code_chunks, now via Gemini embeddings
    response = httpx.post("https://hooks.slack.com/services/x", json=payload)
    return response.status_code
# retrigger after groq model fix
