from pathlib import Path

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from openai import OpenAI

from chatbot.chromadb_client import ChromaDBClient

MD_HEADERS_TO_SPLIT_ON = [
    ("#", "Header 1"),
    ("##", "Header 2"),
    ("###", "Header 3"),
]


class EmbeddingsClient:
    def __init__(self):
        self.openai_client = OpenAI()
        self.chromadb_client = ChromaDBClient("chatbot")
        self.markdown_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=MD_HEADERS_TO_SPLIT_ON
        )

    def _chunk_md_document(self, document):
        # Split markdown document into chunks based on headers
        # See: https://python.langchain.com/docs/how_to/markdown_header_metadata_splitter/
        md_chunks = self.markdown_splitter.split_text(document)

        # Within each chunk, split into smaller chunks
        final_chunks = []
        for md_chunk in md_chunks:
            chunks = RecursiveCharacterTextSplitter(
                chunk_size=500, chunk_overlap=50
            ).split_text(md_chunk.page_content)
            final_chunks.extend(chunks)

        # Return list of the chunks
        return final_chunks

    def embed_document(self, file_path: Path):
        if file_path.suffix == ".md":
            chunks = self._chunk_md_document(file_path.read_text())
            embeddings = self.openai_client.embeddings.create(
                input=chunks, model="text-embedding-3-small"
            )
            self.chromadb_client.add_to_collection(
                # file_path#chunk_number is the ID of the document in Chroma
                ids=[f"{file_path.as_posix()}#{i}" for i in range(len(chunks))],
                embeddings=[embedding.embedding for embedding in embeddings.data],
                documents=chunks,
            )
        else:
            raise ValueError(
                f"Unsupported file type: {file_path.suffix} - only .md files are supported right now"
            )

        print("Embedding done")
