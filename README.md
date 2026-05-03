# Study Bot

A local AI assistant with two modes: an intelligent math tutor with RAG-powered textbook access, and an automated job application agent. Everything runs locally via [Ollama](https://ollama.com) — no API keys required for LLM inference.

---

## Table of Contents

- [Overview](#overview)
- [Study Bot](#study-bot)
  - [Profiles](#profiles)
  - [How It Works](#how-it-works)
  - [Math Tools](#math-tools)
  - [Vision / Handwriting](#vision--handwriting)
  - [Memory Management](#memory-management)
- [Job Apps Agent](#job-apps-agent)
  - [Workflow](#workflow)
  - [Resume Component Library](#resume-component-library)
- [Ingestion](#ingestion)
- [Setup](#setup)
- [Running the App](#running-the-app)
- [Project Structure](#project-structure)

---

## Overview

| Mode | Entry Point | Purpose |
|---|---|---|
| Study Bot | `app.py` (Streamlit) | RAG-powered tutor with math and vision tools |
| Job Apps Agent | `job_agent.py` | Scrape job postings, tailor resume + cover letter |

---

## Study Bot

Launch the Streamlit interface and select a profile. The bot retrieves relevant passages from your embedded textbooks, then uses a ReAct (Reason + Act) loop to answer questions — calling math tools as needed before responding.

### Profiles

Three profiles are defined in `config/config.yaml`. Each has a distinct system prompt and queries different ChromaDB collections.

| Profile | Collections | Focus |
|---|---|---|
| Math Tutor | `linear_algebra`, `number_theory` | Theorem proofs, worked examples, LaTeX-formatted answers |
| Game Dev | `linear_algebra`, `unity_docs` | Unity C# patterns, game math, engine APIs |
| Job Apps | *(none — see Job Apps Agent)* | Resume tailoring, cover letters, interview prep |

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

Four tools are registered in `app/tools.py` and called by the agent as needed. They are tried in priority order with automatic fallback:

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

Upload a photo of handwritten math work. The vision module (`app/vision.py`) uses LLaVA:13b to:

- **Transcribe** — convert handwritten work to LaTeX, preserving any errors exactly as written
- **Check errors** — compare the transcription against a correct solution and list specific mistakes (algebraic errors, sign errors, arithmetic mistakes) step by step

### Memory Management

Long study sessions are automatically summarized to prevent context overflow (`app/memory.py`):

- Triggers when history exceeds 10 messages
- Summarizes older turns into a compact 3–5 sentence block covering topics, problems solved, key results, and misconceptions corrected
- Keeps only the last 4 messages (2 exchanges) alongside the summary
- Summary is injected as context at the start of each new agent call

---

## Job Apps Agent

`job_agent.py` is a standalone script that automates resume and cover letter tailoring for a specific job posting. It pauses at each step for your approval before proceeding.

### Workflow

```
1. Scrape job posting  →  Playwright fetches the URL, LLM extracts structured JSON
                          {job_title, company, summary, requirements, responsibilities}

2. Select components   →  LLM reads components.yaml and picks matching skills,
                          project bullets, and cover letter paragraphs

3. Generate documents  →  Builds resume.docx and cover_letter.docx via python-docx

4. Log actions         →  Saves timestamped log to logs/actions_log.json
```

Output files are written to `data/job-apps/output/`.

All experience and projects are always included in the resume — the agent only selects the order of skill bullets and cover letter paragraphs to maximize relevance.

### Resume Component Library

`data/job-apps/components.yaml` is the source of truth for all resume content. It contains:

- **Personal info** — name, contact, portfolio, LinkedIn
- **Skills** — 11 tagged categories (coding languages, backend, networking, math, data, leadership, etc.)
- **Certifications** — CompTIA A+, AWS Cloud Practitioner
- **Education** — CSU Monterey Bay (CS), UC Santa Barbara (Film & Media)
- **Experience** — Jobs with individually tagged bullets for role-specific selection
- **Projects** — Portfolio projects with descriptions
- **Cover letter paragraphs** — Pre-written opening, closing, and role-specific blocks

Each item carries tags (e.g., `software_engineer`, `it`, `tutoring`, `admin`) so the agent knows what to include for a given role.

---

## Ingestion

Textbooks and documentation are chunked, embedded, and stored in ChromaDB before the study bot can reference them. Run from the project root:

```bash
./index.sh
```

This presents a menu of available ingest scripts:

| Script | Purpose |
|---|---|
| `ingest/ingest_textbook.py` | Chunk and embed a PDF textbook (keyword-based section detection) |
| `ingest/ingest_textbook_styled.py` | Same pipeline, but uses font size and bold/italic flags for section detection (better for typeset PDFs) |
| `ingest/unity_docs_ingest.py` | Crawl Unity ScriptReference and embed into `unity_docs` collection |

**Textbook ingest options:**

| Flag | Description |
|---|---|
| `--pdf` | Path to PDF (relative to project root) |
| `--collection` | ChromaDB collection name |
| `--title` | Book title (added to chunk metadata) |
| `--author` | Author name (added to chunk metadata) |
| `--start-page` | Skip front matter / TOC (1-indexed) |
| `--list` | List all existing collections and exit |

Chunk size is 800 tokens with 100-token overlap. Logs are written to `logs/`.

---

## Setup

**Requirements:** Python 3.11+, [Ollama](https://ollama.com) running locally, Wolfram Engine (optional — SymPy is the fallback).

```bash
# Clone and enter project
cd study-bot

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers (for job agent)
playwright install chromium

# Pull required Ollama models
ollama pull qwen3:8b
ollama pull hf.co/Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M
ollama pull llava:13b          # only needed for vision/handwriting feature
```

Place PDF textbooks in `data/textbooks/`, then run `./index.sh` to ingest them before using the study bot.

---

## Running the App

**Study Bot (Streamlit UI):**
```bash
source .venv/bin/activate
streamlit run app.py
```

**Job Apps Agent:**
```bash
source .venv/bin/activate
python job_agent.py
```

**Ingest a textbook or docs:**
```bash
./index.sh
```

---

## Project Structure

```
study-bot/
├── app.py                        # Streamlit UI — main entry point for study bot
├── job_agent.py                  # Job application automation agent
├── index.sh                      # Ingest script selector
│
├── app/
│   ├── agent.py                  # LangGraph ReAct agent (think → act → observe)
│   ├── tools.py                  # Math tools: Wolfram, SymPy, NumPy, SciPy
│   ├── math_engine.py            # Math computation layer with fallback chain
│   ├── memory.py                 # Conversation summarization / context management
│   └── vision.py                 # Handwriting transcription and error detection (LLaVA)
│
├── ingest/
│   ├── ingest_textbook.py        # PDF → ChromaDB (keyword section detection)
│   ├── ingest_textbook_styled.py # PDF → ChromaDB (font/style section detection)
│   └── unity_docs_ingest.py      # Unity ScriptReference crawler → ChromaDB
│
├── config/
│   └── config.yaml               # Models, profiles, retrieval settings
│
├── data/
│   ├── textbooks/                # Source PDFs for ingestion
│   └── job-apps/
│       ├── components.yaml       # Resume component library
│       └── output/               # Generated resumes and cover letters
│
├── chroma_db/                    # Persistent vector store
├── logs/                         # Ingest and agent action logs
└── requirements.txt
```
