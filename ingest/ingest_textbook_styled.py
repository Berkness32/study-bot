"""
ingest_textbook_styled.py — Style-aware PDF chunker and ChromaDB ingester
Primary target: data/textbooks/LinearAlgebra.pdf (Ron Larson, Elementary Linear Algebra)

Body font: 10pt. Chapter headings: 14pt BOLD. Section headings: 11.5pt+ BOLD/ITALIC.
Default args are pre-set for LinearAlgebra — run with no flags to ingest it directly.

Differences from ingest_textbook.py:
  - Uses PyMuPDF (fitz) instead of pypdf for text extraction
  - Detects section boundaries using font size and bold/italic flags
    rather than keyword pattern matching
  - Adds heading_level and is_heading metadata fields to every chunk
  - Everything else — chunking, overlap, embedding, ChromaDB, logging — is identical

Usage:
    # Ingest LinearAlgebra with defaults:
    python ingest_textbook_styled.py

    # Override any defaults:
    python ingest_textbook_styled.py --pdf data/textbooks/LinearAlgebra.pdf --collection linear_algebra

    # Scan fonts before ingesting a new textbook:
    python ingest_textbook_styled.py --scan --pdf data/textbooks/LinearAlgebra.pdf

    python ingest_textbook_styled.py --list

The --scan flag runs a font analysis pass and prints the detected font sizes
without ingesting anything. Use this first when switching to a different textbook.
"""

import argparse
import gc
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# ── Logging setup ─────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
log_file = f"logs/ingest_styled_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ],
)
log = logging.getLogger("ingest_textbook_styled")

# ── Config ────────────────────────────────────────────────────────────────────
CHROMA_PATH   = "chroma_db"
EMBED_MODEL   = "hf.co/Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M"
CHUNK_SIZE    = 800
CHUNK_OVERLAP = 100
PAGE_BATCH    = 5

# ── Font detection thresholds ─────────────────────────────────────────────────
# These control what counts as a heading vs. body text.
# Run --scan on your PDF first to see the actual font sizes used,
# then adjust these if needed.

# A span is treated as a heading candidate if its font size exceeds
# body text size by at least this many points.
HEADING_SIZE_DELTA = 2.5

# If a span is bold or italic AND at least this size, it's a heading candidate
# even if it isn't much larger than body text.
BOLD_ITALIC_MIN_SIZE = 11.5

# Minimum character length for a heading — filters out single letters,
# page numbers, and other short styled fragments.
HEADING_MIN_CHARS = 6

# Maximum character length for a heading — filters out long bold paragraphs
# that are styled for emphasis rather than as structural headings.
HEADING_MAX_CHARS = 120

HEADING_MIN_WORDS = 2

# PyMuPDF font flag bits
FLAG_BOLD   = 2 ** 4   # 16
FLAG_ITALIC = 2 ** 1   # 2


# ── Embedding function ────────────────────────────────────────────────────────

class OllamaEmbeddingFunction:
    """
    ChromaDB-compatible embedding function that calls Ollama locally.
    Identical to ingest_textbook.py.
    """
    def __init__(self, model: str = EMBED_MODEL):
        self.model = model
        log.info(f"  Embedding function initialized — model: {self.model}")

    def __call__(self, input: list[str]) -> list[list[float]]:
        import ollama
        embeddings = []
        for text in input:
            try:
                response = ollama.embeddings(model=self.model, prompt=text)
                embeddings.append(response["embedding"])
            except Exception as e:
                log.error(f"  Embedding failed for text snippet: {e}")
                raise
        return embeddings


# ── Font analysis ─────────────────────────────────────────────────────────────

def get_body_font_size(doc) -> float:
    """
    Estimate the body text font size by finding the most common font size
    across all spans in the first 20 pages. This is used as the baseline
    for detecting headings by size difference.
    """
    from collections import Counter
    size_counts = Counter()

    pages_to_sample = min(20, len(doc))
    for page_num in range(pages_to_sample):
        try:
            page = doc[page_num]
            blocks = page.get_text("dict", flags=0)["blocks"]
            for block in blocks:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        size = round(span.get("size", 0), 1)
                        text = span.get("text", "").strip()
                        if len(text) > 10:
                            size_counts[size] += len(text)
        except Exception:
            continue

    if not size_counts:
        log.warning("  Could not determine body font size — defaulting to 10.0pt")
        return 10.0

    body_size = size_counts.most_common(1)[0][0]
    log.info(f"  Detected body font size: {body_size}pt")
    return body_size


def scan_fonts(pdf_path: str, start_page: int = 0):
    """
    --scan mode: print a font size report for the first 10 pages.
    Use this to verify heading detection before running a full ingest.
    """
    try:
        import fitz
    except ImportError:
        log.error("PyMuPDF not installed. Run: pip install pymupdf")
        sys.exit(1)

    log.info(f"[SCAN] Analysing fonts in: {pdf_path}")
    doc = fitz.open(pdf_path)
    body_size = get_body_font_size(doc)

    print(f"\n{'='*60}")
    print(f"FONT SCAN — {Path(pdf_path).name}")
    print(f"Detected body font size: {body_size}pt")
    print(f"Heading size threshold:  >{body_size + HEADING_SIZE_DELTA}pt")
    print(f"Bold/italic threshold:   >={BOLD_ITALIC_MIN_SIZE}pt + bold or italic flag")
    print(f"{'='*60}\n")

    pages_to_show = min(start_page + 10, len(doc))
    for page_num in range(start_page, pages_to_show):
        page = doc[page_num]
        blocks = page.get_text("dict", flags=0)["blocks"]
        print(f"── Page {page_num + 1} ──")
        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text  = span.get("text", "").strip()
                    size  = span.get("size", 0)
                    flags = span.get("flags", 0)
                    bold   = bool(flags & FLAG_BOLD)
                    italic = bool(flags & FLAG_ITALIC)

                    if not text or len(text) < HEADING_MIN_CHARS:
                        continue

                    is_heading = (
                        size > body_size + HEADING_SIZE_DELTA
                        or (size >= BOLD_ITALIC_MIN_SIZE and (bold or italic))
                    )

                    if is_heading and len(text) <= HEADING_MAX_CHARS and len(text.split()) >= HEADING_MIN_WORDS:
                        style = []
                        if bold:   style.append("BOLD")
                        if italic: style.append("ITALIC")
                        style_str = f"[{'+'.join(style)}]" if style else ""
                        print(f"  HEADING {size:.1f}pt {style_str}: {text[:80]}")

    doc.close()
    print(f"\nScan complete. Adjust HEADING_SIZE_DELTA or BOLD_ITALIC_MIN_SIZE")
    print(f"in the config section if the wrong spans are being detected as headings.")


# ── Style-aware page extraction ───────────────────────────────────────────────

def extract_page_with_structure(page, body_size: float) -> list[dict]:
    """
    Extract text from one PyMuPDF page as a list of span dicts:
        {text, size, bold, italic, is_heading, heading_level}

    heading_level:
        1 = chapter-level  (large font, typically > body + 4pt)
        2 = section-level  (medium font, or bold/italic)
        0 = body text
    """
    spans_out = []
    try:
        blocks = page.get_text("dict", flags=0)["blocks"]
        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text  = span.get("text", "").strip()
                    size  = span.get("size", 0)
                    flags = span.get("flags", 0)
                    bold   = bool(flags & FLAG_BOLD)
                    italic = bool(flags & FLAG_ITALIC)

                    if not text:
                        continue

                    # Classify this span
                    is_large   = size > body_size + HEADING_SIZE_DELTA
                    is_styled  = (bold or italic) and size >= BOLD_ITALIC_MIN_SIZE
                    word_count = len(text.split())
                    is_heading = (
                        (is_large or is_styled)
                        and (HEADING_MIN_CHARS <= len(text) <= HEADING_MAX_CHARS)
                        and word_count >= HEADING_MIN_WORDS
                    )

                    heading_level = 0
                    if is_heading:
                        if size > body_size + 4.0:
                            heading_level = 1   # chapter-level
                        else:
                            heading_level = 2   # section/subsection-level

                    spans_out.append({
                        "text":          text,
                        "size":          size,
                        "bold":          bold,
                        "italic":        italic,
                        "is_heading":    is_heading,
                        "heading_level": heading_level,
                    })
    except Exception as e:
        log.warning(f"    Span extraction error: {e}")

    return spans_out


def spans_to_structured_text(spans: list[dict]) -> str:
    """
    Convert a list of span dicts into a single string with heading markers
    injected so the downstream chunker can find boundaries.

    Heading markers look like:
        <<H1>> Chapter 3 Introduction to Cryptography
        <<H2>> 3.1 Symmetric Encryption
    """
    lines = []
    prev_was_heading = False

    for span in spans:
        text = span["text"]
        if span["is_heading"]:
            marker = "<<H1>>" if span["heading_level"] == 1 else "<<H2>>"
            if not prev_was_heading:
                lines.append("")   # blank line before heading
            lines.append(f"{marker} {text}")
            prev_was_heading = True
        else:
            if prev_was_heading:
                lines.append("")   # blank line after heading
            lines.append(text)
            prev_was_heading = False

    return "\n".join(lines)


# ── Chunking ──────────────────────────────────────────────────────────────────

# Heading marker pattern — used as chunk boundaries
HEADING_RE = re.compile(r"^<<H[12]>>", re.MULTILINE)


def split_segment(segment: str, start_page: int) -> list[dict]:
    """
    Split one segment into CHUNK_SIZE pieces with sentence-boundary preference.
    Identical to ingest_textbook.py.
    """
    chunks = []
    pos    = 0
    length = len(segment)

    while pos < length:
        end = min(pos + CHUNK_SIZE, length)

        if end < length:
            sb = segment.rfind(". ", pos, end)
            if sb > pos + CHUNK_SIZE // 2:
                end = sb + 1
                log.debug(f"  [chunker]   Sentence boundary split at pos={sb}")
            else:
                log.debug(f"  [chunker]   No sentence boundary — hard split at char {end}")

        piece = segment[pos:end].strip()
        if piece:
            chunks.append({"text": piece, "page_num": start_page})

        next_pos = end - CHUNK_OVERLAP
        if next_pos <= pos:
            next_pos = end
        pos = next_pos

    return chunks


def chunk_text(text: str, start_page: int) -> list[dict]:
    """
    Split structured text into chunks using <<H1>> / <<H2>> markers as
    boundaries, then fall back to size-based splitting within large segments.
    """
    log.debug(f"  [chunker] Chunking {len(text)} chars from page {start_page}")
    chunks = []

    try:
        KEYWORD_RE = re.compile(
            r"^(Definition|Example\s+\d+[\.\d]*|Theorem\s+\d+[\.\d]*|Lemma\s+\d+[\.\d]*|"
            r"Proof\.?|Remark\.?|Corollary\s+\d+[\.\d]*|Exercise\s+\d+[\.\d]*|"
            r"Application|Summary|Properties of|Exercises)",
            re.MULTILINE
        )
        boundaries = [0]
        for m in HEADING_RE.finditer(text):
            boundaries.append(m.start())
        for m in KEYWORD_RE.finditer(text):
            boundaries.append(m.start())
        boundaries.append(len(text))
        boundaries = sorted(set(boundaries))
        log.debug(f"  [chunker] Found {len(boundaries)-1} segment(s)")

        for i in range(len(boundaries) - 1):
            try:
                segment = text[boundaries[i]:boundaries[i+1]]
                # Strip the heading marker from the text stored in ChromaDB
                segment = re.sub(r"^<<H[12]>>\s*", "", segment).strip()
                if not segment:
                    continue

                if len(segment) <= CHUNK_SIZE:
                    chunks.append({"text": segment, "page_num": start_page})
                    log.debug(f"  [chunker] Segment {i}: whole ({len(segment)} chars)")
                else:
                    sub = split_segment(segment, start_page)
                    log.debug(f"  [chunker] Segment {i}: split → {len(sub)} chunks")
                    chunks.extend(sub)

            except Exception as e:
                log.warning(f"  [chunker] Segment {i} error: {e} — skipping")
                continue

    except Exception as e:
        log.error(f"  [chunker] Fatal: {e}")
        return []

    log.debug(f"  [chunker] Total chunks: {len(chunks)}")
    return chunks


# ── Metadata inference ────────────────────────────────────────────────────────

def infer_metadata(text: str, page_num: int, book_title: str, author: str,
                   source: str, is_heading: bool = False,
                   heading_level: int = 0) -> dict:
    """
    Extract chapter, section, topic, and content-type flags from chunk text.
    Extended from ingest_textbook.py to include heading_level and is_heading.
    """
    log.debug(f"    [metadata] Inferring metadata for chunk at page {page_num}")
    meta = {
        "page":          page_num,
        "chapter":       "",
        "section":       "",
        "topic":         "",
        "is_example":    0,
        "is_definition": 0,
        "is_theorem":    0,
        "is_heading":    int(is_heading),
        "heading_level": heading_level,
        "source":        source,
        "book_title":    book_title,
        "author":        author,
    }
    try:
        ch = re.search(r"(CHAPTER|Chapter)\s+(\d+)\s*([\w\s]{0,40})", text)
        if ch:
            meta["chapter"] = f"Chapter {ch.group(2)}"
            meta["topic"]   = ch.group(3).strip()

        sec = re.search(r"(SECTION|Section)\s+(\d+[\.\d]*)\s*([\w\s]{0,50})", text)
        if sec:
            meta["section"] = f"Section {sec.group(2)}"
            if not meta["topic"]:
                meta["topic"] = sec.group(3).strip()

        nsec = re.search(r"^(\d+\.\d+)\s+([A-Z][A-Za-z\s]{3,40})", text, re.MULTILINE)
        if nsec and not meta["section"]:
            meta["section"] = nsec.group(1)
            meta["topic"]   = nsec.group(2).strip()

        # If the chunk starts with a heading-like line, use it as the topic
        first_line = text.splitlines()[0].strip() if text.strip() else ""
        if not meta["topic"] and len(first_line) > 4 and len(first_line) < 80:
            meta["topic"] = first_line

        meta["is_example"]    = int(bool(re.search(r"\b(Example|EXAMPLE)\s*\d*", text)))
        meta["is_definition"] = int(bool(re.search(r"\b(Definition|DEFINITION)\b", text)))
        meta["is_theorem"]    = int(bool(re.search(r"\b(Theorem|THEOREM|Lemma|LEMMA)\b", text)))

    except Exception as e:
        log.warning(f"    [metadata] Failed at page {page_num}: {e} — using defaults")

    return meta


# ── Main ingest ───────────────────────────────────────────────────────────────

def ingest(pdf_path: str, collection_name: str, book_title: str, author: str, start_page: int = 0):
    try:
        import fitz
    except ImportError:
        log.error("PyMuPDF not installed. Run: pip install pymupdf")
        sys.exit(1)

    log.info(f"{'='*60}")
    log.info("START INGEST (style-aware)")
    log.info(f"  PDF        : {pdf_path}")
    log.info(f"  Collection : {collection_name}")
    log.info(f"  Title      : {book_title or '(not provided)'}")
    log.info(f"  Author     : {author or '(not provided)'}")
    log.info(f"  Embed model: {EMBED_MODEL}")
    log.info(f"  Chunk size : {CHUNK_SIZE} chars  |  Overlap: {CHUNK_OVERLAP}  |  Page batch: {PAGE_BATCH}")
    log.info(f"{'='*60}")

    # ── Step 1: Open PDF ──────────────────────────────────────────────────────
    log.info("[1/6] Opening PDF with PyMuPDF...")
    try:
        doc         = fitz.open(pdf_path)
        total_pages = len(doc)
        log.info(f"  PDF opened — {total_pages} pages")
    except FileNotFoundError:
        log.error(f"  PDF not found: {pdf_path}")
        sys.exit(1)
    except Exception as e:
        log.error(f"  Failed to open PDF: {e}")
        sys.exit(1)

    # ── Step 2: Detect body font size ─────────────────────────────────────────
    log.info("[2/6] Analysing font sizes...")
    body_size = get_body_font_size(doc)
    log.info(f"  Body size: {body_size}pt  |  Heading threshold: >{body_size + HEADING_SIZE_DELTA}pt")

    # ── Step 3: Connect to ChromaDB ───────────────────────────────────────────
    log.info("[3/6] Connecting to ChromaDB...")
    try:
        import chromadb
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        log.info(f"  Connected at '{CHROMA_PATH}'")
    except Exception as e:
        log.error(f"  ChromaDB connection failed: {e}")
        sys.exit(1)

    # ── Step 4: Set up collection ─────────────────────────────────────────────
    log.info("[4/6] Setting up collection...")
    try:
        existing = list(client.list_collections())
        log.info(f"  Existing collections: {existing if existing else 'none'}")

        if collection_name in existing:
            log.info(f"  '{collection_name}' exists — deleting for fresh ingest")
            client.delete_collection(collection_name)

        ef = OllamaEmbeddingFunction(model=EMBED_MODEL)

        log.info("  Testing embedding model with a short probe...")
        try:
            test = ef(["test"])
            log.info(f"  Embedding probe OK — vector dim: {len(test[0])}")
        except Exception as e:
            log.error(f"  Embedding probe failed: {e}")
            log.error(f"  Make sure Ollama is running and '{EMBED_MODEL}' is pulled.")
            log.error(f"  Run: ollama pull {EMBED_MODEL}")
            sys.exit(1)

        collection = client.create_collection(
            name=collection_name,
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},
        )
        log.info(f"  Collection '{collection_name}' created with cosine similarity")
    except SystemExit:
        raise
    except Exception as e:
        log.error(f"  Failed to set up collection: {e}")
        sys.exit(1)

    # ── Step 5: Process pages in batches ──────────────────────────────────────
    log.info(f"[5/6] Processing {total_pages} pages in batches of {PAGE_BATCH}...")

    source         = Path(pdf_path).name
    chunk_index    = 0
    total_chunks   = 0
    skipped_pages  = 0
    failed_batches = 0
    carry_text     = ""
    total_batches  = (total_pages + PAGE_BATCH - 1) // PAGE_BATCH

    for batch_start in range(start_page, total_pages, PAGE_BATCH):
        batch_end  = min(batch_start + PAGE_BATCH, total_pages)
        batch_num  = batch_start // PAGE_BATCH + 1
        first_page = batch_start + 1

        log.info(f"  [batch {batch_num}/{total_batches}] Pages {batch_start+1}–{batch_end}")

        batch_text = carry_text
        if carry_text:
            log.debug(f"    Carrying {len(carry_text)} chars from previous batch")

        pages_read = 0
        for i in range(batch_start, batch_end):
            try:
                page  = doc[i]
                spans = extract_page_with_structure(page, body_size)
                if not spans:
                    log.debug(f"    Page {i+1}: no spans — skipping")
                    skipped_pages += 1
                    continue

                page_text = spans_to_structured_text(spans)
                if not page_text.strip():
                    log.debug(f"    Page {i+1}: empty after conversion — skipping")
                    skipped_pages += 1
                    continue

                batch_text += page_text + "\n\n"
                pages_read += 1
                log.debug(f"    Page {i+1}: {len(page_text)} chars, "
                          f"{sum(1 for s in spans if s['is_heading'])} headings detected")

            except Exception as e:
                log.warning(f"    Page {i+1}: extraction failed ({e}) — skipping")
                skipped_pages += 1
                continue

        log.debug(f"    Batch total: {len(batch_text)} chars from {pages_read} pages")

        if not batch_text.strip():
            log.warning(f"    Batch {batch_num}: no text — skipping")
            carry_text = ""
            continue

        try:
            chunks = chunk_text(batch_text, first_page)
        except Exception as e:
            log.error(f"    Batch {batch_num}: chunking failed ({e}) — skipping")
            failed_batches += 1
            carry_text = ""
            continue

        if not chunks:
            log.warning(f"    Batch {batch_num}: 0 chunks — skipping")
            carry_text = ""
            continue

        log.debug(f"    {len(chunks)} chunks (last held as carry)")
        carry_text    = chunks[-1]["text"] if len(chunks) > 1 else ""
        ingest_chunks = chunks[:-1]        if len(chunks) > 1 else chunks

        if not ingest_chunks:
            log.debug("    Only 1 chunk — deferring to next batch")
            continue

        documents, metadatas, ids = [], [], []
        for c in ingest_chunks:
            try:
                meta = infer_metadata(
                    c["text"], c["page_num"], book_title, author, source
                )
                meta["chunk_index"] = chunk_index
                documents.append(c["text"])
                metadatas.append(meta)
                ids.append(f"{Path(pdf_path).stem}_chunk_{chunk_index:05d}")
                chunk_index += 1
            except Exception as e:
                log.warning(f"    Chunk {chunk_index}: metadata error ({e}) — skipping")
                continue

        if documents:
            try:
                collection.add(documents=documents, metadatas=metadatas, ids=ids)
                total_chunks += len(documents)
                log.info(f"    → Stored {len(documents)} chunks  (running total: {total_chunks})")
            except Exception as e:
                log.error(f"    Batch {batch_num}: ChromaDB write failed ({e}) — batch lost")
                failed_batches += 1

        del documents, metadatas, ids, chunks, batch_text
        gc.collect()
        log.debug(f"    Memory freed after batch {batch_num}")

    # ── Step 6: Flush carry ───────────────────────────────────────────────────
    log.info("[6/6] Flushing final carry chunk...")
    if carry_text.strip():
        try:
            meta = infer_metadata(carry_text, total_pages, book_title, author, source)
            meta["chunk_index"] = chunk_index
            collection.add(
                documents=[carry_text],
                metadatas=[meta],
                ids=[f"{Path(pdf_path).stem}_chunk_{chunk_index:05d}"],
            )
            total_chunks += 1
            log.info("  Final carry chunk stored")
        except Exception as e:
            log.warning(f"  Failed to store final carry chunk: {e}")
    else:
        log.info("  No carry chunk to flush")

    doc.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    log.info(f"{'='*60}")
    log.info("INGEST COMPLETE")
    log.info(f"  Total chunks stored : {total_chunks}")
    log.info(f"  Pages skipped       : {skipped_pages}")
    log.info(f"  Failed batches      : {failed_batches}")
    log.info(f"  Embed model         : {EMBED_MODEL}")
    log.info(f"  Collection          : {collection_name}")
    log.info(f"  Log saved to        : {log_file}")
    log.info(f"{'='*60}")

    if failed_batches > 0:
        log.warning(f"  {failed_batches} batch(es) had write errors — review log")
    if total_chunks == 0:
        log.error("  No chunks stored. Run --scan to check font detection.")


# ── List collections ──────────────────────────────────────────────────────────

def list_collections():
    log.info("Listing all ChromaDB collections...")
    try:
        import chromadb
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        cols   = client.list_collections()
        if not cols:
            log.info("  No collections found.")
            return
        log.info(f"  {'Collection':<30} {'Chunks':>8}")
        log.info(f"  {'-'*40}")
        for c in cols:
            try:
                col = client.get_collection(c)
                log.info(f"  {c:<30} {col.count():>8}")
            except Exception as e:
                log.warning(f"  {c:<30}  (error: {e})")
    except Exception as e:
        log.error(f"  Failed to connect to ChromaDB: {e}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    DEFAULT_PDF        = "data/textbooks/LinearAlgebra.pdf"
    DEFAULT_COLLECTION = "linear_algebra"
    DEFAULT_TITLE      = "Elementary Linear Algebra"
    DEFAULT_AUTHOR     = "Ron Larson"
    DEFAULT_START_PAGE = 11   # pages 1-10 are front matter / TOC / index

    parser = argparse.ArgumentParser(
        description="Style-aware PDF ingester using PyMuPDF font detection",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--pdf",        default=DEFAULT_PDF,        help="Path to PDF file")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION, help="ChromaDB collection name")
    parser.add_argument("--title",      default=DEFAULT_TITLE,      help="Book title")
    parser.add_argument("--author",     default=DEFAULT_AUTHOR,     help="Author")
    parser.add_argument("--start-page", type=int, default=DEFAULT_START_PAGE,
                        help="First page to ingest (1-indexed). Skips front matter / TOC.")
    parser.add_argument("--list",       action="store_true", help="List all collections and exit")
    parser.add_argument("--scan",       action="store_true",
                        help="Scan font sizes and print heading detection report — no ingest")
    args = parser.parse_args()

    log.info(f"ingest_textbook_styled.py started — log: {log_file}")

    if args.list:
        list_collections()
    elif args.scan:
        if not os.path.exists(args.pdf):
            log.error(f"File not found: {args.pdf}")
            sys.exit(1)
        scan_fonts(args.pdf, start_page=args.start_page - 1)
    else:
        if not os.path.exists(args.pdf):
            log.error(f"File not found: {args.pdf}")
            sys.exit(1)
        ingest(args.pdf, args.collection, args.title, args.author, start_page=args.start_page - 1)
