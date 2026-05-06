"""
ingest_textbook.py  —  Smart PDF chunker and ChromaDB ingester for textbooks
Embedding model: hf.co/Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M via Ollama
Verbose logging to console + logs/ingest_TIMESTAMP.log
Safe exit on all errors — never crashes

Usage:
    python ingest_textbook.py --pdf data/books/LinearAlgebra.pdf --collection linear_algebra
    python ingest_textbook.py --pdf data/books/LinearAlgebra.pdf --collection linear_algebra --title "Elementary Linear Algebra 8e" --author "Ron Larson"
    python ingest_textbook.py --list
"""

import argparse
import gc
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).parent.parent

# ── Logging setup ─────────────────────────────────────────────────────────────
os.makedirs(_ROOT / "logs", exist_ok=True)
log_file = str(_ROOT / f"logs/ingest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ],
)
log = logging.getLogger("ingest_textbook")

# ── Config ────────────────────────────────────────────────────────────────────
CHROMA_PATH    = str(_ROOT / "chroma_db")
EMBED_MODEL    = "hf.co/Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M"   # pulled via: ollama pull hf.co/Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M
CHUNK_SIZE     = 800
CHUNK_OVERLAP  = 100
PAGE_BATCH     = 5

SECTION_PATTERNS = [
    r"^(SECTION|Section)\s+\d+[\.\d]*",
    r"^(CHAPTER|Chapter)\s+\d+",
    r"^(Example|EXAMPLE)\s+\d+",
    r"^(Theorem|THEOREM|Definition|DEFINITION|Lemma|LEMMA)\s*\d*",
    r"^\d+\.\d+\s+[A-Z]",
    r"^(Exercise|Problem|EXERCISE|PROBLEM)s?\s+\d+",
]
SECTION_RE = re.compile("|".join(SECTION_PATTERNS), re.MULTILINE)


# ── Qwen3 embedding function ──────────────────────────────────────────────────

class OllamaEmbeddingFunction:
    """
    ChromaDB-compatible embedding function that calls Ollama locally.
    Uses hf.co/Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M for high-quality semantic embeddings.
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def infer_metadata(text: str, page_num: int, book_title: str, author: str, source: str) -> dict:
    """Extract chapter, section, topic, and content-type flags from chunk text."""
    log.debug(f"    [metadata] Inferring metadata for chunk at page {page_num}")
    meta = {
        "page": page_num, "chapter": "", "section": "", "topic": "",
        "is_example": 0, "is_definition": 0, "is_theorem": 0,
        "source": source, "book_title": book_title, "author": author,
    }
    try:
        ch = re.search(r"(CHAPTER|Chapter)\s+(\d+)\s*([\w\s]{0,40})", text)
        if ch:
            meta["chapter"] = f"Chapter {ch.group(2)}"
            meta["topic"]   = ch.group(3).strip()
            log.debug(f"    [metadata] Found chapter: {meta['chapter']}")

        sec = re.search(r"(SECTION|Section)\s+(\d+[\.\d]*)\s*([\w\s]{0,50})", text)
        if sec:
            meta["section"] = f"Section {sec.group(2)}"
            if not meta["topic"]:
                meta["topic"] = sec.group(3).strip()
            log.debug(f"    [metadata] Found section: {meta['section']}")

        nsec = re.search(r"^(\d+\.\d+)\s+([A-Z][A-Za-z\s]{3,40})", text, re.MULTILINE)
        if nsec and not meta["section"]:
            meta["section"] = nsec.group(1)
            meta["topic"]   = nsec.group(2).strip()
            log.debug(f"    [metadata] Found numbered section: {meta['section']} — {meta['topic']}")

        meta["is_example"]    = int(bool(re.search(r"\b(Example|EXAMPLE)\s+\d+", text)))
        meta["is_definition"] = int(bool(re.search(r"\b(Definition|DEFINITION)\b", text)))
        meta["is_theorem"]    = int(bool(re.search(r"\b(Theorem|THEOREM|Lemma|LEMMA)\b", text)))

        flags = []
        if meta["is_example"]:    flags.append("EXAMPLE")
        if meta["is_definition"]: flags.append("DEFINITION")
        if meta["is_theorem"]:    flags.append("THEOREM")
        if flags:
            log.debug(f"    [metadata] Content flags: {', '.join(flags)}")

    except Exception as e:
        log.warning(f"    [metadata] Failed to infer metadata for page {page_num}: {e} — using defaults")

    return meta


def split_segment(segment: str, start_page: int) -> list[dict]:
    """
    Split one segment into CHUNK_SIZE pieces with sentence-boundary preference.
    Guaranteed to terminate — pos strictly advances on every iteration.
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
    """Split text into chunks respecting section/example boundaries."""
    log.debug(f"  [chunker] Chunking {len(text)} characters starting at page {start_page}")
    chunks = []

    try:
        boundaries = [0]
        for m in SECTION_RE.finditer(text):
            boundaries.append(m.start())
        boundaries.append(len(text))
        boundaries = sorted(set(boundaries))
        log.debug(f"  [chunker] Found {len(boundaries)-1} segment(s) between boundaries")

        for i in range(len(boundaries) - 1):
            try:
                segment = text[boundaries[i]:boundaries[i+1]]
                if not segment.strip():
                    log.debug(f"  [chunker] Segment {i}: empty — skipping")
                    continue

                seg_len = len(segment)
                if seg_len <= CHUNK_SIZE:
                    chunks.append({"text": segment.strip(), "page_num": start_page})
                    log.debug(f"  [chunker] Segment {i}: fits whole ({seg_len} chars) → 1 chunk")
                else:
                    log.debug(f"  [chunker] Segment {i}: {seg_len} chars — splitting")
                    sub = split_segment(segment, start_page)
                    log.debug(f"  [chunker] Segment {i}: → {len(sub)} sub-chunks")
                    chunks.extend(sub)

            except Exception as e:
                log.warning(f"  [chunker] Error on segment {i}: {e} — skipping")
                continue

    except Exception as e:
        log.error(f"  [chunker] Fatal error: {e}")
        return []

    log.debug(f"  [chunker] Total chunks produced: {len(chunks)}")
    return chunks


def ingest(pdf_path: str, collection_name: str, book_title: str, author: str):
    log.info(f"{'='*60}")
    log.info("START INGEST")
    log.info(f"  PDF        : {pdf_path}")
    log.info(f"  Collection : {collection_name}")
    log.info(f"  Title      : {book_title or '(not provided)'}")
    log.info(f"  Author     : {author or '(not provided)'}")
    log.info(f"  Embed model: {EMBED_MODEL}")
    log.info(f"  Chunk size : {CHUNK_SIZE} chars  |  Overlap: {CHUNK_OVERLAP}  |  Page batch: {PAGE_BATCH}")
    log.info(f"{'='*60}")

    # ── Step 1: Open PDF ──────────────────────────────────────────────────────
    log.info("[1/5] Opening PDF...")
    try:
        from pypdf import PdfReader
        reader      = PdfReader(pdf_path)
        total_pages = len(reader.pages)
        log.info(f"  PDF opened successfully — {total_pages} pages found")
    except FileNotFoundError:
        log.error(f"  PDF not found: {pdf_path} — check the path and try again")
        sys.exit(1)
    except Exception as e:
        log.error(f"  Failed to open PDF: {e}")
        sys.exit(1)

    # ── Step 2: Connect to ChromaDB ───────────────────────────────────────────
    log.info("[2/5] Connecting to ChromaDB...")
    try:
        import chromadb
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        log.info(f"  Connected to ChromaDB at '{CHROMA_PATH}'")
    except Exception as e:
        log.error(f"  Failed to connect to ChromaDB: {e}")
        sys.exit(1)

    # ── Step 3: Set up collection ─────────────────────────────────────────────
    log.info("[3/5] Setting up collection...")
    try:
        existing = list(client.list_collections())
        log.info(f"  Existing collections: {existing if existing else 'none'}")

        if collection_name in existing:
            log.info(f"  '{collection_name}' exists — deleting for fresh ingest")
            client.delete_collection(collection_name)
            log.info(f"  Deleted '{collection_name}'")

        ef = OllamaEmbeddingFunction(model=EMBED_MODEL)
        log.info(f"  Using OllamaEmbeddingFunction with {EMBED_MODEL}")

        # Verify Ollama + embedding model are reachable before starting
        log.info("  Testing embedding model with a short probe...")
        try:
            test = ef(["test"])
            log.info(f"  Embedding probe OK — vector dimension: {len(test[0])}")
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

    # ── Step 4: Process pages ─────────────────────────────────────────────────
    log.info(f"[4/5] Processing {total_pages} pages in batches of {PAGE_BATCH}...")

    source         = Path(pdf_path).name
    chunk_index    = 0
    total_chunks   = 0
    skipped_pages  = 0
    failed_batches = 0
    carry_text     = ""
    total_batches  = (total_pages + PAGE_BATCH - 1) // PAGE_BATCH

    for batch_start in range(0, total_pages, PAGE_BATCH):
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
                page_text = reader.pages[i].extract_text() or ""
                if not page_text.strip():
                    log.debug(f"    Page {i+1}: empty — skipping")
                    skipped_pages += 1
                    continue
                batch_text += page_text.strip() + "\n\n"
                pages_read += 1
                log.debug(f"    Page {i+1}: {len(page_text)} chars extracted")
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
            log.warning(f"    Batch {batch_num}: 0 chunks produced — skipping")
            carry_text = ""
            continue

        log.debug(f"    {len(chunks)} chunks produced (last held as carry)")
        carry_text    = chunks[-1]["text"] if len(chunks) > 1 else ""
        ingest_chunks = chunks[:-1]        if len(chunks) > 1 else chunks

        if not ingest_chunks:
            log.debug("    Only 1 chunk — deferring to next batch as carry")
            continue

        documents, metadatas, ids = [], [], []
        for c in ingest_chunks:
            try:
                meta = infer_metadata(c["text"], c["page_num"], book_title, author, source)
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

    # ── Step 5: Flush carry ───────────────────────────────────────────────────
    log.info("[5/5] Flushing final carry chunk...")
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
        log.warning(f"  {failed_batches} batch(es) had write errors — review log for details")
    if total_chunks == 0:
        log.error("  No chunks stored. Verify the PDF has selectable text.")


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
                log.warning(f"  {c:<30}  (error reading count: {e})")
    except Exception as e:
        log.error(f"  Failed to connect to ChromaDB: {e}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest a textbook PDF into ChromaDB")
    parser.add_argument("--pdf",        help="Path to PDF file")
    parser.add_argument("--collection", help="ChromaDB collection name")
    parser.add_argument("--title",      default="", help="Book title")
    parser.add_argument("--author",     default="", help="Author")
    parser.add_argument("--list",       action="store_true", help="List all collections")
    args = parser.parse_args()

    log.info(f"ingest_textbook.py started — log: {log_file}")

    if args.list:
        list_collections()
    elif args.pdf and args.collection:
        if not os.path.exists(args.pdf):
            log.error(f"File not found: {args.pdf}")
            sys.exit(1)
        ingest(args.pdf, args.collection, args.title, args.author)
    else:
        log.error("Missing arguments. Use --pdf and --collection, or --list.")
        parser.print_help()
        sys.exit(1)
