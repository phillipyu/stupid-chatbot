from typing import Any

from openai import OpenAI


class ResponseClient:
    def __init__(self, instructions: str, tools, model="gpt-4.1"):
        self.openai_client = OpenAI()
        self.model = model
        self.instructions = instructions
        self.tools = tools

    def create_response(self, input: Any):
        try:
            return self.openai_client.responses.create(
                model=self.model,
                input=input,
                instructions=self.instructions,
                stream=True,
                tools=self.tools,
            )
        except Exception as e:
            print(e)
            raise e
