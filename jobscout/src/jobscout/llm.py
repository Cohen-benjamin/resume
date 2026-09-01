"""Claude access: one client, one place that knows the request shape.

Two call patterns are used in this app:

* **Single structured call** -- profile extraction. One request, strict JSON out.
* **Batch** -- scoring and briefs. One request per job through the Message
  Batches API at half price, sharing a cached prompt prefix so the resume and
  rubric are billed once for the whole batch rather than once per job.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: Batch polling. Most batches finish well inside this; the API's own ceiling is
#: 24h, but a job digest that took that long would be useless, so we give up and
#: fall back rather than block a scheduled run indefinitely.
_POLL_INTERVAL_SECONDS = 15
_POLL_TIMEOUT_SECONDS = 45 * 60


class LLMUnavailable(RuntimeError):
    """No usable Claude credentials, or the SDK is not installed."""


@dataclass
class BatchRequest:
    custom_id: str
    system: list[dict[str, Any]]
    user: str
    schema: dict[str, Any] | None = None
    max_tokens: int = 2000


def make_client(api_key: str = ""):
    """Build an Anthropic client.

    An unset key is not the same as no credentials -- the SDK also resolves
    OAuth profiles on disk -- so an empty key still gets a zero-arg client and
    the SDK decides.
    """
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise LLMUnavailable("the anthropic package is not installed") from exc

    try:
        return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    except Exception as exc:
        raise LLMUnavailable(f"could not construct Anthropic client: {exc}") from exc


def structured_call(
    client: Any,
    *,
    model: str,
    system: str | list[dict[str, Any]],
    user: str,
    schema: dict[str, Any],
    max_tokens: int = 8000,
) -> dict[str, Any]:
    """One request, guaranteed-shape JSON back."""
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    if getattr(resp, "stop_reason", None) == "refusal":
        raise LLMUnavailable("request was declined by the safety classifier")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(text)


def run_batch(
    client: Any,
    *,
    model: str,
    requests: Sequence[BatchRequest],
    poll_timeout: int = _POLL_TIMEOUT_SECONDS,
    on_progress: Any = None,
) -> dict[str, dict[str, Any]]:
    """Submit a batch and collect results keyed by `custom_id`.

    Results come back in arbitrary order, so they are keyed rather than zipped.
    A request that errors is omitted from the result rather than raising: one
    bad job description should not cost you the whole digest.
    """
    if not requests:
        return {}

    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    payload = []
    for req in requests:
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens,
            "system": req.system,
            "messages": [{"role": "user", "content": req.user}],
        }
        if req.schema:
            params["output_config"] = {"format": {"type": "json_schema", "schema": req.schema}}
        payload.append(
            Request(custom_id=req.custom_id, params=MessageCreateParamsNonStreaming(**params))
        )

    batch = client.messages.batches.create(requests=payload)
    log.info("submitted batch %s with %d requests", batch.id, len(payload))

    deadline = time.time() + poll_timeout
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        if time.time() > deadline:
            # Cancel rather than abandon, so we are not billed for work whose
            # results we will never read.
            try:
                client.messages.batches.cancel(batch.id)
            except Exception:  # noqa: BLE001 - cancellation is best-effort
                log.warning("could not cancel batch %s", batch.id)
            raise TimeoutError(f"batch {batch.id} did not finish within {poll_timeout}s")
        if on_progress:
            on_progress(batch)
        time.sleep(_POLL_INTERVAL_SECONDS)

    results: dict[str, dict[str, Any]] = {}
    for result in client.messages.batches.results(batch.id):
        if result.result.type != "succeeded":
            log.warning("batch item %s: %s", result.custom_id, result.result.type)
            continue
        text = next((b.text for b in result.result.message.content if b.type == "text"), "")
        try:
            results[result.custom_id] = json.loads(text)
        except json.JSONDecodeError:
            log.warning("batch item %s returned unparseable JSON", result.custom_id)
    return results


def cached_system(stable: str, label: str = "") -> list[dict[str, Any]]:
    """A system prompt whose whole body is cached.

    Everything that varies per job goes in the user turn instead, so this prefix
    stays byte-identical across a batch and is billed once.
    """
    block: dict[str, Any] = {
        "type": "text",
        "text": stable,
        "cache_control": {"type": "ephemeral"},
    }
    if label:
        block["text"] = f"{label}\n\n{stable}"
    return [block]
