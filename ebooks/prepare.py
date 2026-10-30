#!/usr/bin/env python3
"""Extract ebook sources into attributed, Gemini-token-checked NDJSON chunks."""

import argparse
import hashlib
import io
import json
import logging
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urldefrag

import tiktoken
from ebooklib import epub
from langchain_text_splitters import RecursiveCharacterTextSplitter
from unstructured.chunking.title import chunk_by_title
from unstructured.partition.html import partition_html
from unstructured.partition.pdf import partition_pdf
from unstructured.partition.text import partition_text

logger = logging.getLogger(__name__)

FORMATS = {".pdf", ".epub", ".azw3", ".djvu", ".djv", ".txt"}
ENCODING = "cl100k_base"  # tokenizer of OpenAI text-embedding-3-small
TOKEN_LIMIT = 8191  # text-embedding-3-small input limit per text


class TokenCountError(RuntimeError):
    """Token-counting failure invalidates the rest of the ingestion run."""


class SourceTimeout(BaseException):
    """BaseException so a parser's broad `except Exception` cannot swallow it."""


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def collect_epub_targets(items, parents, targets):
    for item in items:
        if isinstance(item, tuple):
            section, children = item
            title = getattr(section, "title", None)
            hierarchy = parents + ([title.strip()] if title and title.strip() else [])
            href = getattr(section, "href", None)
            if href and hierarchy:
                targets.setdefault(unquote(urldefrag(href)[0]), set()).add(
                    tuple(hierarchy)
                )
            collect_epub_targets(children, hierarchy, targets)
        else:
            title, href = getattr(item, "title", None), getattr(item, "href", None)
            if href and title and title.strip():
                targets.setdefault(unquote(urldefrag(href)[0]), set()).add(
                    tuple(parents + [title.strip()])
                )


def epub_groups(path):
    """Partition EPUB spine documents using EbookLib's navigation hierarchy."""
    book = epub.read_epub(str(path))
    targets = {}
    collect_epub_targets(book.toc, [], targets)
    groups = []
    ambiguous = []
    for item_id, _linear in book.spine:
        item = book.get_item_with_id(item_id)
        if item is None:
            raise ValueError(f"EPUB spine references missing item: {item_id}")
        name = unquote(urldefrag(item.get_name())[0])
        candidates = targets.get(name, set())
        if len(candidates) > 1:
            ambiguous.append(name)
        section = list(next(iter(candidates))) if len(candidates) == 1 else []
        elements = partition_html(
            file=io.BytesIO(item.get_content()), metadata_filename=str(path)
        )
        if elements:
            groups.append((elements, section))
    warning = (
        f"ambiguous table-of-contents targets in {len(ambiguous)} spine documents"
        if ambiguous
        else None
    )
    return groups, warning


def extract_pdf(path):
    elements = partition_pdf(filename=str(path), strategy="fast")
    if elements:
        return [(elements, [])], None
    # Unstructured's fast parser returns nothing for some PDFs that have a text
    # layer; fall back to poppler, keeping page numbers from its form-feeds.
    if not shutil.which("pdftotext"):
        raise RuntimeError("required executable not found: pdftotext")
    result = subprocess.run(
        ["pdftotext", "-enc", "UTF-8", str(path), "-"],
        capture_output=True,
        check=False,
        text=True,
        errors="replace",
    )
    if result.returncode:
        raise RuntimeError(
            f"pdftotext exited {result.returncode}: {result.stderr.strip()}"
        )
    elements = []
    for number, page in enumerate(result.stdout.split("\f"), 1):
        for element in partition_text(text=page):
            element.metadata.page_number = number
            elements.append(element)
    return [(elements, [])], "Unstructured fast extraction was empty; used pdftotext"


def extract(path):
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".epub":
        return epub_groups(path)
    if suffix == ".txt":
        return [(partition_text(filename=str(path), encoding="utf-8-sig"), [])], None
    tool = "ebook-convert" if suffix == ".azw3" else "djvutxt"
    if not shutil.which(tool):
        raise RuntimeError(f"required executable not found: {tool}")
    with tempfile.TemporaryDirectory(prefix="antfly-ebook-") as temp:
        output = Path(temp) / "extracted.txt"
        result = subprocess.run(
            [tool, str(path), str(output)],
            capture_output=True,
            check=False,
            text=True,
            errors="replace",
        )
        details = result.stderr.strip()
        if result.returncode:
            raise RuntimeError(
                f"{tool} exited {result.returncode}: {details or result.stdout.strip() or 'no diagnostic'}"
            )
        elements = partition_text(filename=str(output), encoding="utf-8-sig")
        return [(elements, [])], details or None


def section_map(elements):
    """Associate each original element with the actual EPUB title context."""
    sections = []
    result = {}
    titles = 0
    for element in elements:
        if element.category == "Title" and str(element).strip():
            depth = element.metadata.category_depth
            if depth is None or depth < 0:
                # Without a level, treat each heading as a peer, not an invented hierarchy.
                depth = 0
            sections = sections[:depth]
            sections.append(str(element).strip())
            titles += 1
        result[element.id] = list(sections)
    return result, titles


def count_tokens(encoding, texts):
    """Largest token count among the texts (each is embedded separately)."""
    return max(len(encoding.encode(text, disallowed_special=())) for text in texts)


def checked_chunks(client, chunks, chunk_size, overlap):
    """Count batches conservatively, subdividing oversized batches and texts."""
    if not chunks:
        return []
    if (
        count_tokens(client, [chunk["embedding_text"] for chunk in chunks])
        <= TOKEN_LIMIT
    ):
        return chunks
    if len(chunks) > 1:
        middle = len(chunks) // 2
        return checked_chunks(
            client, chunks[:middle], chunk_size, overlap
        ) + checked_chunks(client, chunks[middle:], chunk_size, overlap)
    chunk = chunks[0]
    if chunk.get("table_html"):
        raise ValueError(
            "table exceeds Gemini's token limit; refusing to discard table structure"
        )
    # Split using a maintained library, while measuring the complete contextual text.
    size = min(chunk_size, len(chunk["body"])) // 2
    while size >= 1:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=size, chunk_overlap=min(overlap, size // 4)
        )
        pieces = splitter.split_text(chunk["body"])
        if len(pieces) > 1:
            result = []
            for piece in pieces:
                item = dict(chunk, body=piece)
                item["embedding_text"] = contextual_text(
                    item["title"], item["section"], piece
                )
                result.extend(
                    checked_chunks(client, [item], size, min(overlap, size // 4))
                )
            return result
        size //= 2
    raise ValueError("context or indivisible text exceeds the embedding token limit")


def contextual_text(title, section, body):
    return " > ".join([title] + section) + "\n\n" + body


def block_record(block, title, navigation, sections, suffix):
    body = str(block).strip()
    if not body:
        return None
    originals = block.metadata.orig_elements or []
    detected_section = next(
        (sections[item.id] for item in originals if item.id in sections), []
    )
    section = list(navigation)
    for heading in detected_section:
        if not section or heading != section[-1]:
            section.append(heading)
    pages = sorted(
        {
            item.metadata.page_number
            for item in originals
            if item.metadata.page_number is not None
        }
    )
    record = {
        "title": title,
        "body": body,
        "section": section,
        "embedding_text": contextual_text(title, section, body),
    }
    if suffix == ".pdf":
        record["page_numbers"] = pages
    if block.metadata.text_as_html:
        record["table_html"] = block.metadata.text_as_html
    return record


def attribute_records(checked, path, relative):
    source_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()
    ids = [f"ebook:{source_id}:{index:06d}" for index in range(len(checked))]
    for index, record in enumerate(checked):
        record.update(
            id=ids[index],
            source_path=str(path),
            source_filename=path.name,
            source_relative_path=relative,
            chunk_index=index,
            previous_id=ids[index - 1] if index else None,
            next_id=ids[index + 1] if index + 1 < len(ids) else None,
            content_sha256=hashlib.sha256(record["body"].encode("utf-8")).hexdigest(),
        )


def prepare_records(path, relative, groups, client, size, overlap):
    title = path.stem.strip() or path.name
    title_count = 0
    chunks = []
    for elements, navigation in groups:
        sections, detected = (
            section_map(elements) if path.suffix.lower() == ".epub" else ({}, 0)
        )
        title_count += detected + bool(navigation)
        blocks = chunk_by_title(
            elements,
            max_characters=size,
            overlap=overlap,
            multipage_sections=True,
            include_orig_elements=True,
            combine_text_under_n_chars=0 if path.suffix.lower() == ".epub" else None,
        )
        for block in blocks:
            record = block_record(
                block, title, navigation, sections, path.suffix.lower()
            )
            if record is not None:
                chunks.append(record)
    if not chunks:
        raise ValueError(
            "no extractable text (possibly scanned, empty, encrypted, or corrupt; OCR is not performed)"
        )
    # Count in bounded batches; a successful aggregate count below the limit also
    # bounds each member. Large batches are recursively subdivided.
    checked = []
    for start in range(0, len(chunks), 32):
        checked.extend(
            checked_chunks(client, chunks[start : start + 32], size, overlap)
        )
    attribute_records(checked, path, relative)
    return checked, title_count


def output_is_in_sources(output, report_path, directories):
    return any(
        output == directory
        or directory in output.parents
        or report_path == directory
        or directory in report_path.parents
        for directory in directories
    )


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root", type=Path, default=Path("/home/user/Documents/rome/_raw")
    )
    parser.add_argument("--output", type=Path, required=True, help="NDJSON output path")
    parser.add_argument(
        "--report", type=Path, required=True, help="JSON source-by-source report path"
    )
    parser.add_argument("--chunk-size", type=int, default=1200)
    parser.add_argument("--overlap", type=int, default=180)
    parser.add_argument(
        "--source-timeout",
        type=int,
        default=600,
        help="seconds allowed to partition one source before it is reported as an error",
    )
    args = parser.parse_args()
    if args.chunk_size < 2 or not 0 <= args.overlap < args.chunk_size // 2:
        parser.error(
            "chunk size must be at least 2 and overlap must be nonnegative and less than half the chunk size"
        )
    root = args.source_root.expanduser().resolve()
    directories = [root / "_archived", root / "_extracted"]
    if any(not path.is_dir() for path in directories):
        parser.error(
            f"both _archived and _extracted directories are required under {root}"
        )
    output = args.output.expanduser().resolve()
    report_path = args.report.expanduser().resolve()
    if output == report_path or output_is_in_sources(output, report_path, directories):
        parser.error(
            "output and report must be distinct and outside source directories"
        )
    return args, root, directories, output, report_path


def source_files(directories):
    files = []
    for directory in directories:
        files.extend(
            sorted(
                (
                    p
                    for p in directory.rglob("*")
                    if p.is_file()
                    and p.suffix.lower() in FORMATS
                    and not (
                        directory.name == "_extracted"
                        and p.name.lower() == "readme.txt"
                    )
                ),
                key=lambda p: str(p),
            )
        )
    return files


def extract_clean(path, timeout):
    # Corrupt PDFs can send the pure-Python parser into very long scans.
    def on_timeout(_signum, _frame):
        raise SourceTimeout(f"partitioning exceeded {timeout}s; source skipped")

    signal.signal(signal.SIGALRM, on_timeout)
    signal.alarm(timeout)
    try:
        groups, warning = extract(path)
    finally:
        signal.alarm(0)
    # Only discard margin material that the partitioner explicitly classifies.
    groups = [
        (
            [
                element
                for element in elements
                if element.category not in {"Header", "Footer"}
            ],
            section,
        )
        for elements, section in groups
    ]
    clean = " ".join(
        " ".join(str(element).replace("\x00", " ").split())
        for elements, _section in groups
        for element in elements
    ).strip()
    if not clean:
        raise ValueError(
            "no extractable text (possibly scanned, empty, encrypted, or corrupt; OCR is not performed)"
        )
    return groups, warning, hashlib.sha256(clean.encode("utf-8")).hexdigest()


def add_source_warnings(entry, path, records, titles):
    if path.suffix.lower() == ".epub":
        entry["detected_titles"] = titles
        if not titles:
            entry["section_warning"] = (
                "No unambiguous EPUB navigation or Title elements; chapter breadcrumbs are unavailable"
            )
    if path.suffix.lower() == ".pdf":
        missing_pages = sum(not record["page_numbers"] for record in records)
        if missing_pages:
            entry["page_warning"] = (
                f"{missing_pages} chunks have no original-element page metadata"
            )


def process_source(path, relative, entry, out, args, client, byte_hashes, text_hashes):
    file_hash = digest_file(path)
    entry["sha256"] = file_hash
    if file_hash in byte_hashes:
        entry.update(status="duplicate_bytes", duplicate_of=byte_hashes[file_hash])
        return 0
    groups, warning, text_hash = extract_clean(path, args.source_timeout)
    entry["text_sha256"] = text_hash
    if warning:
        entry["warning"] = warning
    if text_hash in text_hashes:
        byte_hashes[file_hash] = relative
        entry.update(status="duplicate_text", duplicate_of=text_hashes[text_hash])
        return 0
    records, titles = prepare_records(
        path, relative, groups, client, args.chunk_size, args.overlap
    )
    add_source_warnings(entry, path, records, titles)
    for record in records:
        out.write(json.dumps(record, ensure_ascii=False) + "\n")
    out.flush()
    byte_hashes[file_hash] = relative
    text_hashes[text_hash] = relative
    entry.update(status="indexed", chunks=len(records))
    return len(records)


def main():
    args, root, directories, output, report_path = parse_arguments()
    client = tiktoken.get_encoding(ENCODING)
    files = source_files(directories)
    byte_hashes, text_hashes, entries = {}, {}, []
    total_chunks = 0
    # The dashboard treats existence of a valid report as completion.
    report_path.unlink(missing_ok=True)
    with output.open("w", encoding="utf-8") as out:
        for path in files:
            relative = path.relative_to(root).as_posix()
            entry = {"source_path": str(path), "relative_path": relative}
            entries.append(entry)
            try:
                total_chunks += process_source(
                    path, relative, entry, out, args, client, byte_hashes, text_hashes
                )
            except TokenCountError:
                raise
            except (Exception, SourceTimeout) as exc:
                entry.update(status="error", error=str(exc))
                logger.exception("Error processing %s", relative)
    errors = sum(entry["status"] == "error" for entry in entries)
    report = {
        "source_root": str(root),
        "output": str(output),
        "chunk_size": args.chunk_size,
        "overlap": args.overlap,
        "sources": len(entries),
        "chunks": total_chunks,
        "errors": errors,
        "entries": entries,
    }
    with report_path.open("w", encoding="utf-8") as out:
        json.dump(report, out, indent=2, ensure_ascii=False)
        out.write("\n")
    print(
        f"{len(entries)} sources, {total_chunks} chunks, {errors} errors; report: {report_path}",
        file=sys.stderr,
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
