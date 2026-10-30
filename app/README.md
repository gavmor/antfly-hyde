# Query-time HyDE search

`hyde.py` generates one or more hypothetical passages, embeds them as a batch, averages their dense vectors and L2-normalizes the mean, then sends a single Antfly query combining the vector with a full-text **match of the original question** using weighted reciprocal rank fusion. Generated text is hypothetical and must not be treated as evidence. Results are real indexed documents, not a generated answer.

## Prerequisites

- A running Antfly server with `/db/v1/query`, a populated table, an active full-text index, and a dense embedding index. Wait for indexing to complete. The dense index must be compatible with the embedding model, dimensions, normalization and document-side embedding recipe used here. A matching model name alone does not guarantee compatible vectors.
- For Gemini, the `google-genai` Python package and a Gemini API key. For the default OpenAI-compatible provider, a generator serving the OpenAI-compatible chat completion protocol and an embedder serving the OpenAI-compatible embeddings protocol. Current Antfly inference serves these at `/ai/v1/generate` and `/ai/v1/embeddings`; install/load appropriate models and use `/ai/v1/models` to inspect availability. The supplied Antfly container may have no inference models; having a server does not imply it can generate or embed.
- Optionally, a current Antfly inference service with a reranker model at `/ai/v1/rerank`. The selected source field must exist as a nonempty string in every candidate.

## Providers and the Rome index

The `rome` table's `body_dense` index uses OpenAI `text-embedding-3-small` at 768 dimensions, so the query-time embedding must match: use `--provider openai` (needs `OPENAI_API_KEY`; direnv loads it). Gemini (`--provider gemini`) uses a different embedding space and does not match this index. Run from the project root with the installed project environment:

```sh
cd /home/user/Desktop/gavmor/antfly-hyde
export GEMINI_API_KEY
uv run python app/hyde.py 'What caused the fall of the Roman Republic?' \
  --provider gemini --table rome --index body_dense \
  --samples 3 --limit 10 --context-neighbors
```

Set `GEMINI_API_KEY` in your shell before exporting it; do not put the key in the command line. The Gemini provider sends the question and generated passages to Google's API. It defaults to `gemini-3.1-flash-lite` for generation and `gemini-embedding-001` for embeddings; `--generator-model` and `--embed-model` override them. Every hypothetical passage is embedded with `RETRIEVAL_DOCUMENT` and `output_dimensionality=768`, then the vectors are averaged and normalized. Use an override only if its embedding recipe is compatible with the target index. `--document-task` is unnecessary for Gemini. `--generate-url` and `--embed-url` apply only to the OpenAI-compatible provider; `--timeout` also applies to Gemini requests. `--context-neighbors` fetches available adjacent chunks from Antfly and requires the neighbors to belong to the same source.

## OpenAI-compatible inference

For example, with a separately provisioned table `knowledge_base`, dense index `kb_dense`, and models installed in Antfly inference:

```sh
cd /home/user/Desktop/gavmor/antfly-hyde/app
python3 hyde.py 'How does this system handle failures?' \
  --table knowledge_base --index kb_dense \
  --generator-model YOUR_INSTALLED_GENERATOR --embed-model YOUR_INDEX_EMBEDDING_MODEL \
  --samples 3 --limit 10
```

Replace the uppercase names with actual installed model names and table/index names. The defaults point to `http://127.0.0.1:18080`; use `--antfly-url`, `--generate-url`, `--embed-url`, and `--rerank-url` to override them independently. Endpoints other than the database URL are **full URLs**. For instance, when the index was built with the same locally installed Ollama embedding model and recipe, Ollama's OpenAI-compatible endpoints can supply generation and embeddings:

```sh
python3 hyde.py 'How does this system handle failures?' \
  --table knowledge_base --index kb_dense \
  --generate-url http://127.0.0.1:11434/v1/chat/completions \
  --embed-url http://127.0.0.1:11434/v1/embeddings \
  --generator-model llama3.2:1b --embed-model nomic-embed-text:latest \
  --samples 3
```

The index's ingestion service must also be able to reach its embedding provider; `127.0.0.1` inside a container refers to the container, not the host. Do not use this Ollama example with an index built with another model or different prefixes. For Antfly models requiring document-role encoding, use `--document-task` to send `task_type: RETRIEVAL_DOCUMENT`; only use this when it matches the indexing recipe and the endpoint supports it.

Optional reranking scores the fused candidate documents against the **original query**, not the hypothetical passage:

```sh
python3 hyde.py 'How does this system handle failures?' \
  --table knowledge_base --index kb_dense \
  --generator-model YOUR_INSTALLED_GENERATOR --embed-model YOUR_INDEX_EMBEDDING_MODEL \
  --rerank-model YOUR_INSTALLED_RERANKER --rerank-field body \
  --candidates 40 --limit 10
```

`--rerank-field` accepts a dot-separated path inside `_source`. `--full-text-index` selects a named full-text index where supported; otherwise the table default is used. `--lexical-weight`, `--dense-weight`, and `--rank-constant` control RRF. The result JSON includes the passages, vector dimension, returned hits and Antfly's total; reranked hits additionally contain `_rerank_score`. Requests fail explicitly on errors, including unavailable models, bad vectors, missing rerank fields and incompatible server APIs. Set `ANTFLY_API_KEY` and/or `INFERENCE_API_KEY` for bearer authentication; avoid embedding credentials in URLs. Each request has a configurable `--timeout`.

## Contract differences and verification

The proposal's `prompt`/`text` generation interface and array of per-index embedding clauses are not the checked-in API: generation uses `messages` and `choices[0].message.content`, and query-time vectors use an index-keyed `embeddings` object. Fusion uses `merge_config`, with the lexical weight keyed by `full_text`. The client calls reranking separately, explicitly passing the original query, rather than assuming a server query processor will choose it. No sparse model is assumed. Multi-sample pooling includes the generated passages only; it does not silently mix the original query into the document-side centroid.

The earlier OpenAI-compatible CLI was exercised against Antfly 0.2.5 with an Ollama-backed dense index and three generated samples. The expected document ranked first and carried both full-text and dense index scores. The optional rerank path requires an installed reranker and was not exercised. Use `uv run python app/hyde.py --help` from the project root for the available options.
