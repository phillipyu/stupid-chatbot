import json
from importlib import resources
from importlib.resources import as_file
from typing import Optional

import yaml
from openai import Stream
import argparse

from openai.types.responses import ResponseFunctionToolCall, ResponseStreamEvent
from platformdirs import user_data_path

from chatbot.chromadb_client import ChromaDBClient
from chatbot.embeddings_client import EmbeddingsClient
from chatbot.response_client import ResponseClient
from chatbot.utils import (
    get_date,
    get_date_schema,
    run_python_code,
    run_python_code_schema,
)

SIMILARITY_THRESHOLD = 0.5
APP_NAME = "chatbot"
_HISTORY_FILE = "history.json"
MAX_NUM_ITER = 5
SYSTEM_PROMPT = """
In addition to the above persona, you are a helpful agent.

You always reason in the following format, and never skip steps:

Thought: You reason about what to do next.
Action: You decide what to do next, e.g. choosing one of the functions to invoke.
Observation: I will show you the result.

Repeat this loop until the problem is solved. You ALWAYS print your thoughts at each step.

End with `Final Answer: ...` once you're done.
"""


class Chat:
    def __init__(self):
        self.chromadb_client = ChromaDBClient("chatbot")
        self.embeddings_client = EmbeddingsClient()
        parsed_args = self.parse_args()
        persona_instructions = self.validate_and_extract_persona_instructions(
            parsed_args
        )
        self.response_client = ResponseClient(
            persona_instructions, tools=[get_date_schema, run_python_code_schema]
        )

        # For now, just hardcode the resource to be embedded
        # Ultimately, this should be user-defined
        resource_path = resources.files("chatbot.resources").joinpath(
            "remote_data_spec.md"
        )
        with as_file(resource_path) as path:
            self.embeddings_client.embed_document(path)

        # Define an absolute path for the history file OUTSIDE of the package
        # We shouldn't be writing to the package itself - don't want unintentional side effects
        data_dir = user_data_path(APP_NAME)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = data_dir / _HISTORY_FILE

        # Maintain conversation history, to allow the model to retain context from prior interactions
        if self.history_path.exists():
            with self.history_path.open("r") as f:
                try:
                    self.history = json.load(f)
                except json.JSONDecodeError:
                    print("Failed to load history, starting from scratch...")
                    self.history = []
        else:
            # File doesn't exist, start from scratch
            # File will be created on flush
            self.history = []

        # Maximum number of turns allowed in the history file
        self.MAX_TURNS = 20

        if parsed_args.reset_history:
            self._reset_history()

    @classmethod
    def parse_args(cls):
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--persona",
            type=str,
            required=False,
            help="The persona of the chatbot. Select between: academic_professor, nerdy_software_engineer, finance_bro",
        )
        parser.add_argument(
            "--reset-history",
            action="store_true",
            help="Reset the conversation history",
        )
        args = parser.parse_args()
        return args

    @classmethod
    def validate_and_extract_persona_instructions(cls, args) -> Optional[str]:
        if not args.persona:
            return SYSTEM_PROMPT

        config_path = resources.files("chatbot.config").joinpath("roles.yaml")
        with config_path.open("r") as f:
            roles = yaml.safe_load(f)
            if args.persona not in roles:
                print(args.persona, roles)
                raise ValueError(f"Invalid persona: {args.persona}")

            persona = roles[args.persona]["prompt"]

            return persona + " \n " + SYSTEM_PROMPT

    def _flush_to_history(self):
        """
        Flush the current history into the history file

        Only keep the last MAX_TURNS history
        """
        with self.history_path.open("w") as f:
            json.dump(self.history[-self.MAX_TURNS :], f)

        print("History flushed!")

    def _reset_history(self):
        self.history = []
        self._flush_to_history()

    def _call_function(self, function_name: str, kwargs: dict):
        if function_name == "get_date":
            return get_date(**kwargs)
        if function_name == "run_python_code":
            return run_python_code(**kwargs)
        else:
            raise ValueError(f"Unknown function: {function_name}")

    def _process_stream_response(
        self, response: Stream[ResponseStreamEvent], num_iter: int
    ):
        """
        Process the stream response from the OpenAI API.

        Keeps track of number of iterations, terminates after 3 iterations.
        """
        if num_iter >= MAX_NUM_ITER:
            print("Max number of iterations reached, forcing a decision...")
            self.history.append(
                {
                    "role": "user",
                    "content": f"You have looped {num_iter} times without making a decision. Make a decision now.",
                }
            )
            self.response_client.create_response(input=self.history)
            return

        tool_calls: dict[int, ResponseFunctionToolCall] = {}
        for chunk in response:
            if (
                chunk.type == "response.output_item.added"
                and chunk.item.type == "function_call"
            ):
                # Function call added
                tool_calls[chunk.output_index] = chunk.item
                print()
                print(
                    f"Invoking function {chunk.item.name} with arguments: ",
                    end="",
                    flush=True,
                )
            elif chunk.type == "response.function_call_arguments.delta":
                # Accumulate the function call arguments
                if not tool_calls[chunk.output_index]:
                    raise ValueError(
                        "Function call arguments delta received before function call added"
                    )

                tool_calls[chunk.output_index].arguments += chunk.delta
                print(f"{chunk.delta}", end="", flush=True)
            elif chunk.type == "response.function_call_arguments.done":
                print("...")  # To end the invoking function... line

                # We're done assembling the function call arguments. Let's call the function now!
                if not tool_calls[chunk.output_index]:
                    raise ValueError(
                        "Function call arguments done received before function call added"
                    )

                tool_call: ResponseFunctionToolCall = tool_calls[chunk.output_index]
                kwargs = json.loads(tool_call.arguments)
                result = self._call_function(tool_call.name, kwargs)
                # Append the function call to the history
                self.history.append(
                    {
                        "type": "function_call",
                        "call_id": tool_call.call_id,
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                        # "id": tool_call.id,  # Commenting out because in my experience, providing this id can lead to weird crashes for certain queries
                        "status": tool_call.status,
                    }
                )

                # Assemble the function call output into a form the API will understand
                # This is the "Observation" part of the ReAct loop
                function_output = {
                    "type": "function_call_output",
                    "call_id": tool_call.call_id,
                    "output": "Observation: " + str(result),
                }
                self.history.append(
                    function_output
                )  # Add the function call output to the history
                follow_up_response = self.response_client.create_response(
                    input=self.history,
                )
                self._process_stream_response(follow_up_response, num_iter + 1)
            elif chunk.type == "response.output_text.delta":
                # Outputting a chunk of text, print the chunk
                print(chunk.delta, end="", flush=True)
            elif chunk.type == "response.completed":
                # Response is done, append the response to the history
                output = chunk.response.output_text
                if output:
                    # Don't consider empty outputs (like the response.completed event after a function call)
                    self.history.append(
                        {"role": "assistant", "content": chunk.response.output_text}
                    )

    def _embed_user_message(self, message: str):
        """
        Embed the user's message + add the closest 3 embeddings to the history (provided they are similar enough)
        """
        closest_neighbors = self.chromadb_client.query_collection(
            query_text=message,
            n_results=3,
        )
        for document, distance in closest_neighbors:
            if distance <= SIMILARITY_THRESHOLD:
                # print(document, distance)
                self.history.append(
                    {
                        "role": "system",
                        "content": "You are a helpful assistant. Use the document to answer the user's question: "
                        + document,
                    }
                )

    def start(self):
        while True:
            try:
                next_message = input("You: ")
                self._embed_user_message(next_message)
                self.history.append({"role": "user", "content": next_message})
                try:
                    response = self.response_client.create_response(
                        input=self.history,
                    )
                except Exception as e:
                    print(e)
                    # Flush the history and start over
                    self.history = []
                    continue
                print("ChatGPT: ", end="", flush=True)

                # Process the stream response
                self._process_stream_response(response, num_iter=0)

                print()
            except KeyboardInterrupt:
                self._flush_to_history()
                break


def main():
    """Entry point for the `chatbot` console script."""
    chat = Chat()
    chat.start()


if __name__ == "__main__":
    main()
