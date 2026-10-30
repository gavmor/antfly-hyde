# Write-time Doc2Query exploration

This example generates hypothetical questions **before ingestion**, stores them alongside each original document, and maintains two Antfly dense indexes: one over the document and one over its generated questions. Search can use either index or fuse both. No LLM generation occurs at query time; Antfly still embeds the search text. This is an external ingestion workflow, not a native Antfly enrichment chain or a single unioned artifact index.

## Prerequisites

- Python 3, a compatible `antfly` CLI on `PATH`, and `ANTFLY_URL` pointing to the server. This machine has Antfly 0.2.5 installed and its Zsh environment points to `http://127.0.0.1:18080`. For another machine, install a matching CLI from [Antfly downloads](https://antfly.io/docs/downloads). Use a fresh table name, never the existing `rome` table.
- Ollama reachable from the machine running Python, with `llama3.2:1b` for generation and `nomic-embed-text` for embeddings. Install these models with `ollama pull llama3.2:1b` and `ollama pull nomic-embed-text` if needed. Ollama's OpenAI-compatible `/v1/chat/completions` endpoint is used without credentials.
- **Also** ensure Ollama is reachable from the Antfly server process. The `url` in the index configuration is resolved on the server, not by the CLI. If Antfly runs in a container, `127.0.0.1` means that container; configure Ollama to listen on an address reachable from the container and use that host address below. Exposure beyond localhost needs appropriate network restrictions. No provider credential is required for this local example.

The example uses the 768-dimensional `nomic-embed-text` model for both indexes and queries. If changing models, change both index configurations and the dimension together; different vector spaces must not be mixed merely because their dimensions match. Embedding models can have length limits; keep each body and the combined question text within the model's limit, or preprocess long documents into independently identified passages.

## Run

From this directory, generate one output row per input row. Input is NDJSON with unique, nonempty string `id`, `title`, and `body` fields. Other fields are preserved. `questions` and `question_generator_model` are overwritten. The model is asked for three questions, but the code accepts any nonzero number of question-ending lines; inspect the output for hallucinations and quality before loading. The original sample documents are fictional.

```sh
python3 generate.py documents.ndjson --model llama3.2:1b > generated.ndjson
```

Use `--ollama-url http://HOST:11434` if the generator is elsewhere. If generation fails the command exits nonzero and the redirected output may be partial: discard it, fix the problem, and rerun before loading. Generated questions are model output, not verified facts. Do not send sensitive documents to an untrusted model endpoint.

Choose a **new** table name and set the URL from Antfly's network namespace. These commands connect to Antfly over HTTP, create both embedding indexes and the default full-text index, then load the generated records directly from the host. `172.17.0.1` was this machine's Docker bridge gateway; use the address reachable by your Antfly server.

```sh
TABLE=doc2query_demo
OLLAMA_FROM_ANTFLY=http://172.17.0.1:11434
EMBEDDER=$(printf '{"provider":"ollama","model":"nomic-embed-text","url":"%s"}' "$OLLAMA_FROM_ANTFLY")

antfly table create --table "$TABLE" \
  --index "$(printf '{"name":"document_vectors","type":"embeddings","template":"{{title}} {{body}}","dimension":768,"embedder":%s}' "$EMBEDDER")" \
  --index "$(printf '{"name":"question_vectors","type":"embeddings","field":"questions","dimension":768,"embedder":%s}' "$EMBEDDER")"
antfly load --table "$TABLE" --file generated.ndjson \
  --id-field id --sync-level full_index --no-checkpoint
```

Loading is an upsert: rerunning replaces rows with the same IDs, but does **not** delete rows removed from the input. Re-run generation on every changed document; direct writes bypass this workflow. The `full_index` sync level waits for indexing; inspect index status and resolve provider or indexing errors before comparing retrieval.

```sh
antfly index list --table "$TABLE"
antfly lookup --table "$TABLE" --key harbor
antfly query --table "$TABLE" --semantic-search 'When are the harbor gates closed?' \
  --indexes document_vectors --fields 'title,body,questions' --limit 3
antfly query --table "$TABLE" --semantic-search 'When are the harbor gates closed?' \
  --indexes question_vectors --fields 'title,body,questions' --limit 3
antfly query --table "$TABLE" --semantic-search 'When are the harbor gates closed?' \
  --indexes document_vectors,question_vectors --fields 'title,body,questions' --limit 3
```

The last request fuses two ranked index results; it does not concatenate vectors or create one physical index. All generated questions for a document are embedded as **one text**, not as one vector per question. Compare with a relevance-labelled set, not just this one query: extra generated terms can improve recall or introduce semantic drift. Any failure or malformed/empty generation stops ingestion rather than silently replacing questions with a fallback.

## Why not the proposed native chain?

The current source schema (`openapi.yaml`, `EnrichmentConfig`) allows a chunk enrichment to consume an asset and an embedding enrichment to consume a **chunk**, not an asset directly. Generator asset producers exist. However, schema admission alone does not demonstrate that arbitrary generator-asset text is fanned out into chunks and embedded end to end in the deployed version: the runtime has special handling for document-extraction unit assets and media outputs, and this example does not assume the arbitrary generator chain works. A production native solution needs an end-to-end runtime test on the exact deployment, including update/delete and failure propagation.

The current artifact index guide documents `sources` unioning up to 64 named artifact streams and requiring `index_capabilities.artifact_sources`; compatible dimensions and vector spaces are mandatory, and explicit and implicit vector-space identifiers cannot be mixed. That is not what the two ordinary indexes above do. Source references in the Antfly checkout: `docs/guides/artifact-indexes.mdx`, `openapi.yaml` (`EnrichmentConfig`, `CreateEmbeddingsIndexRequest`), `zig/pkg/antfly-embedded/src/storage/db/catalog/index_manager.zig` (chunk scheduling), and `zig/pkg/antfly-embedded/src/storage/db/enrichment/enrichment_runtime.zig` (asset-backed chunk handling). The actual embedding and provider setup follows `docs/guides/quickstart.mdx` and `docs/guides/providers/ollama.mdx`.
