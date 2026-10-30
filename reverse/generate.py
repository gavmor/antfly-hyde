#!/usr/bin/env python3
"""Generate an NDJSON corpus with hypothetical questions using Ollama."""

import argparse
import json
import re
import urllib.error
import urllib.request


def generate(base_url, model, title, body):
    prompt = (
        "Write three questions whose answers appear in the document below. "
        "Output one question per line, each ending in a question mark. "
        "Do not answer the questions or add facts absent from the document.\n\n"
        f"Title: {title}\nDocument:\n{body}\n\nQuestions:"
    )
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "You write questions, never answers. Output exactly three questions, each ending with a question mark.",
                },
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "temperature": 0.2,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        result = json.load(response)
    text = result["choices"][0]["message"]["content"]
    lines = [
        re.sub(r"^\s*(?:[-*•]\s*|\d+[.)]\s*)", "", line).strip()
        for line in text.splitlines()
    ]
    questions = [line for line in lines if line.endswith("?")]
    if not questions:
        raise ValueError("model returned no question-ending lines")
    return questions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="NDJSON input with string id, title, and body")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="llama3.2:1b")
    args = parser.parse_args()
    seen = set()
    with open(args.input, encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
                if not isinstance(doc, dict) or any(
                    not isinstance(doc.get(k), str) or not doc[k].strip()
                    for k in ("id", "title", "body")
                ):
                    raise ValueError("expected nonempty string id, title, and body")
                if doc["id"] in seen:
                    raise ValueError("duplicate id")
                seen.add(doc["id"])
                questions = generate(
                    args.ollama_url, args.model, doc["title"], doc["body"]
                )
                doc["questions"] = "\n".join(questions)
                doc["question_generator_model"] = args.model
                print(json.dumps(doc, ensure_ascii=False), flush=True)
            except (
                ValueError,
                KeyError,
                IndexError,
                TypeError,
                OSError,
                urllib.error.URLError,
            ) as exc:
                raise SystemExit(f"{args.input}:{line_number}: {exc}") from exc


if __name__ == "__main__":
    main()
