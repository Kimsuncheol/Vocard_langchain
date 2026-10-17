import math

from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from vocard.ingestion import InputRejected


def embeddings_for(pipeline: dict) -> Embeddings:
    if pipeline["embedding_provider"] != "openai":
        raise InputRejected("Unsupported embedding provider")
    return OpenAIEmbeddings(
        model=pipeline["embedding_model"],
        dimensions=pipeline["embedding_dimensions"],
        max_retries=2,
        request_timeout=60,
    )


def check_vector(vector, dimensions):
    if (
        not isinstance(vector, list)
        or len(vector) != dimensions
        or any(type(v) not in (float, int) or not math.isfinite(v) for v in vector)
        or not any(v != 0 for v in vector)
    ):
        raise InputRejected("Embedding must be nonzero, finite and match configured dimensions")


def similarity(a, b):
    return math.fsum(x * y for x, y in zip(a, b, strict=True)) / (
        math.sqrt(math.fsum(x * x for x in a)) * math.sqrt(math.fsum(x * x for x in b))
    )
