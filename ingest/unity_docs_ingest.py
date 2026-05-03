"""
Unity ScriptReference → ChromaDB Ingestion Pipeline
Requirements:
    pip install scrapy beautifulsoup4 chromadb sentence-transformers psutil
"""

import scrapy
from scrapy.crawler import CrawlerProcess
from bs4 import BeautifulSoup
import chromadb
import ollama
import uuid
import json
import os
import sys
import logging
import traceback
import psutil
import re
import requests
from datetime import datetime

# ─────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────

os.makedirs("logs", exist_ok=True)

_log_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
_log_filename = f"logs/unity_ingest_log_{_log_timestamp}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(_log_filename, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)
log.info(f"[Logging] Writing to: {_log_filename}")

# ─────────────────────────────────────────────
# MEMORY SAFETY
# ─────────────────────────────────────────────

MEMORY_LIMIT_PERCENT = 85  # stop if RAM usage exceeds this

def check_memory(context: str = ""):
    """
    Check current RAM usage. Exits the program if usage exceeds the limit.
    Call this at the start of each major operation.
    """
    try:
        mem = psutil.virtual_memory()
        used_pct = mem.percent
        available_mb = mem.available // (1024 * 1024)
        log.info(f"[Memory check{' — ' + context if context else ''}] "
                 f"Used: {used_pct:.1f}% | Available: {available_mb} MB")

        if used_pct >= MEMORY_LIMIT_PERCENT:
            log.critical(
                f"MEMORY LIMIT EXCEEDED ({used_pct:.1f}% >= {MEMORY_LIMIT_PERCENT}%). "
                f"Exiting safely to prevent crash."
            )
            sys.exit(1)

    except Exception as e:
        log.warning(f"Could not read memory stats: {e}")


# ─────────────────────────────────────────────
# UNITY VERSION DETECTION
# ─────────────────────────────────────────────

def get_latest_unity_version() -> str | None:
    """
    Fetch the latest Unity version by:
    1. Getting the sitemap index to find child sitemap URLs
    2. Fetching one child sitemap and parsing the version number from its URLs
       (e.g. https://docs.unity3d.com/2022.3/Documentation/ScriptReference/...)
    Falls back to None if it can't be determined.
    """
    try:
        log.info("[Version] Fetching Unity sitemap index to detect latest version...")
        resp = requests.get("https://docs.unity3d.com/sitemap.xml", timeout=10)
        resp.raise_for_status()

        # Find child sitemap URLs — they look like sitemap-docs-unity3dN.xml
        child_sitemaps = re.findall(
            r'https://docs\.unity3d\.com/sitemap-docs-unity3d\d+\.xml',
            resp.text
        )

        if not child_sitemaps:
            log.warning("[Version] No child sitemaps found in sitemap index.")
            return None

        log.info(f"[Version] Found {len(child_sitemaps)} child sitemaps. Sampling for version numbers...")

        # Fetch a few child sitemaps and collect all version numbers from their URLs
        all_versions = set()
        for sitemap_url in child_sitemaps[:5]:  # sample first 5 to find versions quickly
            try:
                r = requests.get(sitemap_url, timeout=15)
                r.raise_for_status()
                found = re.findall(
                    r'docs\.unity3d\.com/(\d{4}\.\d+)/Documentation/ScriptReference/',
                    r.text
                )
                all_versions.update(found)
                if all_versions:
                    break  # stop as soon as we find at least one version
            except Exception as e:
                log.debug(f"[Version] Could not fetch {sitemap_url}: {e}")
                continue

        if not all_versions:
            log.warning("[Version] No version numbers found in child sitemaps.")
            return None

        # Sort numerically to get the true latest (e.g. 2023.2 > 2022.3)
        sorted_versions = sorted(
            all_versions,
            key=lambda v: tuple(int(x) for x in v.split(".")),
            reverse=True
        )
        latest = sorted_versions[0]
        log.info(f"[Version] Latest Unity version detected: {latest}")
        return latest

    except requests.RequestException as e:
        log.warning(f"[Version] Network error fetching sitemap: {e}")
        return None
    except Exception as e:
        log.warning(f"[Version] Unexpected error detecting version: {e}")
        log.debug(traceback.format_exc())
        return None

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

# Set this to pin a specific Unity version and skip auto-detection.
# Unity 6 LTS is versioned internally as "6000.2" in the docs URLs.
# Leave as None to auto-detect the latest version from the sitemap.
# Examples: "6000.2" (Unity 6 LTS), "2022.3" (Unity 2022 LTS)
UNITY_VERSION_OVERRIDE = "6000.2"
EMBED_MODEL = "hf.co/Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M"


RAW_OUTPUT_FILE = "unity_raw.jsonl"  # stays in project root, matches your existing file

class UnityDocsSpider(scrapy.Spider):
    name = "unity_docs"

    # Start from the sitemap index — this lists every page statically
    # without needing JavaScript to render the class list
    start_urls = ["https://docs.unity3d.com/sitemap.xml"]

    custom_settings = {
        "DOWNLOAD_DELAY": 0.5,
        "AUTOTHROTTLE_ENABLED": True,
        "LOG_LEVEL": "WARNING",
        "FEEDS": {
            RAW_OUTPUT_FILE: {"format": "jsonlines", "overwrite": True}
        },
    }

    allowed_domains = ["docs.unity3d.com"]
    visited = set()

    # Set by main() before the crawler starts
    unity_version: str | None = None

    def parse(self, response):
        """Parse the sitemap XML and queue all ScriptReference URLs."""
        try:
            from scrapy import Selector
            sel = Selector(response, type="xml")
            urls = sel.xpath("//*[local-name()='loc']/text()").getall()

            # Build an allowlist pattern using the detected Unity version.
            # If version detection failed, fall back to matching any versioned
            # Documentation path to avoid scraping nothing.
            if self.unity_version:
                escaped = re.escape(self.unity_version)
                canonical_pattern = re.compile(
                    rf"docs\.unity3d\.com/{escaped}/Documentation/ScriptReference/"
                )
                log.info(f"[Crawler] Filtering to Unity version: {self.unity_version}")
            else:
                # Fallback: accept any canonical versioned English path
                canonical_pattern = re.compile(
                    r"docs\.unity3d\.com/\d{4}\.\d+/Documentation/ScriptReference/"
                )
                log.warning("[Crawler] Version unknown — accepting all versioned English paths.")

            script_ref_urls = [
                u for u in urls
                if u.endswith(".html")
                and canonical_pattern.search(u)
            ]
            log.info(f"[Crawler] Found {len(script_ref_urls)} ScriptReference URLs for version {self.unity_version}.")

            for url in script_ref_urls:
                if url not in self.visited:
                    yield response.follow(url, self.parse_page)

            # Also follow any nested sitemap files
            for url in urls:
                if url.endswith(".xml") and url not in self.visited:
                    self.visited.add(url)
                    yield response.follow(url, self.parse)

        except MemoryError:
            log.critical("[Crawler] MemoryError parsing sitemap. Stopping spider.")
            self.crawler.engine.close_spider(self, "MemoryError")
        except Exception as e:
            log.error(f"[Crawler] Error parsing sitemap {response.url}: {e}")
            log.debug(traceback.format_exc())

    def parse_page(self, response):
        """Scrape a single ScriptReference page."""
        try:
            if response.url in self.visited:
                return
            self.visited.add(response.url)

            if len(self.visited) % 100 == 0:
                check_memory(f"crawler — {len(self.visited)} pages visited")

            log.info(f"[Crawler] Scraped: {response.url}")
            yield {
                "url": response.url,
                "html": response.text,
            }

        except MemoryError:
            log.critical(f"[Crawler] MemoryError on {response.url}. Stopping spider.")
            self.crawler.engine.close_spider(self, "MemoryError")
        except Exception as e:
            log.error(f"[Crawler] Error scraping {response.url}: {e}")
            log.debug(traceback.format_exc())


# ─────────────────────────────────────────────
# STEP 2: BeautifulSoup — clean HTML → plain text
# ─────────────────────────────────────────────

def clean_page(html: str, url: str) -> dict | None:
    """Extract meaningful text from a Unity doc page."""
    try:
        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.find("h1") or soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else url

        content_div = (
            soup.find("div", class_="section")
            or soup.find("div", class_="content")
            or soup.find("article")
        )

        if not content_div:
            log.debug(f"[Cleaner] No content div found for {url} — skipping.")
            return None

        for tag in content_div.find_all(["nav", "footer", "script", "style"]):
            tag.decompose()

        text = content_div.get_text(separator="\n", strip=True)

        if len(text) < 100:
            log.debug(f"[Cleaner] Page too short ({len(text)} chars) for {url} — skipping.")
            return None

        return {"title": title, "url": url, "text": text}

    except MemoryError:
        log.critical(f"[Cleaner] MemoryError while parsing {url}. Exiting.")
        sys.exit(1)

    except Exception as e:
        log.error(f"[Cleaner] Failed to parse {url}: {e}")
        log.debug(traceback.format_exc())
        return None


# ─────────────────────────────────────────────
# STEP 3: Chunking — split long pages into smaller pieces
# ─────────────────────────────────────────────

def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Split text into overlapping word chunks."""
    try:
        words = text.split()
        chunks = []
        start = 0
        while start < len(words):
            end = start + chunk_size
            chunk = " ".join(words[start:end])
            chunks.append(chunk)
            start += chunk_size - overlap

        log.debug(f"[Chunker] Split into {len(chunks)} chunks.")
        return chunks

    except MemoryError:
        log.critical("[Chunker] MemoryError during chunking. Exiting.")
        sys.exit(1)

    except Exception as e:
        log.error(f"[Chunker] Unexpected error: {e}")
        log.debug(traceback.format_exc())
        return []


# ─────────────────────────────────────────────
# STEP 4: ChromaDB — embed and store chunks
# ─────────────────────────────────────────────

CHROMA_DIR = "./chroma_db"  # matches your existing folder
COLLECTION_NAME = "unity_docs"

def ingest_to_chromadb(raw_file: str):
    """Read scraped JSONL, clean, chunk, embed, and insert into ChromaDB."""

    check_memory("before embedding")

    log.info(f"[Ingest] Using Ollama embedding model: {EMBED_MODEL}")
    def embed(text: str) -> list[float]:
        """Embed a single text string via Ollama."""
        try:
            response = ollama.embeddings(model=EMBED_MODEL, prompt=text)
            return response["embedding"]
        except Exception as e:
            log.error(f"[Ingest] Embedding failed: {e}")
            raise

    log.info(f"[Ingest] Connecting to ChromaDB at {CHROMA_DIR}")
    try:
        client = chromadb.PersistentClient(path=CHROMA_DIR)

        # Delete existing collection if present so we start clean each run.
        # This prevents duplicate chunks from accumulating across runs.
        existing = list(client.list_collections())
        if COLLECTION_NAME in existing:
            client.delete_collection(name=COLLECTION_NAME)
            log.info(f"[Ingest] Deleted existing '{COLLECTION_NAME}' collection for fresh start.")

        # Note: we pass embeddings manually so no embedding_function needed here.
        # The embed() function above handles all embedding via Ollama.
        collection = client.create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        log.info(f"[Ingest] Created fresh collection '{COLLECTION_NAME}'.")
    except Exception as e:
        log.critical(f"[Ingest] Failed to connect to ChromaDB: {e}")
        log.debug(traceback.format_exc())
        sys.exit(1)

    total_chunks = 0
    total_pages = 0
    skipped_pages = 0

    log.info(f"[Ingest] Reading from {raw_file}...")
    try:
        with open(raw_file, "r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, start=1):

                # Memory check every 50 pages
                if line_number % 50 == 0:
                    check_memory(f"ingesting page {line_number}")

                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as e:
                    log.warning(f"[Ingest] Skipping malformed JSON on line {line_number}: {e}")
                    skipped_pages += 1
                    continue

                cleaned = clean_page(entry.get("html", ""), entry.get("url", "unknown"))
                if not cleaned:
                    skipped_pages += 1
                    continue

                chunks = chunk_text(cleaned["text"])
                if not chunks:
                    log.warning(f"[Ingest] No chunks produced for {cleaned['url']} — skipping.")
                    skipped_pages += 1
                    continue

                for i, chunk in enumerate(chunks):
                    try:
                        embedding = embed(chunk)
                        collection.add(
                            ids=[str(uuid.uuid4())],
                            embeddings=[embedding],
                            documents=[chunk],
                            metadatas=[{
                                "title": cleaned["title"],
                                "url": cleaned["url"],
                                "chunk_index": i,
                            }]
                        )
                        total_chunks += 1

                    except MemoryError:
                        log.critical(
                            f"[Ingest] MemoryError embedding chunk {i} of {cleaned['url']}. "
                            f"Saved {total_chunks} chunks so far. Exiting."
                        )
                        sys.exit(1)

                    except Exception as e:
                        log.error(
                            f"[Ingest] Failed to embed/store chunk {i} "
                            f"from {cleaned['url']}: {e}"
                        )
                        log.debug(traceback.format_exc())
                        continue  # skip this chunk, keep going

                total_pages += 1
                log.info(f"[Ingest] ✓ [{total_pages}] {cleaned['title']} — {len(chunks)} chunks")

    except FileNotFoundError:
        log.critical(f"[Ingest] Raw file not found: {raw_file}. Did the crawler run?")
        sys.exit(1)

    except MemoryError:
        log.critical(
            f"[Ingest] MemoryError reading {raw_file}. "
            f"Saved {total_chunks} chunks before failure. Exiting."
        )
        sys.exit(1)

    except Exception as e:
        log.critical(f"[Ingest] Unexpected fatal error during ingestion: {e}")
        log.debug(traceback.format_exc())
        sys.exit(1)

    log.info(
        f"\n[Ingest] Complete — {total_pages} pages ingested, "
        f"{skipped_pages} skipped, {total_chunks} total chunks stored in {CHROMA_DIR}"
    )


# ─────────────────────────────────────────────
# STEP 5: Query helper — test your database
# ─────────────────────────────────────────────

def query_docs(question: str, n_results: int = 3):
    """Run a sample query against ChromaDB."""
    log.info(f"[Query] Running query: '{question}'")
    try:
        check_memory("before query")

        client = chromadb.PersistentClient(path=CHROMA_DIR)
        collection = client.get_collection(name=COLLECTION_NAME)

        log.info(f"[Query] Embedding query via Ollama ({EMBED_MODEL})...")
        embedding = ollama.embeddings(model=EMBED_MODEL, prompt=question)["embedding"]
        results = collection.query(query_embeddings=[embedding], n_results=n_results)

        print(f"\nQuery: {question}\n{'─'*50}")
        for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
            print(f"[{meta['title']}] {meta['url']}")
            print(doc[:300])
            print()

    except MemoryError:
        log.critical("[Query] MemoryError during query. Exiting.")
        sys.exit(1)

    except Exception as e:
        log.error(f"[Query] Failed to run query '{question}': {e}")
        log.debug(traceback.format_exc())


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("Unity Docs Ingestion Pipeline — Starting")
    log.info("=" * 60)

    check_memory("startup")

    # 1. Determine Unity version — use override if set, otherwise auto-detect
    if UNITY_VERSION_OVERRIDE:
        unity_version = UNITY_VERSION_OVERRIDE
        log.info(f"[Main] Using pinned Unity version: {unity_version} (set via UNITY_VERSION_OVERRIDE)")
    else:
        log.info("[Main] No version override set — auto-detecting from sitemap...")
        unity_version = get_latest_unity_version()
        if unity_version:
            log.info(f"[Main] Auto-detected Unity version: {unity_version}")
        else:
            log.warning("[Main] Could not detect Unity version — will use fallback pattern.")

    # 2. Crawl
    log.info("[Main] Starting Scrapy crawler...")
    try:
        process = CrawlerProcess()
        process.crawl(UnityDocsSpider, unity_version=unity_version)
        process.start()
        log.info("[Main] Crawler finished.")
    except MemoryError:
        log.critical("[Main] MemoryError during crawl. Exiting.")
        sys.exit(1)
    except Exception as e:
        log.critical(f"[Main] Crawler failed: {e}")
        log.debug(traceback.format_exc())
        sys.exit(1)

    # 3. Ingest
    if os.path.exists(RAW_OUTPUT_FILE):
        log.info(f"[Main] Found {RAW_OUTPUT_FILE}. Starting ingestion...")
        ingest_to_chromadb(RAW_OUTPUT_FILE)
    else:
        log.critical(f"[Main] {RAW_OUTPUT_FILE} not found after crawl. Exiting.")
        sys.exit(1)

    # 4. Test queries
    log.info("[Main] Running test queries...")
    query_docs("How do I use Rigidbody.AddForce?")
    query_docs("What is the difference between Update and FixedUpdate?")

    log.info("[Main] Pipeline complete.")
