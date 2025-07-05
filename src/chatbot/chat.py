import json
from importlib import resources
from importlib.resources import as_file
from typing import Optional

import yaml
from openai import OpenAI, Stream
import argparse

from openai.types.responses import ResponseFunctionToolCall, ResponseStreamEvent
from platformdirs import user_data_path

from chatbot.chromadb_client import ChromaDBClient
from chatbot.embeddings_client import EmbeddingsClient
from chatbot.utils import get_date_schema, get_date

SIMILARITY_THRESHOLD = 0.5
APP_NAME = "chatbot"
_HISTORY_FILE = "history.json"


class Chat:
    def __init__(self):
        self.openai_client = OpenAI()
        self.chromadb_client = ChromaDBClient("chatbot")
        self.embeddings_client = EmbeddingsClient()

        # For now, just hardcode the resource to be embedded
        # Ultimately, this should probably be user-defined
        resource_path = resources.files("chatbot.resources").joinpath(
            "remote_data_spec.md"
        )
        with as_file(resource_path) as path:
            self.embeddings_client.embed_document(path)

        parsed_args = self.parse_args()
        self.persona_instructions = self.validate_and_extract_persona_instructions(
            parsed_args
        )
        self.tools = [get_date_schema]

        # Define an absolute path for the history file OUTSIDE of the package
        # We shouldn't be writing to the package itself, don't want unintentional side effects
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

    @classmethod
    def parse_args(cls):
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--persona",
            type=str,
            required=False,
            help="The persona of the chatbot. Select between: academic_professor, nerdy_software_engineer, finance_bro",
        )
        args = parser.parse_args()
        return args

    @classmethod
    def validate_and_extract_persona_instructions(cls, args) -> Optional[str]:
        if not args.persona:
            return None

        config_path = resources.files("chatbot.config").joinpath("roles.yaml")
        with config_path.open("r") as f:
            roles = yaml.safe_load(f)
            if args.persona not in roles:
                print(args.persona, roles)
                raise ValueError(f"Invalid persona: {args.persona}")

            return roles[args.persona]["prompt"]

    def _flush_to_history(self):
        """
        Flush the current history into the history file

        Only keep the last MAX_TURNS history
        """
        with self.history_path.open("w") as f:
            json.dump(self.history[-self.MAX_TURNS :], f)

        print("History flushed!")

    def _process_stream_response(self, response: Stream[ResponseStreamEvent]):
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
                args = json.loads(tool_call.arguments)
                result = get_date(args["timezone"])
                # Append the function call to the history
                self.history.append(
                    {
                        "type": "function_call",
                        "call_id": tool_call.call_id,
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                        "id": tool_call.id,
                        "status": tool_call.status,
                    }
                )
                # self.history.append(tool_call)

                # Assemble the function call output into a form the API will understand
                function_output = {
                    "type": "function_call_output",
                    "call_id": tool_call.call_id,
                    "output": result,
                }
                self.history.append(
                    function_output
                )  # Add the function call output to the history
                follow_up_response = self.openai_client.responses.create(
                    model="gpt-4.1",
                    input=self.history,
                    instructions=self.persona_instructions,
                    stream=True,
                    tools=self.tools,
                )
                self._process_stream_response(follow_up_response)
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
                response = self.openai_client.responses.create(
                    model="gpt-4.1",
                    input=self.history,
                    instructions=self.persona_instructions,
                    stream=True,
                    tools=self.tools,
                )
                print("ChatGPT: ", end="", flush=True)

                # Process the stream response
                self._process_stream_response(response)

                print()
            except KeyboardInterrupt:
                self._flush_to_history()
                break


def main():
    """Entry point for the `chatbot` console script."""
    chat = Chat()
    chat.start()
