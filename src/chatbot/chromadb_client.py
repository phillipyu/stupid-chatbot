import os
from typing import Any

import chromadb
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction


class ChromaDBClient:
    def __init__(self, collection_name: str):
        self.client = chromadb.Client()
        self.collection = self.client.get_or_create_collection(
            collection_name,
            embedding_function=OpenAIEmbeddingFunction(
                api_key=os.environ.get("OPENAI_API_KEY"),
                model_name="text-embedding-3-small",
            ),
            configuration={
                "hnsw": {
                    "space": "cosine",  # Use cosine distance -- 0 means almost identical, 1 means completely different
                }
            },
        )

    def add_to_collection(
        self,
        ids: list[str],
        embeddings: list[Any],
        documents: list[str],
    ):
        self.collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
        )

    def query_collection(self, query_text, n_results):
        """
        Query the collection for the n closest results

        Return list of tuples of (document, distance)
        """
        result = self.collection.query(query_texts=[query_text], n_results=n_results)
        documents = result.get("documents")[0]
        distances = result.get("distances")[0]
        if not documents or not distances:
            raise ValueError("No results found")

        return [(documents[i], distances[i]) for i in range(len(documents))]
