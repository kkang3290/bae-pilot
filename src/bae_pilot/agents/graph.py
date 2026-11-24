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
from bae_pilot.models.base import complete_role, model_messages


class TurnState(TypedDict, total=False):
    context: dict
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
        context = {
            **state["context"],
            **{
                name: state[name]
                for name in ("understanding", "strategy", "talk", "review")
                if name in state
            },
        }
        validated = await complete_role(self.provider, self.settings, role, context, schema)
        return {
            role: validated.model_dump(),
            "trace": [*state["trace"], role],
        }

    async def _understanding(self, state):
        result = await self._call("understanding", state, UnderstandingResult)
        valid_ids = {m["record_id"] for m in state["context"]["messages"]}
        if any(
            not set(item["source_event_ids"]) <= valid_ids
            for item in result["understanding"]["facts"]
        ):
            raise AppError("invalid_fact_evidence", 502)
        return {**result, "deep": True}

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
        # Do not send channel/account/contact identifiers or internal run metadata to a model.
        context = {
            "messages": model_messages(context["messages"]),
            "memories": [
                {k: m[k] for k in ("kind", "content", "source_event_ids")}
                for m in context.get("memories", [])
            ],
            "style": context.get("style", ""),
            "history_window": context.get("history_window"),
            "memory_version": context.get("memory_version", 0),
        }
        peer = [m for m in context["messages"] if m["direction"] == "inbound"]
        greeting = peer[-1]["text"].strip() if peer else ""
        deep = greeting not in {"你好", "嗨", "早安", "晚安", "在吗", "谢谢"}
        # Never inherit LangSmith tracing from the shell: fake/offline runs must stay local.
        with tracing_context(enabled=False):
            state = await self.compiled.ainvoke(
                {"context": context, "deep": deep, "rewrites": 0, "trace": []},
                config={"recursion_limit": 20, "callbacks": []},
            )
        return GraphResult.model_validate(state["result"])
