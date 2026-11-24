"""Opt-in official Anthropic SDK adapter; tests replace only its HTTP transport."""

import json

from pydantic import ValidationError

from bae_pilot.agents.prompts import PROMPTS
from bae_pilot.domain import AppError


class AnthropicProvider:
    def __init__(self, settings, *, client=None):
        try:
            import anthropic
        except ImportError as exc:
            raise AppError("anthropic_dependency_missing", 503) from exc
        self.sdk = anthropic
        self.settings = settings
        # Default SDK credential resolution supports environment or an explicit CLI profile.
        self.client = client or anthropic.AsyncAnthropic(
            timeout=settings.model_timeout_s, max_retries=1
        )

    async def complete(self, role, context, schema):
        sdk = self.sdk
        options = {}
        if self.settings.refusal_fallback:
            options = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            async with self.client.beta.messages.stream(
                model=self.settings.model,
                max_tokens=self.settings.model_max_tokens,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": sdk.transform_schema(schema)},
                },
                system=PROMPTS[role],
                messages=[
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"untrusted_conversation_data": context},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    }
                ],
                **options,
            ) as stream:
                message = await stream.get_final_message()
            if message.stop_reason == "refusal":
                raise AppError("model_refused", 502)
            if message.stop_reason != "end_turn":
                raise AppError("model_incomplete", 502)
            text = "".join(block.text for block in message.content if block.type == "text")
            return schema.model_validate_json(text)
        except sdk.NotFoundError as exc:
            raise AppError("model_unavailable", 502) from exc
        except sdk.RateLimitError as exc:
            raise AppError("model_rate_limited", 503) from exc
        except (sdk.AuthenticationError, sdk.PermissionDeniedError) as exc:
            raise AppError("model_credentials_rejected", 503) from exc
        except sdk.APIStatusError as exc:
            raise AppError(
                "model_service_unavailable" if exc.status_code >= 500 else "model_request_rejected",
                503 if exc.status_code >= 500 else 502,
            ) from exc
        except sdk.APIConnectionError as exc:
            raise AppError("model_connection_failed", 503) from exc
        except (sdk.APIResponseValidationError, ValidationError, json.JSONDecodeError) as exc:
            raise AppError("invalid_model_output", 502) from exc

    async def close(self):
        await self.client.close()
