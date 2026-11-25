"""Compiled, side-effect-free generation graph. Dispatch is intentionally outside it."""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from bae_pilot.agents.schemas import (
    GraphResult,
    ReviewResult,
    StrategyResult,
    TalkResult,
    UnderstandingResult,
)
from bae_pilot.domain import AppError
from bae_pilot.models.base import complete_role
from bae_pilot.models.context import prepare_context


class TurnState(TypedDict, total=False):
    context: dict
    required_ids: set[int]
    deep: bool
    rewrites: int
    trace: list[str]
    understanding: dict
    strategy: dict
    talk: dict
    review: dict
    result: dict


class ConversationGraph:
    def __init__(self, provider, settings):
        self.provider = provider
        self.settings = settings
        graph = StateGraph(TurnState)
        graph.add_node("understanding", self._understanding)
        graph.add_node("strategy", self._strategy)
        graph.add_node("talk", self._talk)
        graph.add_node("review", self._review)
        graph.add_node("rewrite", self._rewrite)
        graph.add_node("validate", self._validate)
        graph.add_conditional_edges(START, lambda s: "understanding" if s["deep"] else "talk")
        graph.add_conditional_edges(
            "understanding",
            lambda s: "validate" if s["understanding"]["needs_handoff"] else "strategy",
        )
        graph.add_edge("strategy", "talk")
        graph.add_conditional_edges("talk", self._after_talk)
        graph.add_conditional_edges(
            "review",
            lambda s: (
                "rewrite"
                if s["review"]["verdict"] == "revise" and s["rewrites"] < 1
                else "validate"
            ),
        )
        graph.add_edge("rewrite", "talk")
        graph.add_edge("validate", END)
        self.compiled = graph.compile()

    async def _call(self, role, state, schema):
        if len(state["trace"]) >= self.settings.model_max_calls:
            raise AppError("model_budget_exhausted", 502)
        prepared = prepare_context(
            state["context"],
            role=role,
            state=state,
            required_ids=state["required_ids"],
            max_chars=self.settings.max_context_chars,
        )
        validated = await complete_role(
            self.provider, self.settings, role, prepared.payload, schema
        )
        if role == "understanding" and any(
            not set(item.source_event_ids) <= prepared.visible_source_ids
            for item in validated.facts
        ):
            raise AppError("invalid_fact_evidence", 502)
        return {
            role: validated.model_dump(),
            "trace": [*state["trace"], role],
        }

    async def _understanding(self, state):
        return {**await self._call("understanding", state, UnderstandingResult), "deep": True}

    async def _strategy(self, state):
        return await self._call("strategy", state, StrategyResult)

    async def _talk(self, state):
        return await self._call("talk", state, TalkResult)

    async def _review(self, state):
        return await self._call("review", state, ReviewResult)

    async def _rewrite(self, state):
        return {"rewrites": state["rewrites"] + 1}

    def _after_talk(self, state):
        talk = state["talk"]
        if talk["action"] != "reply":
            return "validate"
        if talk["requires_deep"]:
            return "validate" if state["deep"] else "understanding"
        return "review" if state["deep"] else "validate"

    async def _validate(self, state):
        talk = state.get("talk", {})
        status = talk.get("action", "handoff")
        reason = ""
        if state.get("understanding", {}).get("needs_handoff") or talk.get("requires_deep"):
            status, reason = "handoff", "needs_human_context"
        review = state.get("review", {})
        if review and review["verdict"] != "pass":
            status, reason = "handoff", "review_not_passed"
        reply = talk.get("reply", "").strip()
        if status == "reply" and (not reply or len(reply) > self.settings.max_reply_chars):
            status, reason = "handoff", "invalid_reply_length"
        result = GraphResult(
            status=status,
            reply=reply if status == "reply" else "",
            trace=[*state["trace"], "validate"],
            model_calls=len(state["trace"]),
            reason=reason,
        )
        return {"result": result.model_dump()}

    async def run(self, context: dict[str, Any]):
        if context.get("context_overflow"):
            raise AppError("context_too_large", 413)
        # Direct callers without a Store snapshot must not accidentally mark all
        # their input optional. Runtime always supplies the exact queued set.
        required_ids = set(
            context.get("queued_event_ids", [m["record_id"] for m in context["messages"]])
        )
        if context.get("outbound_anchor_id") is not None:
            required_ids.add(context["outbound_anchor_id"])
        peer = [m for m in context["messages"] if m["direction"] == "inbound"]
        greeting = peer[-1]["text"].strip() if peer else ""
        deep = greeting not in {"你好", "嗨", "早安", "晚安", "在吗", "谢谢"}
        # Never inherit LangSmith tracing from the shell: fake/offline runs must stay local.
        with tracing_context(enabled=False):
            state = await self.compiled.ainvoke(
                {
                    "context": context,
                    "required_ids": required_ids,
                    "deep": deep,
                    "rewrites": 0,
                    "trace": [],
                },
                config={"recursion_limit": 20, "callbacks": []},
            )
        return GraphResult.model_validate(state["result"])
