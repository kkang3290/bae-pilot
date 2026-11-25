"""Pure request projection and whole-message folding; original events stay in PostgreSQL."""

import json
import logging
from copy import deepcopy
from dataclasses import dataclass

from bae_pilot.domain import AppError

logger = logging.getLogger(__name__)

MESSAGE_FIELDS = ("record_id", "direction", "origin", "message_type", "text")
ROLE_INPUTS = {
    "understanding": (),
    "strategy": ("understanding",),
    "talk": ("understanding", "strategy"),
    "review": ("understanding", "strategy", "talk"),
}


def context_chars(value):
    """Compatibility budget: JSON characters, not tokens or the complete API envelope."""
    return len(json.dumps(value, ensure_ascii=False))


def model_messages(messages):
    """Shared explicit allowlist for foreground and memory model inputs."""
    return [{field: message[field] for field in MESSAGE_FIELDS} for message in messages]


def messages_lower_bound(count, text_chars):
    """Lower bound for a JSON message array, never a proof the request will fit.

    Positive BIGINT IDs are at least as long as 0; enum strings are at least as
    long as empty strings. Each decoded text character consumes at least one
    JSON character (escaping can only increase that). Include [] and ', ' too.
    """
    minimum_message = {field: 0 if field == "record_id" else "" for field in MESSAGE_FIELDS}
    return 2 + count * context_chars(minimum_message) + 2 * max(count - 1, 0) + text_chars


@dataclass(frozen=True)
class PreparedContext:
    payload: dict
    visible_source_ids: frozenset[int]
    stats: dict[str, int]


def prepare_context(context, *, role, state, required_ids, max_chars):
    """Keep all required data; only optional history can be folded, oldest first."""
    messages = model_messages(context["messages"])
    ids = {message["record_id"] for message in messages}
    required = set(required_ids)
    if len(ids) != len(messages) or not required <= ids:
        raise AppError("invalid_context_sources", 502)

    dependencies = ROLE_INPUTS[role]
    if role == "talk" and state.get("rewrites", 0):
        dependencies += ("talk", "review")
    results = deepcopy({name: state[name] for name in dependencies if name in state})
    fact_sources = {
        source
        for fact in results.get("understanding", {}).get("facts", [])
        for source in fact["source_event_ids"]
    }
    if not fact_sources <= ids:
        raise AppError("invalid_fact_evidence", 502)
    required |= fact_sources

    def coverage(retained, folded, bounds):
        return {
            "selection": "recent_window+queued+latest_outbound",
            "candidate_messages": len(messages),
            "retained_messages": retained,
            "folded_messages": folded,
            "folded_id_bounds": bounds,
        }

    initial_coverage = coverage(len(messages), 0, None)
    payload = {
        "messages": [],
        "memories": deepcopy(
            [
                {field: memory[field] for field in ("kind", "content", "source_event_ids")}
                for memory in context.get("memories", [])
            ]
        ),
        "style": context.get("style", ""),
        "history_window": context.get("history_window"),
        "memory_version": context.get("memory_version", 0),
        **results,
        "coverage": initial_coverage,
    }
    # Measure the empty-message skeleton once, then add exact element and ', '
    # separator contributions without serializing every message body twice.
    base_chars = context_chars(payload) - 2
    sizes = [context_chars(message) for message in messages]
    original_chars = base_chars + sum(sizes) + 2 * max(len(messages) - 1, 0) + 2
    original_coverage_chars = context_chars(initial_coverage)
    payload["messages"] = messages

    def estimated_chars(retained, content_chars, metadata):
        return (
            base_chars
            + content_chars
            + 2 * max(retained - 1, 0)
            + 2
            + context_chars(metadata)
            - original_coverage_chars
        )

    optional = sorted(
        (message["record_id"], index)
        for index, message in enumerate(messages)
        if message["record_id"] not in required
    )
    required_count = len(required)
    required_sum = sum(
        size
        for message, size in zip(messages, sizes, strict=True)
        if message["record_id"] in required
    )
    required_coverage = coverage(
        required_count,
        len(optional),
        [optional[0][0], optional[-1][0]] if optional else None,
    )
    required_chars = estimated_chars(required_count, required_sum, required_coverage)
    if required_chars > max_chars:
        logger.warning(
            "context_overflow",
            extra={
                "role": role,
                "original_chars": original_chars,
                "required_chars": required_chars,
                "max_context_chars": max_chars,
            },
        )
        raise AppError("context_too_large", 413)

    folded_ids = set()
    retained_sum = sum(sizes)
    final_estimate = original_chars
    if original_chars > max_chars * 9 // 10:
        target = max_chars * 8 // 10
        if required_chars > target:
            target = max_chars
        low = high = None
        for source_id, index in optional:
            if final_estimate <= target:
                break
            folded_ids.add(source_id)
            retained_sum -= sizes[index]
            low = source_id if low is None else low
            high = source_id
            payload["coverage"] = coverage(
                len(messages) - len(folded_ids), len(folded_ids), [low, high]
            )
            final_estimate = estimated_chars(
                len(messages) - len(folded_ids), retained_sum, payload["coverage"]
            )
    if folded_ids:
        payload["messages"] = [
            message for message in messages if message["record_id"] not in folded_ids
        ]
    final_chars = context_chars(payload)
    if final_chars > max_chars:
        raise AppError("context_too_large", 413)
    stats = {
        "original_chars": original_chars,
        "final_chars": final_chars,
        "retained_messages": len(payload["messages"]),
        "folded_messages": len(folded_ids),
        "retained_memories": len(payload["memories"]),
    }
    logger.info("context_prepared", extra={"role": role, **stats})
    return PreparedContext(
        payload=payload,
        visible_source_ids=frozenset(message["record_id"] for message in payload["messages"]),
        stats=stats,
    )
