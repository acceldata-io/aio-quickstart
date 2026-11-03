"""The chat model the agents talk to.

Every agent uses `FixtureChatModel`: a stand-in that never calls a provider. It
is a real LangChain chat model, so it is traced exactly like a real one, but its
name is `fixture-model` so nobody mistakes its output for a genuine model's.

Two things make it behave like a real tool-calling model, which redaction needs
something to act on:

- `bind_tools()` works, so the tool definitions are sent with each call and
  recorded on the model span (`gen_ai.tool.definitions`).
- `respond` can return tool calls as well as text, so a tool call shows up in
  the model's output messages, with its arguments, before the tool runs.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool

FIXTURE_MODEL_NAME = "fixture-model"

# What `respond` returns: the reply text, and any tool calls as
# {"name": ..., "args": {...}} dicts.
Reply = tuple[str, list[dict]]


def count_tokens(text: str) -> int:
    """A rough token count: about 4 characters per token, as for English text."""
    return max(1, len(text) // 4)


class FixtureChatModel(BaseChatModel):
    """A simulated chat model that answers from a template instead of an LLM.

    The agents send it the same messages a real model would get. `respond`
    reads them and decides the reply: plain text, tool calls, or both.
    """

    model_name: str = FIXTURE_MODEL_NAME
    respond: Callable[[list[BaseMessage]], Reply]

    @property
    def _llm_type(self) -> str:
        return "fixture"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        # The tracer reads the model name from here, so spans say "chat fixture-model".
        return {"model_name": self.model_name}

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any):
        """Send the tools' definitions with every call, as a real model client does."""
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        text, calls = self.respond(messages)
        tool_calls = [
            {"name": call["name"], "args": call["args"], "id": f"call_{number}", "type": "tool_call"}
            for number, call in enumerate(calls, start=1)
        ]
        # Token counts, so the model span has usage like a real model's.
        input_tokens = sum(count_tokens(str(message.content)) for message in messages)
        output_tokens = count_tokens(text + str(tool_calls))
        usage = UsageMetadata(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=input_tokens + output_tokens)
        message = AIMessage(content=text, tool_calls=tool_calls, usage_metadata=usage)
        return ChatResult(generations=[ChatGeneration(message=message)], llm_output={"model_name": self.model_name})
