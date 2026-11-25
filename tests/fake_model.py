"""
A scripted model for the OpenAI Agents SDK: no Groq, no network.
Each step is either a list of tool calls [(name, {args}), ...] or a final answer string, or an
exception to raise. The model also records what the SDK sent it (tools offered, inputs).
"""
import json
import uuid

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText


class ScriptedModel(Model):
    def __init__(self, steps):
        self.steps = list(steps)
        self.offered_tools: list[list[str]] = []
        self.inputs: list = []

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs,
                           tracing, *, previous_response_id=None, conversation_id=None, prompt=None):
        self.offered_tools.append(sorted(t.name for t in tools))
        self.inputs.append(input)
        if not self.steps:
            raise AssertionError("the scripted model ran out of steps")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, str):
            out = [ResponseOutputMessage(id=f"msg_{uuid.uuid4().hex[:8]}", type="message", role="assistant",
                                         status="completed",
                                         content=[ResponseOutputText(type="output_text", text=step, annotations=[])])]
        else:
            out = [ResponseFunctionToolCall(type="function_call", id=f"fc_{uuid.uuid4().hex[:8]}",
                                            call_id=f"call_{uuid.uuid4().hex[:8]}", name=name,
                                            arguments=json.dumps(args), status="completed")
                   for name, args in step]
        return ModelResponse(output=out, usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError
