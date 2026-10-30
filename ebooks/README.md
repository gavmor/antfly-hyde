# Ebook corpus preparation

`prepare.py` recursively considers `.pdf`, `.epub`, `.azw3`, `.djvu`, `.djv`, and `.txt` in `_raw/_archived` and `_raw/_extracted`, except `_extracted/README.txt`. It writes Antfly-loadable NDJSON and a source-by-source JSON report; it does not connect to Antfly or generate embeddings.

## Requirements and extraction

Use the project environment installed with `uv`, including Unstructured with its PDF and EPUB dependencies, `langchain-text-splitters`, and `google-genai`. AZW3 requires Calibre's `ebook-convert`; DJVU/DJV requires DjVuLibre's `djvutxt`. Missing converters and unreadable, empty, encrypted, or image-only inputs are reported as errors. PDF uses Unstructured's fast text extraction, not OCR or high-resolution table recognition. Existing OCR-extracted text is processed as text.

Unstructured's `chunk_by_title` uses a 1,200-character maximum, with multipage sections and original-element metadata enabled. The 180-character overlap applies when splitting an oversized element, not between all chunks: Unstructured's `overlap_all` can carry text across chapter boundaries. EPUBs are read with EbookLib: each spine document is partitioned separately with Unstructured's HTML partitioner, so chunks never cross spine documents, and the EPUB's table-of-contents hierarchy supplies the section breadcrumb. A spine document that matches several table-of-contents entries gets no navigation breadcrumb, and the report counts them as ambiguous. Unstructured `Title` elements inside a document add further breadcrumb levels where it finds them; it found none in the two EPUBs I examined, so those books rely on the table of contents. An EPUB with neither gets an empty section and a report warning rather than an invented hierarchy. Small sections are not combined, so short chunks can occur at chapter boundaries. PDF `page_numbers` come from original elements and may include multiple physical pages. Empty page lists are reported as warnings. Elements classified as `Header` or `Footer` are discarded, but the fast parser does not reliably classify recurring page furniture; repeated headers and watermarks can remain. Table HTML is retained when supplied by Unstructured; fast PDF extraction does not guarantee table recognition.

Every contextual text is checked against the 8,191-token input limit of OpenAI `text-embedding-3-small`, counted locally with `tiktoken` (`cl100k_base`); no API call is made. Chunks are about 1,200 characters, so the limit is rarely close. If a single chunk is over budget, LangChain's recursive text splitter subdivides it and the results are checked again. A table with HTML that would require this extra split is reported as an error rather than silently losing its structure. If a source cannot be partitioned within `--source-timeout` seconds (default 600; corrupt PDFs can hang the parser) it is reported as an error and extraction continues. When Unstructured's fast PDF parser returns nothing for a PDF that has a text layer, `pdftotext` is used instead, with page numbers taken from its page breaks.

## Prepare and monitor

From the project directory:

```sh
uv run python ebooks/prepare.py \
  --source-root /home/user/Documents/rome/_raw \
  --output data/rome-structured.ndjson \
  --report data/rome-structured-report.json
```

`--source-root` defaults to the path shown; `--chunk-size` and `--overlap` override the character settings. Output and report must be distinct and outside the source directories. Existing output and report are replaced. NDJSON is flushed as each source finishes, so the existing dashboard can show extraction progress:

```sh
uv run python ebooks/monitor.py --output data/rome-structured.ndjson --report data/rome-structured-report.json
```

Open `http://127.0.0.1:18762/`. The JSON report is written only when extraction finishes because the dashboard treats its presence as completion. It contains every supported input's status, hashes, duplicate relationship, warnings or errors, and indexed chunk count. The script continues after source failures, preserves successful records, and exits 1 if any source failed. Review the final report before loading. The dashboard shows extraction, not Antfly indexing progress.

Each record has `id`, filename-derived `title`, original chunk `body`, breadcrumb-prefixed `embedding_text`, `section` (a list), `content_sha256` of the UTF-8 body, source paths, zero-based `chunk_index`, and `previous_id`/`next_id` for adjacent chunks of the same source (null at the ends). PDF records include `page_numbers`; available structured table markup is in `table_html`. The stable key combines a SHA-256 hash of the source-relative path and the chunk index. Moving a source, changing its content or chunk settings can change keys or associated text; use a fresh table or remove obsolete records.

Exact file bytes are deduplicated by SHA-256. Extracted element text is also deduplicated by SHA-256 after whitespace and NUL normalization. The first successful source in deterministic order is retained; archived sources precede extracted sources. Different editions or conversions with different extracted text are retained. An unreadable original and a usable pre-extracted copy remain separate entries, preserving both the error and the copy's provenance.

## Load

After configuring the destination table and its dense index (OpenAI embedder over `embedding_text`), stream or load the output using `id` as the ID field. To stream while extraction runs, tail the growing file into the loader (start the loader after the extractor has created and truncated the file, or it will read stale content):

```sh
antfly load --table rome --file data/rome-structured.ndjson --id-field id --size 100 --batches 2000 --sync-level write --no-checkpoint --strict
```

```sh
tail -n +1 -f --pid=<extractor pid> data/rome-structured.ndjson | \
  antfly load --table rome --file /dev/stdin --id-field id --size 100 --batches 5000 \
  --sync-level write --no-checkpoint --strict
```

This writes to the named table; it does not create the table or configure its index. In Antfly 0.2.5, `--batches` limits the total number of batches despite the help text saying it controls in-flight batches. Set it high enough for the corpus and check the final loaded count.
