"""Google Gemini provider implementation.

Translates Anthropic-format messages/tools to Google GenAI SDK format.
"""

from __future__ import annotations

import itertools
import json
import logging
from typing import Any

from google import genai
from google.genai import types

from .base import LLMProvider
from .types import ContentBlock, LLMResponse, Usage

log = logging.getLogger("questline.provider.gemini")


class GeminiProvider(LLMProvider):
    """Wraps the Google GenAI SDK, translating Anthropic-format inputs."""

    def __init__(self) -> None:
        self.client = genai.Client()  # reads GEMINI_API_KEY / GOOGLE_API_KEY from env
        self._tool_use_counter = itertools.count()

    def create_message(
        self,
        *,
        model: str,
        max_tokens: int,
        system: list[dict] | None = None,
        tools: list[dict] | None = None,
        messages: list[dict[str, Any]],
        context_management: dict | None = None,  # ignored — no server-side compaction
    ) -> LLMResponse:
        config = types.GenerateContentConfig(max_output_tokens=max_tokens)

        if system:
            config.system_instruction = self._translate_system(system)
        if tools:
            config.tools = [self._translate_tools(tools)]
            config.automatic_function_calling = (
                types.AutomaticFunctionCallingConfig(disable=True)
            )

        contents = self._translate_messages(messages)
        response = self.client.models.generate_content(
            model=model, contents=contents, config=config,
        )
        return self._normalize(response)

    def create_triage_message(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list[dict[str, Any]],
    ) -> LLMResponse:
        config = types.GenerateContentConfig(max_output_tokens=max_tokens)
        contents = self._translate_messages(messages)
        response = self.client.models.generate_content(
            model=model, contents=contents, config=config,
        )
        return self._normalize(response)

    # ------------------------------------------------------------------
    # Translation: Anthropic → Gemini
    # ------------------------------------------------------------------

    @staticmethod
    def _translate_system(system_blocks: list[dict]) -> str:
        """Concatenate Anthropic system blocks into a single instruction string."""
        parts = []
        for block in system_blocks:
            text = block.get("text", "")
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    @staticmethod
    def _fix_schema_types(schema: dict) -> dict:
        """Recursively fix JSON Schema nullable types for Gemini.

        Gemini expects a single type string (e.g. "STRING"), not
        JSON Schema nullable arrays like ["string", "null"].
        """
        result = {}
        for k, v in schema.items():
            if k == "type" and isinstance(v, list):
                # ["string", "null"] → "string" (drop null)
                non_null = [t for t in v if t != "null"]
                result[k] = non_null[0] if non_null else "string"
            elif k == "properties" and isinstance(v, dict):
                result[k] = {
                    pk: GeminiProvider._fix_schema_types(pv)
                    if isinstance(pv, dict) else pv
                    for pk, pv in v.items()
                }
            elif isinstance(v, dict):
                result[k] = GeminiProvider._fix_schema_types(v)
            else:
                result[k] = v
        return result

    @staticmethod
    def _translate_tools(tool_defs: list[dict]) -> types.Tool:
        """Convert Anthropic tool definitions to a Gemini Tool object."""
        declarations = []
        for tool in tool_defs:
            decl = {
                "name": tool["name"],
                "description": tool.get("description", ""),
            }
            schema = tool.get("input_schema")
            if schema:
                cleaned = {
                    k: v for k, v in schema.items()
                    if k not in ("cache_control",)
                }
                decl["parameters"] = GeminiProvider._fix_schema_types(cleaned)
            declarations.append(decl)
        return types.Tool(function_declarations=declarations)

    def _translate_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[types.Content]:
        """Convert Anthropic-format messages to Gemini Content objects."""
        contents: list[types.Content] = []
        # Track tool_use id → name for resolving tool_result references
        tool_id_to_name: dict[str, str] = {}

        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            raw = msg.get("content")

            # Simple string content
            if isinstance(raw, str):
                contents.append(
                    types.Content(role=role, parts=[types.Part(text=raw)])
                )
                continue

            if not isinstance(raw, list):
                continue

            parts: list[types.Part] = []
            for block in raw:
                if not isinstance(block, dict):
                    continue

                btype = block.get("type")

                if btype == "text":
                    text = block.get("text", "")
                    if text:
                        parts.append(types.Part(text=text))

                elif btype == "tool_use":
                    tid = block.get("id", "")
                    name = block.get("name", "")
                    tool_id_to_name[tid] = name
                    ts = block.get("thought_signature")
                    if ts:
                        # Has thought_signature — safe to send as function_call.
                        # Decode from base64 string back to bytes.
                        import base64
                        ts_bytes = base64.b64decode(ts) if isinstance(ts, str) else ts
                        parts.append(types.Part(
                            function_call=types.FunctionCall(
                                name=name,
                                args=block.get("input") or {},
                            ),
                            thought_signature=ts_bytes,
                        ))
                    else:
                        # Old history (e.g. from Anthropic) — no thought_signature.
                        # Convert to text to avoid Gemini 3 rejection.
                        args_str = json.dumps(block.get("input") or {})
                        parts.append(types.Part(
                            text=f"[Called {name}({args_str})]"
                        ))
                        # Mark this tool_use_id as "textified" so matching
                        # tool_result also becomes text.
                        tool_id_to_name[tid] = f"__text__{name}"

                elif btype == "tool_result":
                    tid = block.get("tool_use_id", "")
                    name = tool_id_to_name.get(tid, "unknown")
                    raw_content = block.get("content", "")

                    if name.startswith("__text__"):
                        # Matching tool_use was textified — do the same
                        parts.append(types.Part(
                            text=f"[Result: {raw_content[:500]}]"
                        ))
                    else:
                        if isinstance(raw_content, str):
                            try:
                                response_data = json.loads(raw_content)
                            except (json.JSONDecodeError, TypeError):
                                response_data = {"result": raw_content}
                        elif isinstance(raw_content, dict):
                            response_data = raw_content
                        else:
                            response_data = {"result": str(raw_content)}

                        parts.append(types.Part.from_function_response(
                            name=name, response=response_data,
                        ))

                elif btype == "compaction":
                    # From a prior Anthropic session — pass through as text
                    text = block.get("text", "")
                    if text:
                        parts.append(types.Part(
                            text=f"[Previous context summary]: {text}"
                        ))

                # Silently skip unknown block types (cache_control, etc.)

            if parts:
                contents.append(types.Content(role=role, parts=parts))

        # Gemini requires alternating user/model turns.
        contents = self._merge_consecutive_roles(contents)
        # Must start with a user message.
        if contents and contents[0].role == "model":
            contents.insert(0, types.Content(
                role="user", parts=[types.Part(text="Continue.")]
            ))

        return contents

    @staticmethod
    def _merge_consecutive_roles(
        contents: list[types.Content],
    ) -> list[types.Content]:
        """Merge consecutive Content objects with the same role."""
        if not contents:
            return contents
        merged: list[types.Content] = [contents[0]]
        for c in contents[1:]:
            if c.role == merged[-1].role:
                merged[-1].parts.extend(c.parts)
            else:
                merged.append(c)
        return merged

    # ------------------------------------------------------------------
    # Normalization: Gemini → common types
    # ------------------------------------------------------------------

    def _normalize(self, response: Any) -> LLMResponse:
        """Convert Gemini response to normalized LLMResponse."""
        blocks: list[ContentBlock] = []
        has_tool_use = False

        if response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts or []:
                # Skip thinking/reasoning parts
                if getattr(part, "thought", False):
                    continue

                if part.text is not None:
                    blocks.append(ContentBlock(type="text", text=part.text))

                elif part.function_call is not None:
                    has_tool_use = True
                    fc = part.function_call
                    counter = next(self._tool_use_counter)
                    # Preserve thought_signature — Gemini 3 requires it
                    # to be echoed back on function_call parts in history.
                    # Store as base64 string for JSON serialization.
                    ts_raw = getattr(part, "thought_signature", None)
                    if isinstance(ts_raw, bytes):
                        import base64
                        ts = base64.b64encode(ts_raw).decode("ascii")
                    else:
                        ts = ts_raw
                    blocks.append(ContentBlock(
                        type="tool_use",
                        id=f"gemini_{fc.name}_{counter}",
                        name=fc.name,
                        input=dict(fc.args) if fc.args else {},
                        thought_signature=ts,
                    ))

        # Map finish_reason → normalized stop_reason
        if has_tool_use:
            stop_reason = "tool_use"
        elif response.candidates:
            fr = response.candidates[0].finish_reason
            reason_name = fr.name if hasattr(fr, "name") else str(fr)
            stop_reason = {
                "STOP": "end_turn",
                "MAX_TOKENS": "max_tokens",
                "SAFETY": "refusal",
                "RECITATION": "end_turn",
                "BLOCKLIST": "refusal",
                "PROHIBITED_CONTENT": "refusal",
            }.get(reason_name, "end_turn")
        else:
            stop_reason = "end_turn"

        # Usage metadata
        um = getattr(response, "usage_metadata", None)
        usage = Usage(
            input_tokens=getattr(um, "prompt_token_count", 0) or 0,
            output_tokens=getattr(um, "candidates_token_count", 0) or 0,
        )

        return LLMResponse(content=blocks, stop_reason=stop_reason, usage=usage)
