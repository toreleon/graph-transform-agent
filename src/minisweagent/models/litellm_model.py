import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import litellm
from pydantic import BaseModel

from minisweagent.models import GLOBAL_MODEL_STATS
from minisweagent.models.utils.actions_toolcall import (
    BASH_TOOL,
    format_toolcall_observation_messages,
    parse_toolcall_actions,
)
from minisweagent.models.utils.anthropic_utils import _reorder_anthropic_thinking_blocks
from minisweagent.models.utils.cache_control import set_cache_control
from minisweagent.models.utils.openai_multimodal import expand_multimodal_content
from minisweagent.models.utils.retry import retry

logger = logging.getLogger("litellm_model")

# Fields from model_dump() that are safe to pass through to the API.
# Litellm-internal fields (e.g., provider_specific_fields) are stripped.
_KNOWN_MESSAGE_FIELDS = {
    "role", "content", "name", "tool_calls", "tool_call_id", "function_call",
    "refusal", "audio", "annotations",
}

# Roles accepted by the OpenAI chat completions API.
# Messages with other roles (e.g., "exit") are internal to mini-swe-agent
# and must be filtered before sending to the API.  The OpenAI SDK's Pydantic
# Union discriminator serializes unrecognised roles as ``null``, which causes
# "Invalid type for 'messages[N]': expected an object, but got null" errors.
_VALID_API_ROLES = {"system", "user", "assistant", "tool", "function", "developer"}


def _diagnose_null_messages(messages: list) -> None:
    """Log diagnostic info when a null-message BadRequestError occurs."""
    import json as _json

    logger.error("=== NULL MESSAGE DIAGNOSTIC ===")
    logger.error("Total messages: %d", len(messages))
    for i, msg in enumerate(messages):
        if msg is None:
            logger.error("  messages[%d] = NULL", i)
        elif not isinstance(msg, dict):
            logger.error("  messages[%d] = type=%s value=%r", i, type(msg).__name__, msg)
        else:
            role = msg.get("role", "<missing>")
            has_content = "content" in msg
            content_val = msg.get("content")
            content_repr = repr(content_val)[:80] if content_val is not None else "null"
            keys = sorted(msg.keys())
            logger.error(
                "  messages[%d] = role=%s, has_content=%s, content=%s, keys=%s",
                i, role, has_content, content_repr, keys,
            )
    # Also dump the raw JSON to a temp file for inspection
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix="_null_msg_debug.json", delete=False, prefix="litellm_") as f:
            _json.dump(messages, f, indent=2, default=str)
            logger.error("Full messages dumped to: %s", f.name)
    except Exception:
        pass
    logger.error("=== END DIAGNOSTIC ===")


def _sanitize_message(msg: dict) -> dict:
    """Clean a message dict for API submission.

    - Strips the ``extra`` key (internal to mini-swe-agent).
    - Strips unknown/litellm-internal keys (e.g., ``provider_specific_fields``).
    - Removes keys whose value is ``None`` **except** ``content`` (which is
      legitimately ``None`` for assistant messages that only carry tool_calls).
    - Deep-sanitizes ``tool_calls`` to remove Pydantic artifacts.
    """
    out: dict = {}
    for k, v in msg.items():
        if k == "extra":
            continue
        if k not in _KNOWN_MESSAGE_FIELDS:
            continue
        # Keep content even when None (valid for assistant tool_call messages)
        if v is None and k != "content":
            continue
        out[k] = v
    # Deep-sanitize tool_calls: strip None values and Pydantic internals
    if "tool_calls" in out and isinstance(out["tool_calls"], list):
        out["tool_calls"] = _sanitize_tool_calls(out["tool_calls"])
    return out


def _sanitize_tool_calls(tool_calls: list) -> list:
    """Deep-clean tool_calls list for API submission.

    Ensures each tool call is a plain dict with only known fields,
    preventing Pydantic serialization artifacts from causing API errors.
    """
    _KNOWN_TC_FIELDS = {"id", "type", "function", "index"}
    _KNOWN_FN_FIELDS = {"name", "arguments"}
    cleaned = []
    for tc in tool_calls:
        if tc is None:
            continue
        if not isinstance(tc, dict):
            # Convert Pydantic models to dicts
            tc = tc.model_dump() if hasattr(tc, "model_dump") else dict(tc)
        out = {k: v for k, v in tc.items() if k in _KNOWN_TC_FIELDS and v is not None}
        # Ensure function sub-dict is also clean
        fn = out.get("function")
        if isinstance(fn, dict):
            out["function"] = {k: v for k, v in fn.items() if k in _KNOWN_FN_FIELDS}
        cleaned.append(out)
    return cleaned


class LitellmModelConfig(BaseModel):
    model_name: str
    """Model name. Highly recommended to include the provider in the model name, e.g., `anthropic/claude-sonnet-4-5-20250929`."""
    model_kwargs: dict[str, Any] = {}
    """Additional arguments passed to the API."""
    litellm_model_registry: Path | str | None = os.getenv("LITELLM_MODEL_REGISTRY_PATH")
    """Model registry for cost tracking and model metadata. See the local model guide (https://mini-swe-agent.com/latest/models/local_models/) for more details."""
    set_cache_control: Literal["default_end"] | None = None
    """Set explicit cache control markers, for example for Anthropic models"""
    cost_tracking: Literal["default", "ignore_errors"] = os.getenv("MSWEA_COST_TRACKING", "default")
    """Cost tracking mode for this model. Can be "default" or "ignore_errors" (ignore errors/missing cost info)"""
    format_error_template: str = "{{ error }}"
    """Template used when the LM's output is not in the expected format."""
    observation_template: str = (
        "{% if output.exception_info %}<exception>{{output.exception_info}}</exception>\n{% endif %}"
        "<returncode>{{output.returncode}}</returncode>\n<output>\n{{output.output}}</output>"
    )
    """Template used to render the observation after executing an action."""
    multimodal_regex: str = ""
    """Regex to extract multimodal content. Empty string disables multimodal processing."""


class LitellmModel:
    abort_exceptions: list[type[Exception]] = [
        litellm.exceptions.UnsupportedParamsError,
        litellm.exceptions.NotFoundError,
        litellm.exceptions.PermissionDeniedError,
        litellm.exceptions.ContextWindowExceededError,
        litellm.exceptions.AuthenticationError,
        litellm.exceptions.BadRequestError,
        KeyboardInterrupt,
    ]

    def __init__(self, *, config_class: Callable = LitellmModelConfig, **kwargs):
        self.config = config_class(**kwargs)
        if self.config.litellm_model_registry and Path(self.config.litellm_model_registry).is_file():
            litellm.utils.register_model(json.loads(Path(self.config.litellm_model_registry).read_text()))

    def _query(self, messages: list[dict[str, str]], **kwargs):
        # Final defense: filter any null entries that slipped through
        n_before = len(messages)
        messages = [m for m in messages if m is not None and isinstance(m, dict)]
        if len(messages) != n_before:
            logger.warning(
                "Filtered %d invalid entries from %d messages before API call",
                n_before - len(messages), n_before,
            )
        try:
            return litellm.completion(
                model=self.config.model_name,
                messages=messages,
                tools=[BASH_TOOL],
                **(self.config.model_kwargs | kwargs),
            )
        except litellm.exceptions.AuthenticationError as e:
            e.message += " You can permanently set your API key with `mini-extra config set KEY VALUE`."
            raise e
        except litellm.exceptions.BadRequestError as e:
            if "got null" in str(e):
                _diagnose_null_messages(messages)
            raise

    def _prepare_messages_for_api(self, messages: list[dict]) -> list[dict]:
        prepared = []
        for msg in messages:
            if msg is None:
                continue
            # Skip messages with non-API roles (e.g., "exit") — the OpenAI SDK
            # serializes unrecognised roles as null, causing API errors.
            if msg.get("role") not in _VALID_API_ROLES:
                continue
            prepared.append(_sanitize_message(msg))
        prepared = _reorder_anthropic_thinking_blocks(prepared)
        return set_cache_control(prepared, mode=self.config.set_cache_control)

    def query(self, messages: list[dict[str, str]], **kwargs) -> dict:
        for attempt in retry(logger=logger, abort_exceptions=self.abort_exceptions):
            with attempt:
                response = self._query(self._prepare_messages_for_api(messages), **kwargs)
        cost_output = self._calculate_cost(response)
        GLOBAL_MODEL_STATS.add(cost_output["cost"])
        message = response.choices[0].message.model_dump()
        message["extra"] = {
            "actions": self._parse_actions(response),
            "response": response.model_dump(),
            **cost_output,
            "timestamp": time.time(),
        }
        return message

    def _calculate_cost(self, response) -> dict[str, float]:
        try:
            cost = litellm.cost_calculator.completion_cost(response, model=self.config.model_name)
            if cost <= 0.0:
                raise ValueError(f"Cost must be > 0.0, got {cost}")
        except Exception as e:
            cost = 0.0
            if self.config.cost_tracking != "ignore_errors":
                msg = (
                    f"Error calculating cost for model {self.config.model_name}: {e}, perhaps it's not registered? "
                    "You can ignore this issue from your config file with cost_tracking: 'ignore_errors' or "
                    "globally with export MSWEA_COST_TRACKING='ignore_errors'. "
                    "Alternatively check the 'Cost tracking' section in the documentation at "
                    "https://klieret.short.gy/mini-local-models. "
                    " Still stuck? Please open a github issue at https://github.com/SWE-agent/mini-swe-agent/issues/new/choose!"
                )
                logger.critical(msg)
                raise RuntimeError(msg) from e
        return {"cost": cost}

    def _parse_actions(self, response) -> list[dict]:
        """Parse tool calls from the response. Raises FormatError if unknown tool."""
        tool_calls = response.choices[0].message.tool_calls or []
        return parse_toolcall_actions(tool_calls, format_error_template=self.config.format_error_template)

    def format_message(self, **kwargs) -> dict:
        return expand_multimodal_content(kwargs, pattern=self.config.multimodal_regex)

    def format_observation_messages(
        self, message: dict, outputs: list[dict], template_vars: dict | None = None
    ) -> list[dict]:
        """Format execution outputs into tool result messages."""
        actions = message.get("extra", {}).get("actions", [])
        return format_toolcall_observation_messages(
            actions=actions,
            outputs=outputs,
            observation_template=self.config.observation_template,
            template_vars=template_vars,
            multimodal_regex=self.config.multimodal_regex,
        )

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return self.config.model_dump()

    def serialize(self) -> dict:
        return {
            "info": {
                "config": {
                    "model": self.config.model_dump(mode="json"),
                    "model_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                },
            }
        }
