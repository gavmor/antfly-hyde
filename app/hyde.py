#!/usr/bin/env python3
"""Query-time HyDE against Antfly, with Gemini or OpenAI-compatible inference."""
import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


class SearchError(Exception):
    pass


def post(url, payload, token, timeout):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, json.dumps(payload, allow_nan=False).encode(), headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(8192).decode("utf-8", "replace")
        raise SearchError(f"{url}: HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SearchError(f"{url}: {exc}") from exc
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SearchError(f"{url}: invalid JSON response") from exc

def get_document(base_url, table, key, token, timeout):
    url = (base_url.rstrip("/") + "/db/v1/tables/" + urllib.parse.quote(table, safe="")
           + "/documents/" + urllib.parse.quote(key, safe=""))
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise SearchError(f"{url}: could not retrieve context: {exc}") from exc


def add_neighbor_context(hits, args):
    for hit in hits:
        source = hit.get("_source", {})
        neighbors = []
        for direction in ("previous_id", "next_id"):
            key = source.get(direction)
            if key:
                document = get_document(args.antfly_url, args.table, key, args.antfly_token, args.timeout)
                if not isinstance(document, dict) or document.get("source_relative_path") != source.get("source_relative_path"):
                    raise SearchError(f"neighbor {key!r} does not belong to the same source")
                neighbors.append(document)
        hit["context_neighbors"] = neighbors



def pooled_vectors(response, count):
    try:
        data = response["data"]
        if len(data) != count:
            raise ValueError("wrong number of embeddings")
        by_index = {item["index"]: item["embedding"] for item in data}
        if set(by_index) != set(range(count)):
            raise ValueError("missing or duplicated embedding indexes")
        vectors = [by_index[i] for i in range(count)]
        if not isinstance(vectors[0], list) or not vectors[0]:
            raise ValueError("expected nonempty dense float vectors, not sparse embeddings")
        dim = len(vectors[0])
        if any(not isinstance(v, list) or len(v) != dim for v in vectors):
            raise ValueError("embedding dimensions differ")
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for v in vectors for x in v):
            raise ValueError("embedding contains a non-finite or nonnumeric value")
        mean = [sum(v[i] for v in vectors) / count for i in range(dim)]
        norm = math.sqrt(sum(x * x for x in mean))
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("pooled embedding has zero or invalid norm")
        return [x / norm for x in mean]
    except (KeyError, TypeError, ValueError) as exc:
        raise SearchError(f"invalid embedding response: {exc}") from exc


def rerank(hits, args):
    documents = []
    for hit in hits:
        source = hit.get("_source")
        value = source
        for part in args.rerank_field.split("."):
            if not isinstance(value, dict) or part not in value:
                raise SearchError(f"hit {hit.get('_id', '?')} has no rerank field {args.rerank_field!r} in _source")
            value = value[part]
        if not isinstance(value, str) or not value.strip():
            raise SearchError(f"hit {hit.get('_id', '?')} rerank field must be a nonempty string")
        documents.append(value)
    if not documents:
        return hits
    result = post(args.rerank_url, {"model": args.rerank_model, "query": args.query, "documents": documents}, args.inference_token, args.timeout)
    try:
        scores = result["data"]
        by_index = {entry["index"]: entry["score"] for entry in scores}
        if len(scores) != len(hits) or set(by_index) != set(range(len(hits))):
            raise ValueError("missing or duplicate scores")
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in by_index.values()):
            raise ValueError("scores must be finite numbers")
        for i, hit in enumerate(hits):
            hit["_rerank_score"] = by_index[i]
        return sorted(hits, key=lambda h: h["_rerank_score"], reverse=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise SearchError(f"invalid rerank response: {exc}") from exc


def openai_vectors(prompt, args):
    if not os.environ.get("OPENAI_API_KEY"):
        raise SearchError("OPENAI_API_KEY must be set for the OpenAI provider")
    from openai import OpenAI, OpenAIError
    try:
        client = OpenAI(timeout=args.timeout)
        passages = []
        for _ in range(args.samples):
            response = client.chat.completions.create(
                model=args.generator_model, messages=[{"role": "user", "content": prompt}],
                temperature=args.temperature, max_completion_tokens=args.max_tokens)
            text = response.choices[0].message.content
            if not text or not text.strip():
                raise SearchError("OpenAI returned an empty generated passage")
            passages.append(text.strip())
        embeddings = client.embeddings.create(model=args.embed_model, input=passages, dimensions=768)
        vectors = {"data": [{"index": i, "embedding": item.embedding} for i, item in enumerate(embeddings.data)]}
        return passages, pooled_vectors(vectors, len(passages))
    except OpenAIError as exc:
        raise SearchError(f"OpenAI request failed ({type(exc).__name__}): {exc}") from None


def gemini_vectors(prompt, args):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SearchError("GEMINI_API_KEY must be set for the Gemini provider")
    try:
        from google import genai
    except ImportError as exc:
        raise SearchError("the Gemini provider requires the google-genai package") from exc

    try:
        with genai.Client(api_key=api_key, http_options={"timeout": args.timeout * 1000}) as client:
            passages = []
            for _ in range(args.samples):
                response = client.models.generate_content(
                    model=args.generator_model, contents=prompt,
                    config={"temperature": args.temperature, "max_output_tokens": args.max_tokens},
                )
                text = response.text
                if not isinstance(text, str) or not text.strip():
                    raise SearchError("Gemini returned an empty generated passage")
                passages.append(text.strip())
            response = client.models.embed_content(
                model=args.embed_model, contents=passages,
                config={"task_type": "RETRIEVAL_DOCUMENT", "output_dimensionality": 768},
            )
            embeddings = response.embeddings or []
            vectors = {"data": [{"index": i, "embedding": item.values}
                                for i, item in enumerate(embeddings)]}
            vector = pooled_vectors(vectors, len(passages))
            if len(vector) != 768:
                raise SearchError("Gemini returned embeddings with dimensions other than 768")
            return passages, vector
    except SearchError:
        raise
    except Exception as exc:
        status = getattr(exc, "code", None)
        message = str(getattr(exc, "message", type(exc).__name__)).replace(api_key, "[redacted]")
        raise SearchError(f"Gemini request failed{f' (HTTP {status})' if status else ''}: {message}") from None


def run(args):
    if not args.query.strip():
        raise SearchError("query must not be empty")
    prompt = ("Write a concise hypothetical reference passage that directly answers the question. "
              "Use concrete terminology. Output only the passage; do not refer to the question.\n\nQuestion: " + args.query)
    if args.provider == "openai":
        passages, vector = openai_vectors(prompt, args)
    elif args.provider == "gemini":
        passages, vector = gemini_vectors(prompt, args)
    else:
        passages = []
        for _ in range(args.samples):
            result = post(args.generate_url, {"model": args.generator_model, "messages": [{"role": "user", "content": prompt}], "temperature": args.temperature, "max_tokens": args.max_tokens, "stream": False}, args.inference_token, args.timeout)
            try:
                text = result["choices"][0]["message"]["content"]
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("empty generated passage")
                passages.append(text.strip())
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise SearchError(f"invalid generation response: {exc}") from exc
        embed_payload = {"model": args.embed_model, "input": passages}
        if args.document_task:
            embed_payload["task_type"] = "RETRIEVAL_DOCUMENT"
        vectors = post(args.embed_url, embed_payload, args.inference_token, args.timeout)
        vector = pooled_vectors(vectors, len(passages))
    request = {
        "table": args.table,
        "full_text_search": {"match": args.query},
        "indexes": [args.index],
        "embeddings": {args.index: vector},
        "merge_config": {"strategy": "rrf", "rank_constant": args.rank_constant,
                         "weights": {"full_text": args.lexical_weight, args.index: args.dense_weight}},
        "limit": args.candidates if args.rerank_model else args.limit,
    }
    if args.full_text_index:
        request["full_text_index"] = args.full_text_index
    response = post(args.antfly_url.rstrip("/") + "/db/v1/query", request, args.antfly_token, args.timeout)
    try:
        results = response["responses"]
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise ValueError("expected exactly one query result")
        result = results[0]
        if result.get("error") or result.get("status", 200) >= 400:
            raise SearchError(f"Antfly query failed: {result.get('error', result.get('status'))}")
        hits = result["hits"]["hits"]
        if not isinstance(hits, list):
            raise TypeError("hits must be a list")
    except (KeyError, TypeError, ValueError) as exc:
        raise SearchError(f"invalid Antfly query response: {exc}") from exc
    if args.rerank_model:
        hits = rerank(hits, args)
    hits = hits[:args.limit]
    if args.context_neighbors:
        add_neighbor_context(hits, args)
    return {"query": args.query, "hypothetical_documents": passages, "embedding_dimensions": len(vector),
            "ranking": "rerank" if args.rerank_model else "rrf", "hits": hits,
            "antfly_total": result["hits"].get("total")}


def positive(value):
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return n


def nonnegative(value):
    n = float(value)
    if not math.isfinite(n) or n < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return n


def main():
    p = argparse.ArgumentParser(description="Generate hypothetical documents, pool their embeddings, and query Antfly with lexical + dense RRF.")
    p.add_argument("query", help="original user query")
    p.add_argument("--provider", choices=("compatible", "openai", "gemini"), default="compatible",
                   help="inference provider (default: generic OpenAI-compatible endpoints)")
    p.add_argument("--table", required=True)
    p.add_argument("--index", required=True, help="dense embedding index name")
    p.add_argument("--full-text-index", help="named full-text index (omit for table default)")
    p.add_argument("--antfly-url", default="http://127.0.0.1:18080")
    p.add_argument("--generate-url", default="http://127.0.0.1:18080/ai/v1/generate", help="full OpenAI-compatible chat completion URL")
    p.add_argument("--embed-url", default="http://127.0.0.1:18080/ai/v1/embeddings", help="full OpenAI-compatible embedding URL")
    p.add_argument("--generator-model", help="generation model (Gemini default: gemini-3.1-flash-lite)")
    p.add_argument("--embed-model", help="embedding model (Gemini default: gemini-embedding-001); must match the index recipe")
    p.add_argument("--samples", type=positive, default=1)
    p.add_argument("--document-task", action="store_true", help="send Antfly task_type=RETRIEVAL_DOCUMENT to embed endpoint")
    p.add_argument("--temperature", type=nonnegative, default=0.4)
    p.add_argument("--max-tokens", type=positive, default=256)
    p.add_argument("--limit", type=positive, default=10)
    p.add_argument("--candidates", type=positive, default=40, help="RRF candidate count when reranking")
    p.add_argument("--rank-constant", type=nonnegative, default=60)
    p.add_argument("--lexical-weight", type=nonnegative, default=1)
    p.add_argument("--dense-weight", type=nonnegative, default=1)
    p.add_argument("--rerank-model", help="enable Antfly inference reranking with this model")
    p.add_argument("--rerank-url", default="http://127.0.0.1:18080/ai/v1/rerank")
    p.add_argument("--rerank-field", default="body", help="dot-separated string field in returned _source")
    p.add_argument("--context-neighbors", action="store_true", help="retrieve adjacent chunks for each result when available")
    p.add_argument("--timeout", type=positive, default=120, help="per-request timeout in seconds")
    p.add_argument("--brief", action="store_true", help="one compact record per hit (source, pages, id, passage) instead of raw Antfly JSON")
    args = p.parse_args()
    if args.provider == "openai":
        args.generator_model = args.generator_model or "gpt-4.1-mini"
        args.embed_model = args.embed_model or "text-embedding-3-small"
    elif args.provider == "gemini":
        args.generator_model = args.generator_model or "gemini-3.1-flash-lite"
        args.embed_model = args.embed_model or "gemini-embedding-001"
    elif not args.generator_model or not args.embed_model:
        p.error("--generator-model and --embed-model are required for the compatible provider")
    args.antfly_token = os.environ.get("ANTFLY_API_KEY")
    args.inference_token = os.environ.get("INFERENCE_API_KEY")
    if args.candidates < args.limit and args.rerank_model:
        p.error("--candidates must be at least --limit")
    if args.temperature > 2:
        p.error("--temperature must be at most 2")
    if args.lexical_weight == 0 and args.dense_weight == 0:
        p.error("at least one fusion weight must be positive")
    try:
        result = run(args)
        if args.brief:
            result = {"hypothetical_documents": result["hypothetical_documents"], "hits": [
                {"source": h["_source"].get("source_relative_path"), "pages": h["_source"].get("page_numbers"),
                 "id": h["_id"], "scores": h.get("_index_scores"), "passage": h["_source"].get("body"),
                 **({"neighbors": [n.get("body") for n in h["context_neighbors"]]} if "context_neighbors" in h else {})}
                for h in result["hits"]]}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except SearchError as exc:
        print(f"hyde: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
