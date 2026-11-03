"""The chat model the two agents talk to.

Both agents use `FixtureChatModel`: a stand-in that never calls a provider. It
is a real LangChain chat model, so it is traced exactly like a real one, but its
name is `fixture-model` so nobody mistakes its output for a genuine model's.

It also reports token counts, like a real model does, so AIO can show the cost
of each call once a price is set for `fixture-model` (see the README).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatResult

FIXTURE_MODEL_NAME = "fixture-model"


def count_tokens(text: str) -> int:
    """A rough token count: about 4 characters per token, as for English text."""
    return max(1, len(text) // 4)


class FixtureChatModel(BaseChatModel):
    """A simulated chat model that answers from a template instead of an LLM.

    The agents send it the same messages a real model would get. The last
    message is JSON with the facts, so `writer` reads that JSON and turns it
    into plain text. Set `canned_reply` to make it return a fixed text instead,
    which is how a scenario forces a bad answer.
    """

    model_name: str = FIXTURE_MODEL_NAME
    writer: Callable[[dict], str] | None = None
    canned_reply: str | None = None

    @property
    def _llm_type(self) -> str:
        return "fixture"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        # The tracer reads the model name from here, so spans say "chat fixture-model".
        return {"model_name": self.model_name}

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        if self.canned_reply is not None:
            text = self.canned_reply
        else:
            facts = json.loads(messages[-1].content)
            text = self.writer(facts)
        # Token counts, so the model span has usage and AIO can work out a cost.
        input_tokens = sum(count_tokens(str(message.content)) for message in messages)
        output_tokens = count_tokens(text)
        usage = UsageMetadata(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=input_tokens + output_tokens)
        message = AIMessage(content=text, usage_metadata=usage)
        return ChatResult(generations=[ChatGeneration(message=message)], llm_output={"model_name": self.model_name})
