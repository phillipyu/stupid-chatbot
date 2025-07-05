from openai import OpenAI


class EmbeddingsClient:
    def __init__(self):
        self.openai_client = OpenAI()

    def _chunk_document(self, document):
        pass

    def embed_document(self, document):
        return self.openai_client.embeddings.create(
            input=document, model="text-embedding-3-small"
        )
