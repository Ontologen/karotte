import json

from karotte import EvaluationRunConfig
from karotte.schemas import ChatCompletionMessageToolCall, Function, Message


def get_messages(config: EvaluationRunConfig) -> list[Message]:
    """Scripted runs that check an imported task grades as it should.

    `extra_config.validation` picks one: `oracle` applies the task's reference
    solution, mounted read-only at /solution for that run alone, through the
    student's own shell; anything else (`nop`) hands in nothing. The oracle must
    score 1 and the nop 0.
    """
    validation = (config.extra_config or {}).get("validation")
    if validation == "oracle":
        return [
            Message(
                role="assistant",
                content="Applying the reference solution.",
                tool_calls=[
                    ChatCompletionMessageToolCall(
                        id="oracle",
                        type="function",
                        function=Function(
                            name="bash",
                            arguments=json.dumps(
                                {"command": "bash /solution/solve.sh"}
                            ),
                        ),
                    )
                ],
            ),
            Message(role="assistant", content="Done."),
        ]
    return [Message(role="assistant", content="Done.")]
