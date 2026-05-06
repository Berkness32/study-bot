# Agents

A local AI assistant monorepo with two independent agents. Everything runs locally via [Ollama](https://ollama.com) — no API keys required for LLM inference.

---

## Table of Contents

- [Overview](#overview)
- [Study Bot](#study-bot)
  - [Profiles](#profiles)
  - [How It Works](#how-it-works)
  - [Math Tools](#math-tools)
  - [Vision / Handwriting](#vision--handwriting)
  - [Memory Management](#memory-management)
- [Job Agent](#job-agent)
  - [Workflow](#workflow)
  - [Resume Component Library](#resume-component-library)
- [Ingestion](#ingestion)
- [Setup](#setup)
- [Running the Agents](#running-the-agents)
- [Project Structure](#project-structure)

---

## Overview

| Agent | Entry Point | Purpose |
|---|---|---|
| Study Bot | `agents/study-bot/app.py` (Streamlit) | RAG-powered tutor with math and vision tools |
| Job Agent | `agents/job-agent/job_agent.py` | Scrape job postings, tailor resume + cover letter, auto-fill ATS forms |

---

## Study Bot

Launch the Streamlit interface and select a profile. The bot retrieves relevant passages from your embedded textbooks, then uses a ReAct (Reason + Act) loop to answer questions — calling math tools as needed before responding.

### Profiles

Three profiles are defined in `agents/study-bot/config/config.yaml`. Each has a distinct system prompt and queries different ChromaDB collections.

| Profile | Collections | Focus |
|---|---|---|
| Math Tutor | `linear_algebra`, `number_theory` | Theorem proofs, worked examples, LaTeX-formatted answers |
| Game Dev | `linear_algebra`, `unity_docs` | Unity C# patterns, game math, engine APIs |
| Job Apps | *(none)* | Resume tailoring, cover letters, interview prep |

### How It Works

```
User query
  → Retrieve relevant passages from ChromaDB (top 6, score ≥ 0.45)
  → Inject context + conversation history into agent
  → ReAct loop (Qwen3:8b via Ollama):
      Think → call math tool if needed → Observe result → iterate
  → Render response (LaTeX via MathJax, code blocks, markdown)
```

The agent uses [LangGraph](https://github.com/langchain-ai/langgraph) to manage the think/act/observe cycle. Each tool call and its result are captured in a reasoning trace visible in the UI.

**Models used:**

| Role | Model |
|---|---|
| Chat / reasoning | `qwen3:8b` |
| Embeddings | `Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M` |
| Vision | `llava:13b` |

### Math Tools

Four tools are registered in `agents/study-bot/app/tools.py` and called by the agent as needed. They are tried in priority order with automatic fallback:

1. **Wolfram** (`wolfram_compute`) — primary engine via `wolframclient`. Handles algebra, calculus, linear algebra, number theory, ODEs, matrix operations, eigenvalues, integration, LaTeX output.
2. **SymPy** (`sympy_compute`) — symbolic Python fallback. Same capabilities for most operations.
3. **NumPy** (`numpy_compute`) — numerical linear algebra fallback.
4. **SciPy** (`scipy_compute`) — advanced numerical decompositions (LU, SVD, eigenvalues) and linear system solving.

Example Wolfram expressions the agent will generate:

```wolfram
Eigenvalues[{{2, 1}, {1, 2}}]
Integrate[x^2 Sin[x], {x, 0, Pi}]
Factor[x^3 - 6x^2 + 11x - 6]
DSolve[y'[x] == y[x], y[x], x]
```

Results are returned as LaTeX strings and rendered in the UI with MathJax.

### Vision / Handwriting

Upload a photo of handwritten math work. The vision module (`agents/study-bot/app/vision.py`) uses LLaVA:13b to:

- **Transcribe** — convert handwritten work to LaTeX, preserving any errors exactly as written
- **Check errors** — compare the transcription against a correct solution and list specific mistakes (algebraic errors, sign errors, arithmetic mistakes) step by step

### Memory Management

Long study sessions are automatically summarized to prevent context overflow (`agents/study-bot/app/memory.py`):

- Triggers when history exceeds 10 messages
- Summarizes older turns into a compact 3–5 sentence block covering topics, problems solved, key results, and misconceptions corrected
- Keeps only the last 4 messages (2 exchanges) alongside the summary
- Summary is injected as context at the start of each new agent call

---

## Job Agent

`agents/job-agent/job_agent.py` is a standalone CLI script that automates job applications end-to-end. It pauses at each step for your approval before proceeding.

### Workflow

```
1. Board / URL     →  Browse builtin.com, governmentjobs.com, indeed.com, or paste a direct URL

2. Scrape posting  →  Playwright fetches the page, LLM extracts structured JSON
                      {job_title, company, summary, requirements, responsibilities, pay, address}

3. Select content  →  LLM reads components.yaml and picks matching skills,
                      experience bullets, and cover letter paragraphs

4. Generate docs   →  Builds resume.docx and cover_letter.docx via python-docx
                      Auto-trims to 2 pages if needed

5. Fill ATS form   →  Playwright fills application fields page-by-page
                      Supported: Workday, Greenhouse, Lever, iCIMS, generic fallback

6. Log outcome     →  Saves to applications.db (SQLite) and logs/actions_log.json
```

Output documents are written to `agents/job-agent/data/job-apps/output/`. View logged applications any time:

```bash
node agents/job-agent/view_applications.js
```

### Resume Component Library

`agents/job-agent/data/job-apps/components.yaml` is the source of truth for all resume content:

- **Personal info** — name, contact, portfolio, LinkedIn
- **Skills** — 11 tagged categories (coding languages, backend, networking, math, data, leadership, etc.)
- **Certifications** — CompTIA A+, AWS Cloud Practitioner
- **Education** — CSU Monterey Bay (CS), UC Santa Barbara (Film & Media)
- **Experience** — Jobs with individually tagged bullets for role-specific selection
- **Projects** — Portfolio projects with descriptions and links
- **Cover letter paragraphs** — Pre-written opening, closing, and role-specific body blocks

Each item carries tags (e.g., `software_engineer`, `it`, `networking`, `admin`) so the LLM knows what to include for a given role.

---

## Ingestion

Textbooks and documentation are chunked, embedded, and stored in ChromaDB before the study bot can reference them.

```bash
bash agents/study-bot/index.sh
```

This presents a menu of available ingest scripts:

| Script | Purpose |
|---|---|
| `ingest_textbook.py` | Chunk and embed a PDF textbook (keyword-based section detection) |
| `ingest_textbook_styled.py` | Same pipeline, but uses font size and bold/italic flags for section detection (better for typeset PDFs) |
| `unity_docs_ingest.py` | Crawl Unity ScriptReference and embed into `unity_docs` collection |

**Textbook ingest flags:**

| Flag | Description |
|---|---|
| `--pdf` | Path to PDF |
| `--collection` | ChromaDB collection name |
| `--title` | Book title (added to chunk metadata) |
| `--author` | Author name (added to chunk metadata) |
| `--start-page` | Skip front matter / TOC (1-indexed) |
| `--list` | List all existing collections and exit |

Chunk size is 800 tokens with 100-token overlap. Logs are written to `agents/study-bot/logs/`.

Place PDF textbooks in `agents/study-bot/data/textbooks/` before ingesting.

---

## Setup

**Requirements:** Python 3.11+, [Ollama](https://ollama.com) running locally, Node.js (for `view_applications.js`), Wolfram Engine (optional — SymPy is the fallback).

```bash
# Create virtual environment at repo root
python -m venv .venv
source .venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browsers (for job agent)
playwright install chromium

# Install Node dependencies (for view_applications.js)
cd agents/job-agent && npm install && cd ../..

# Pull required Ollama models
ollama pull qwen3:8b
ollama pull hf.co/Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M
ollama pull llava:13b          # only needed for vision/handwriting feature
```

---

## Running the Agents

**Study Bot (Streamlit UI):**
```bash
source .venv/bin/activate
streamlit run agents/study-bot/app.py
```

**Job Agent (CLI):**
```bash
source .venv/bin/activate
python agents/job-agent/job_agent.py

# Go straight to a specific board:
python agents/job-agent/job_agent.py --board builtin
python agents/job-agent/job_agent.py --board governmentjobs
python agents/job-agent/job_agent.py --board indeed

# Skip board picker, apply directly from a URL:
python agents/job-agent/job_agent.py --url "https://..."
```

**View logged applications:**
```bash
node agents/job-agent/view_applications.js
```

**Ingest a textbook or docs:**
```bash
bash agents/study-bot/index.sh
```

---

## Project Structure

```
agents/                               # repo root (directory may be named differently)
├── requirements.txt
│
├── agents/
│   ├── study-bot/
│   │   ├── app.py                    # Streamlit UI — main entry point
│   │   ├── index.sh                  # Ingest script selector
│   │   ├── test_wolfram.py           # Wolfram engine connectivity test
│   │   │
│   │   ├── app/
│   │   │   ├── agent.py              # LangGraph ReAct agent (think → act → observe)
│   │   │   ├── tools.py              # Math tools: Wolfram, SymPy, NumPy, SciPy
│   │   │   ├── math_engine.py        # Math computation layer with fallback chain
│   │   │   ├── memory.py             # Conversation summarization / context management
│   │   │   └── vision.py             # Handwriting transcription via LLaVA
│   │   │
│   │   ├── ingest/
│   │   │   ├── ingest_textbook.py    # PDF → ChromaDB (keyword section detection)
│   │   │   ├── ingest_textbook_styled.py  # PDF → ChromaDB (font/style detection)
│   │   │   └── unity_docs_ingest.py  # Unity ScriptReference crawler → ChromaDB
│   │   │
│   │   ├── config/
│   │   │   └── config.yaml           # Models, profiles, retrieval settings
│   │   │
│   │   ├── data/
│   │   │   └── textbooks/            # Source PDFs for ingestion
│   │   │
│   │   ├── chroma_db/                # Persistent vector store (gitignored)
│   │   └── logs/                     # Ingest logs (gitignored)
│   │
│   └── job-agent/
│       ├── job_agent.py              # CLI entry point — full application pipeline
│       ├── view_applications.js      # Terminal viewer for applications.db
│       ├── _inspect_workday.py       # Workday form structure inspector
│       ├── package.json
│       │
│       ├── job_agent_support/
│       │   ├── db.py                 # SQLite helpers (applications, dead_listings)
│       │   ├── boards/
│       │   │   ├── builtin.py        # builtin.com scraper
│       │   │   ├── indeed.py         # indeed.com scraper
│       │   │   └── governmentjobs.py # governmentjobs.com scraper
│       │   └── ats/
│       │       ├── workday.py        # Workday ATS form filler
│       │       ├── greenhouse.py     # Greenhouse ATS form filler
│       │       ├── lever.py          # Lever ATS form filler
│       │       └── icims.py          # iCIMS ATS form filler
│       │
│       ├── data/job-apps/
│       │   ├── applications.db       # SQLite log of all applications
│       │   ├── components.yaml       # Resume component library (gitignored)
│       │   ├── workday_components.yaml  # Workday-specific field mappings (gitignored)
│       │   ├── Cover_Letters/        # Cover letter templates (.docx)
│       │   ├── MasterDocs/           # Master resume and cover letter (.docx)
│       │   └── output/               # Generated resumes and cover letters (gitignored)
│       │
│       ├── logs/
│       │   ├── actions_log.json      # Timestamped action log (gitignored)
│       │   └── job_agents_logs/      # Per-session ATS logs (gitignored)
│       │
│       └── node_modules/             # gitignored
```
