# Antfly HyDE exploration

Experiments with hypothetical-document retrieval over Roman-history ebooks stored in Antfly, plus the pipeline that builds that corpus. Install dependencies with `uv sync`.

For development, run `uv sync` and `uv run lefthook install` after cloning.
The pre-commit hook checks staged Python files with Ruff (lint and formatting)
and runs ty across the project. Run them manually with `uv run ruff check .`,
`uv run ruff format --check .`, and `uv run ty check`.
Ebook extraction failures are recorded in the report and logged with a
traceback; processing continues with the next source.

## Parts

- [`ebooks/`](ebooks/README.md): turns `~/Documents/rome/_raw` (PDF, EPUB, AZW3, DjVu, text) into source-attributed chunks with Unstructured, checks them against the embedding token limit with `tiktoken`, and streams them into Antfly. `monitor.py` is a browser dashboard for extraction and indexing progress.
- [`app/`](app/README.md): query-time HyDE. Generates one or more hypothetical passages, averages their embeddings, and fuses that dense search with a keyword search of the original query (Antfly RRF). Providers: OpenAI, Gemini, or generic OpenAI-compatible endpoints.
- [`reverse/`](reverse/README.md): write-time Doc2Query exploration (generated questions indexed beside documents), run against Ollama.
- [`extension/`](extension/README.md): investigation of a WASM MCP `hyde_search` tool. **Not implementable on the checked-out Antfly:** the host has no generation import, rewrites extension queries to `match_all`, and its embedding import returns a hash-derived vector. No fake extension is installed.

## The `rome` table

Antfly 0.2.5 runs in Docker at `http://127.0.0.1:18080` with its data in the named volume `antfly_rome_data` (removing the volume destroys the index). The `rome` table has the default full-text index and a dense `body_dense` index over the `embedding_text` field: OpenAI `text-embedding-3-small`, 768 dimensions. The key is stored in Antfly's secret store as `openai.api_key` and referenced from the index as `${secret:openai.api_key}`. Chunk text is sent to OpenAI for embedding; embedding the whole corpus cost well under $1.

```sh
export ANTFLY_URL=http://127.0.0.1:18080
antfly index list --table rome
antfly query --table rome --semantic-search "a Roman noble goes into exile" \
  --full-text-search 'body:exile' --indexes body_dense \
  --fields source_relative_path,page_numbers,body --limit 8
```

HyDE (needs `OPENAI_API_KEY`):

```sh
uv run python app/hyde.py "What happened to a Roman's property in exile?" \
  --provider openai --table rome --index body_dense --samples 2 --limit 8 --brief
```

The query-time embedding model and dimensions must match the index. The Rome vault's search tooling (`scripts/raw_source_search.py --backend antfly` in `~/Documents/rome`) is the supported interface for wiki work.

## Interpretation and limits

Hypothetical passages and generated questions are model guesses, **not evidence**; returned source passages are. In smoke tests HyDE sometimes did worse than plain hybrid search because the keyword half matched generic words (use `--lexical-weight` below 1 for questions). Antfly's built-in RAG answer step (`agents retrieval --generate`) returned `400 invalid retrieval agent request` with an OpenAI generator and is unused. Source text has extraction defects (missing spaces, glyph garbage, ligatures), and *A Place at the Altar* is unindexed because the PDF is corrupt.
