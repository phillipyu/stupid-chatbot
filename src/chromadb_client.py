from typing import Optional

import chromadb
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction


class ChromaDBClient:
    def __init__(self, collection_name: str):
        self.client = chromadb.Client()
        self.collection = self.client.get_or_create_collection(
            collection_name,
            embedding_function=OpenAIEmbeddingFunction(
                model_name="text-embedding-3-small"
            ),
        )

    def add_to_collection(
        self,
        ids,
        embeddings: Optional[list[list[float]]] = None,
        documents: Optional[list[str]] = None,
    ):
        self.collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
        )

    def query_collection(self, query_text, n_results):
        return self.collection.query(query_texts=[query_text], n_results=n_results)
