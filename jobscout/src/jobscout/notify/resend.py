"""Send the digest through Resend."""

from __future__ import annotations

import logging

from ..http import HttpClient

log = logging.getLogger(__name__)

_ENDPOINT = "https://api.resend.com/emails"


class EmailError(RuntimeError):
    pass


def send_digest(
    http: HttpClient,
    *,
    api_key: str,
    to: str,
    from_address: str,
    subject: str,
    html: str,
    text: str,
) -> str:
    """Send, returning the provider's message id.

    Raises EmailError with the provider's own message on failure -- a silently
    unsent digest is worse than a loud one, because a scheduled run that fails
    quietly looks exactly like a week with no matches.
    """
    if not api_key:
        raise EmailError("RESEND_API_KEY is not set")
    if not to:
        raise EmailError("no recipient: set email.to or DIGEST_TO_EMAIL")

    payload = {
        "from": from_address,
        "to": [address.strip() for address in to.split(",") if address.strip()],
        "subject": subject,
        "html": html,
        "text": text,
    }

    try:
        resp = http.request(
            "POST",
            _ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json_body=payload,
            allow_status={400, 401, 403, 422},
        )
    except Exception as exc:  # noqa: BLE001
        raise EmailError(f"could not reach Resend: {exc}") from exc

    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("message", "")
        except Exception:  # noqa: BLE001 - the body may not be JSON
            detail = resp.text[:200]
        raise EmailError(f"Resend returned {resp.status_code}: {detail}")

    try:
        return resp.json().get("id", "")
    except Exception:  # noqa: BLE001
        return ""
