"""Model-boundary contracts: exact input, evidence, and bounded whole-message folding."""

import json
from copy import deepcopy

import pytest

from bae_pilot.agents.graph import ConversationGraph
from bae_pilot.agents.schemas import ReviewResult, TalkResult, UnderstandingResult
from bae_pilot.config import Settings
from bae_pilot.domain import AppError
from tests.helpers import RecordingProvider


def raw(record_id, text, *, direction="inbound"):
    return {
        "record_id": record_id,
        "direction": direction,
        "origin": "peer" if direction == "inbound" else "bot",
        "message_type": "text",
        "text": text,
    }


def inputs(messages, **extra):
    return {
        "messages": messages,
        "memories": [],
        "style": "自然",
        "history_window": 40,
        "memory_version": 0,
        **extra,
    }


def pack(context, *, required_ids, max_chars=1600, role="talk", state=None):
    # Import inside the test call so a missing feature is a test failure, not collection failure.
    from bae_pilot.models.context import prepare_context

    return prepare_context(
        context, role=role, state=state or {}, required_ids=required_ids, max_chars=max_chars
    )


def test_small_input_keeps_verbatim_messages_and_projects_private_fields():
    original = inputs(
        [{**raw(1, '中😀 "quoted"\\\n'), "sender_id": "private-person"}, raw(2, "谢谢")],
        channel="private-channel",
        account_id="private-account",
        run_id="private-run",
        queued_event_ids=[2],
    )
    before = deepcopy(original)
    prepared = pack(original, required_ids={2}, max_chars=2000)
    assert prepared.payload["messages"] == [raw(1, '中😀 "quoted"\\\n'), raw(2, "谢谢")]
    assert prepared.visible_source_ids == {1, 2}
    assert prepared.payload["coverage"]["folded_messages"] == 0
    assert not {"channel", "account_id", "run_id", "queued_event_ids"} & prepared.payload.keys()
    assert original == before
    assert prepared.stats["final_chars"] == len(json.dumps(prepared.payload, ensure_ascii=False))


def test_pressure_folds_oldest_whole_messages_but_preserves_all_memories():
    memories = [
        {"kind": kind, "content": "明确要求不使用某个称呼", "source_event_ids": [99]}
        for kind in ("statement", "summary", "inference")
    ]
    original = inputs([raw(i, "旧" * 500) for i in (1, 2, 3)] + [raw(4, "谢谢")], memories=memories)
    before = deepcopy(original)
    prepared = pack(original, required_ids={4}, max_chars=1800)
    assert [m["record_id"] for m in prepared.payload["messages"]] == [3, 4]
    assert prepared.payload["messages"][0]["text"] == "旧" * 500
    assert prepared.payload["memories"] == memories
    assert prepared.payload["coverage"]["folded_messages"] == 2
    assert prepared.payload["coverage"]["folded_id_bounds"] == [1, 2]
    assert prepared.stats["original_chars"] == len(
        json.dumps(
            {
                **original,
                "coverage": {
                    "selection": "recent_window+queued+latest_outbound",
                    "candidate_messages": 4,
                    "retained_messages": 4,
                    "folded_messages": 0,
                    "folded_id_bounds": None,
                },
            },
            ensure_ascii=False,
        )
    )
    assert len(json.dumps(prepared.payload, ensure_ascii=False)) <= 1440
    assert original == before


def test_necessary_skeleton_above_target_keeps_history_that_still_fits_hard_limit():
    original = inputs([raw(1, "旧" * 500), raw(2, "前一句"), raw(3, "当" * 1200)])
    prepared = pack(original, required_ids={3}, max_chars=1800)
    assert [m["record_id"] for m in prepared.payload["messages"]] == [2, 3]
    assert 1440 < len(json.dumps(prepared.payload, ensure_ascii=False)) <= 1800


def test_fact_sources_remain_visible_to_later_roles_even_when_oldest():
    original = inputs([raw(i, "旧" * 500) for i in (1, 2, 3)] + [raw(4, "当前")])
    state = {
        "understanding": UnderstandingResult(
            facts=[{"statement": "对方此前的陈述", "source_event_ids": [1]}]
        ).model_dump()
    }
    prepared = pack(original, role="strategy", state=state, required_ids={4}, max_chars=1800)
    assert {1, 4} <= prepared.visible_source_ids
    assert prepared.payload["understanding"] == state["understanding"]
    assert prepared.payload["messages"][0] == raw(1, "旧" * 500)
    assert len(json.dumps(prepared.payload, ensure_ascii=False)) <= 1800


def test_required_context_cannot_be_truncated_to_fit():
    with pytest.raises(AppError, match="context_too_large"):
        pack(inputs([raw(1, "当前" * 1000)]), required_ids={1}, max_chars=1000)


def test_missing_required_source_cannot_be_replaced_with_a_pointer():
    with pytest.raises(AppError, match="invalid_context_sources"):
        pack(inputs([raw(2, "当前")]), required_ids={1, 2})


def test_hard_budget_includes_coverage_metadata_and_accepts_exact_boundary():
    context = inputs([raw(1, '"\\\n中😀' * 100)])
    prepared = pack(context, required_ids={1}, max_chars=10000)
    actual_chars = len(json.dumps(prepared.payload, ensure_ascii=False))
    assert pack(context, required_ids={1}, max_chars=actual_chars).payload == prepared.payload
    with pytest.raises(AppError, match="context_too_large"):
        pack(context, required_ids={1}, max_chars=actual_chars - 1)


@pytest.mark.parametrize("text", ["中", "😀", '"\\\n\t\b', "é", " "])
@pytest.mark.parametrize("count", [0, 1, 201])
def test_preflight_lower_bound_never_exceeds_actual_message_array(text, count):
    from bae_pilot.models.context import messages_lower_bound

    messages = [raw(10**18 + i, text) for i in range(count)]
    actual_chars = len(json.dumps(messages, ensure_ascii=False))
    assert messages_lower_bound(count, count * len(text)) <= actual_chars


async def test_short_upgrade_does_not_leak_old_draft_to_specialists_or_new_talk():
    provider = RecordingProvider(talk=[TalkResult(reply="old-private-draft", requires_deep=True)])
    result = await ConversationGraph(provider, Settings()).run(
        inputs(
            [{**raw(1, "你好"), "sender_id": "private-person"}],
            queued_event_ids=[1],
            channel="private-channel",
            account_id="private-account",
            run_id="private-run",
        )
    )
    assert "private-person" not in json.dumps(provider.calls, ensure_ascii=False)
    assert all(
        not {"channel", "account_id", "run_id", "queued_event_ids"} & ctx.keys()
        for _, ctx in provider.calls
    )
    assert result.trace == ["talk", "understanding", "strategy", "talk", "review", "validate"]
    assert result.reply != "old-private-draft"
    assert [
        set(ctx) & {"understanding", "strategy", "talk", "review"} for _, ctx in provider.calls
    ] == [
        set(),
        set(),
        {"understanding"},
        {"understanding", "strategy"},
        {"understanding", "strategy", "talk"},
    ]
    assert "old-private-draft" not in json.dumps(provider.calls[1:], ensure_ascii=False)


async def test_rewrite_uses_feedback_but_review_never_gets_its_previous_verdict():
    provider = RecordingProvider(
        review=[ReviewResult(verdict="revise", reason="请先澄清"), ReviewResult(verdict="pass")]
    )
    result = await ConversationGraph(provider, Settings()).run(
        inputs([raw(1, "我们需要谈谈")], queued_event_ids=[1])
    )
    assert result.status == "reply" and result.model_calls == 6
    rewrite_request = provider.calls[4][1]
    assert rewrite_request["review"]["reason"] == "请先澄清"
    assert "talk" in rewrite_request
    assert "review" not in provider.calls[5][1]


@pytest.mark.parametrize("source_id", [1, 99, 100000])
async def test_fact_evidence_is_checked_against_packed_input_not_full_candidates(source_id):
    provider = RecordingProvider(
        understanding=[
            UnderstandingResult(
                facts=[{"statement": "未展示原文的信息", "source_event_ids": [source_id]}]
            )
        ]
    )
    graph = ConversationGraph(provider, Settings(max_context_chars=1200))
    with pytest.raises(AppError, match="invalid_fact_evidence"):
        await graph.run(
            inputs(
                [raw(1, "旧" * 3000), raw(2, "需要谈谈")],
                queued_event_ids=[2],
                memories=[{"kind": "summary", "content": "历史摘要", "source_event_ids": [99]}],
            )
        )
    assert [role for role, _ in provider.calls] == ["understanding"]
    assert [m["record_id"] for m in provider.calls[0][1]["messages"]] == [2]


async def test_reply_anchor_cannot_be_folded_to_make_an_oversized_turn_fit():
    provider = RecordingProvider()
    graph = ConversationGraph(provider, Settings(max_context_chars=1800))
    with pytest.raises(AppError, match="context_too_large"):
        await graph.run(
            inputs(
                [raw(1, "已发" * 600, direction="outbound"), raw(2, "当" * 600)],
                queued_event_ids=[2],
                outbound_anchor_id=1,
            )
        )
    assert provider.calls == []


def test_context_statistics_do_not_log_sensitive_input(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="bae_pilot.models.context"):
        prepared = pack(
            inputs([raw(1, "private-message-sentinel")], style="private-style-sentinel"),
            required_ids={1},
            max_chars=2000,
        )
    record = caplog.records[-1]
    assert record.final_chars == len(json.dumps(prepared.payload, ensure_ascii=False))
    assert record.retained_messages == 1 and record.folded_messages == 0
    assert "private-message-sentinel" not in str(record.__dict__)
    assert "private-style-sentinel" not in str(record.__dict__)


async def test_oversized_required_input_never_reaches_provider():
    provider = RecordingProvider()
    graph = ConversationGraph(provider, Settings(max_context_chars=1000))
    with pytest.raises(AppError, match="context_too_large"):
        await graph.run(inputs([raw(1, "当前" * 1000)], queued_event_ids=[1]))
    assert provider.calls == []
