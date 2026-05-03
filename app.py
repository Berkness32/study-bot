"""
app.py — Study Bot
Run with: streamlit run app.py
Embedding model: hf.co/Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M (must match ingest_textbook.py)
"""

import streamlit as st
import streamlit.components.v1 as components
import chromadb
from chromadb.utils import embedding_functions
import ollama
import re

from app.agent import run_agent
from app.memory import build_history_with_memory, format_summary_as_context

import yaml
from pathlib import Path

def load_config():
    config_path = Path("config/config.yaml")
    with open(config_path) as f:
        return yaml.safe_load(f)

CONFIG   = load_config()
PROFILES = CONFIG["profiles"]
EMBED_MODEL = CONFIG["models"]["embed"]

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Study Bot",
    page_icon="📐",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:ital,wght@0,300;0,400;0,600;1,300&display=swap');

html, body, [class*="css"] { font-family: 'IBM Plex Sans', sans-serif; }
.stApp { background-color: #0f0f0f; color: #e8e8e0; }
[data-testid="stSidebar"] { background-color: #141414; border-right: 1px solid #2a2a2a; }

.profile-active {
    background-color: #1e1e1e;
    border: 1px solid #c8b560;
    border-radius: 4px;
    padding: 0.5rem 0.75rem;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.85rem;
    color: #c8b560;
    margin-bottom: 0.25rem;
}
.app-header {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 1.1rem; color: #555;
    letter-spacing: 0.05em; margin-bottom: 0;
}
.app-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 2rem; font-weight: 600;
    color: #e8e8e0; letter-spacing: -0.02em; margin-top: 0;
}
.profile-badge {
    display: inline-block;
    background-color: #1e1e1e;
    border: 1px solid #c8b560;
    border-radius: 3px;
    padding: 0.2rem 0.6rem;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.78rem; color: #c8b560;
    letter-spacing: 0.05em; text-transform: uppercase;
}
.user-msg {
    background-color: #1a1a1a;
    border-left: 3px solid #c8b560;
    border-radius: 0 6px 6px 0;
    padding: 0.75rem 1rem; margin: 0.5rem 0;
}
hr { border-color: #1e1e1e; }
</style>
""", unsafe_allow_html=True)

# ── Embedding model (must match ingest_textbook.py) ───────────────────────────
class OllamaEmbeddingFunction(chromadb.EmbeddingFunction):
    def __init__(self, model: str = EMBED_MODEL):
        self.model = model

    def __call__(self, input: chromadb.Documents) -> chromadb.Embeddings:
        embeddings = []
        for text in input:
            try:
                response = ollama.embeddings(model=self.model, prompt=text)
                embeddings.append(response["embedding"])
            except Exception as e:
                raise RuntimeError(f"Embedding failed ({self.model}): {e}")
        return embeddings

# ── ChromaDB ──────────────────────────────────────────────────────────────────
@st.cache_resource
def get_chroma_client():
    return chromadb.PersistentClient(path="chroma_db")

@st.cache_resource
def get_embedding_function():
    return OllamaEmbeddingFunction(model=EMBED_MODEL)

def get_collection(name):
    try:
        return get_chroma_client().get_collection(name, embedding_function=get_embedding_function())
    except Exception as e:
        st.error(f"Could not load collection '{name}': {e}")
        return None

def retrieve(query, collections, n_results=6, score_threshold=0.45,
             filter_meta=None):
    """
    Query ChromaDB collections with relevance filtering and optional
    metadata filters e.g. filter_meta={"chapter": "3"}.
    """
    all_results = []
    for col_name in collections:
        col = get_collection(col_name)
        if col is None:
            continue
        try:
            kwargs = dict(query_texts=[query], n_results=min(n_results, col.count()))
            if filter_meta:
                kwargs["where"] = filter_meta
            results = col.query(**kwargs)
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                score = round(1 - dist, 3)
                if score >= score_threshold:
                    all_results.append({
                        "text":     doc,
                        "metadata": meta,
                        "score":    score,
                        "collection": col_name,
                    })
        except Exception as e:
            st.warning(f"Retrieval error from '{col_name}': {e}")

    all_results.sort(key=lambda x: x["score"], reverse=True)
    return all_results[:n_results]

def format_context(chunks):
    if not chunks:
        return ""
    parts = ["--- RETRIEVED TEXTBOOK PASSAGES ---"]
    for i, chunk in enumerate(chunks):
        meta = chunk["metadata"]
        loc  = f"Page {meta.get('page','?')}"
        if meta.get("section"): loc += f" | {meta['section']}"
        if meta.get("topic"):   loc += f" | {meta['topic']}"
        parts.append(f"\n[Passage {i+1} — {loc}]\n{chunk['text']}")
    parts.append("--- END OF RETRIEVED PASSAGES ---")
    return "\n".join(parts)

# ── LaTeX + markdown renderer ─────────────────────────────────────────────────
def normalize_latex(text: str) -> str:
    """Normalize all LaTeX delimiter styles to $ and $$ for Streamlit/MathJax."""
    text = re.sub(r'\\\[(.*?)\\\]', r'$$\1$$', text, flags=re.DOTALL)
    text = re.sub(r'\\\((.*?)\\\)', r'$\1$',   text, flags=re.DOTALL)
    return text

def render_response(text: str):
    """
    Render a model response with full LaTeX, bold, italic, and code support.
    Uses MathJax via st.components for reliable inline + display math rendering.
    """
    text = normalize_latex(text)

    # Triple backtick code blocks (must come before single backtick handling)
    def replace_code_block(m):
        lang = m.group(1) or ""
        code = m.group(2)
        lang_class = f"language-{lang}" if lang else ""
        return (
            f'<pre style="background:#282c34;border-radius:6px;'
            f'padding:1rem;overflow-x:auto;margin:0.75rem 0;">'
            f'<code class="{lang_class}" style="font-family:IBM Plex Mono,monospace;'
            f'font-size:0.85em;white-space:pre;">{code}</code></pre>'
        )
    text = re.sub(r'```(\w+)?\n?(.*?)```', replace_code_block, text, flags=re.DOTALL)

    def replace_inline_code_block(m):
        lang = m.group(1) or ""
        code = m.group(2)
        lang_class = f"language-{lang}" if lang else ""
        return (
            f'<pre style="background:#282c34;border-radius:6px;'
            f'padding:1rem;overflow-x:auto;margin:0.75rem 0;">'
            f'<code class="{lang_class}" style="font-family:IBM Plex Mono,monospace;'
            f'font-size:0.85em;white-space:pre;">{code}</code></pre>'
        )
    text = re.sub(r'`(csharp|python|bash|js|cpp|c)([^`]+)`', replace_inline_code_block, text, flags=re.DOTALL)

    # Markdown → HTML conversions
    text = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', text)
    ...
    text = re.sub(r'\*(.*?)\*',     r'<em>\1</em>',         text)
    text = re.sub(r'^### (.+)$', r'<h3 style="color:#e8e8e0;font-family:IBM Plex Mono,monospace;margin:1rem 0 0.5rem;">\1</h3>', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.+)$',  r'<h2 style="color:#e8e8e0;font-family:IBM Plex Mono,monospace;margin:1rem 0 0.5rem;">\1</h2>', text, flags=re.MULTILINE)
    text = re.sub(r'^# (.+)$',   r'<h1 style="color:#e8e8e0;font-family:IBM Plex Mono,monospace;margin:1rem 0 0.5rem;">\1</h1>', text, flags=re.MULTILINE)
    text = re.sub(
        r'`(.*?)`',
        r'<code style="font-family:IBM Plex Mono,monospace;background:#1e1e1e;'
        r'padding:0.1rem 0.3rem;border-radius:3px;font-size:0.9em;">\1</code>',
        text
    )
    # Replace newlines everywhere EXCEPT inside <pre> blocks
    parts = re.split(r'(<pre.*?</pre>)', text, flags=re.DOTALL)
    processed_parts = []
    for part in parts:
        if part.startswith('<pre'):
            processed_parts.append(part)  # leave code blocks untouched
        else:
            processed_parts.append(part.replace('\n\n', '<br><br>').replace('\n', '<br>'))
    text = ''.join(processed_parts)

    mathjax_html = f"""
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/csharp.min.js"></script>
    <script>
    window.MathJax = {{
        tex: {{
            inlineMath:  [['$', '$']],
            displayMath: [['$$', '$$']],
            processEscapes: true
        }},
        svg: {{ fontCache: 'global' }}
    }};
    </script>
    <script>document.addEventListener('DOMContentLoaded', () => hljs.highlightAll());</script>
    <script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
    <div style="
        color: #e8e8e0;
        font-family: 'IBM Plex Sans', sans-serif;
        font-size: 1rem;
        line-height: 1.8;
        padding: 0.25rem 0;
    ">
    {text}
    </div>
    """
    components.html(mathjax_html, height=600, scrolling=True)

# ── Ollama streaming ──────────────────────────────────────────────────────────
def stream_response(messages):
    try:
        stream = ollama.chat(
            model="qwen3:8b",
            messages=messages,
            stream=True,
        )
        for chunk in stream:
            yield chunk["message"]["content"]
    except Exception as e:
        yield f"\n\n⚠️ Model error: {e}\n\nMake sure Ollama is running."

# ── Session state ─────────────────────────────────────────────────────────────
if "active_profile" not in st.session_state:
    st.session_state.active_profile = "Math Tutor"
if "chat_histories" not in st.session_state:
    st.session_state.chat_histories = {p: [] for p in PROFILES}
if "summaries" not in st.session_state:
    st.session_state.summaries = {p: "" for p in PROFILES}

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 📚 Study Bot")
    st.markdown("---")
    st.markdown("**Profile**")
    for profile_name, profile in PROFILES.items():
        if profile_name == st.session_state.active_profile:
            st.markdown(f'<div class="profile-active">{profile["icon"]} {profile_name}</div>', unsafe_allow_html=True)
        else:
            if st.button(f'{profile["icon"]} {profile_name}', key=f"btn_{profile_name}"):
                st.session_state.active_profile = profile_name
                st.rerun()

    st.markdown("---")
    active = PROFILES[st.session_state.active_profile]
    st.markdown("**Collections**")
    if active["collections"]:
        # Initialize toggle state for this profile
        toggle_key = f"active_collections_{st.session_state.active_profile}"
        if toggle_key not in st.session_state:
            st.session_state[toggle_key] = {c: True for c in active["collections"]}

        for col_name in active["collections"]:
            col   = get_collection(col_name)
            count = col.count() if col else 0
            enabled = st.toggle(
                f"📄 {col_name} · {count} chunks",
                value=st.session_state[toggle_key].get(col_name, True),
                key=f"toggle_{st.session_state.active_profile}_{col_name}",
            )
            st.session_state[toggle_key][col_name] = enabled
    else:
        st.caption("No collections for this profile yet.")

    st.markdown("---")
    st.markdown("**Agent settings**")
    thinking_mode = st.toggle("🧠 Thinking mode", value=False,
        help="Enables deeper reasoning. Slower but better for hard problems.")
    st.markdown("---")
    # Vision upload — Math Tutor only
    uploaded_image = None
    if st.session_state.active_profile == "Math Tutor":
        st.markdown("**📷 Handwritten math**")
        uploaded_image = st.file_uploader(
            "Upload a photo of your work",
            help="LLaVA will transcribe your handwriting and check for errors. (jpg, jpeg, png, webp)",
        )
        st.markdown("---")
    if st.button("🗑 Clear chat"):
        st.session_state.chat_histories[st.session_state.active_profile] = []
        st.rerun()
    st.markdown("---")
    st.caption(f"Chat model : qwen3:8b")
    st.caption(f"Embed model: {EMBED_MODEL}")
    st.caption("DB: ChromaDB (local)")

# ── Main area ─────────────────────────────────────────────────────────────────
profile      = PROFILES[st.session_state.active_profile]
chat_history = st.session_state.chat_histories[st.session_state.active_profile]

col1, col2 = st.columns([5, 1])
with col1:
    st.markdown('<p class="app-header">local · private · offline</p>', unsafe_allow_html=True)
    st.markdown('<p class="app-title">Study Bot</p>', unsafe_allow_html=True)
with col2:
    st.markdown("<br><br>", unsafe_allow_html=True)
    st.markdown(f'<div class="profile-badge">{profile["icon"]} {st.session_state.active_profile}</div>', unsafe_allow_html=True)

st.markdown("---")

# Render chat history
for msg in chat_history:
    if msg["role"] == "user":
        st.markdown(f'<div class="user-msg">🧑 {msg["content"]}</div>', unsafe_allow_html=True)
    else:
        with st.container(border=True):
            render_response(msg["content"])

# Chat input
user_input = st.chat_input(profile["placeholder"])

if user_input:
    st.markdown(f'<div class="user-msg">🧑 {user_input}</div>', unsafe_allow_html=True)

    # Memory: trim history if needed
    summary = st.session_state.summaries[st.session_state.active_profile]
    chat_history, summary = build_history_with_memory(chat_history, summary)
    st.session_state.summaries[st.session_state.active_profile] = summary
    # Vision: handle uploaded image
    image_transcription = None
    if uploaded_image is not None:
        import tempfile, os
        allowed = {".jpg", ".jpeg", ".png", ".webp"}
        ext = Path(uploaded_image.name).suffix.lower()
        if ext not in allowed:
            st.error(f"Unsupported file type: {ext}. Please upload a jpg, png, or webp.")
            uploaded_image = None
        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(uploaded_image.name).suffix) as tmp:
            tmp.write(uploaded_image.read())
            tmp_path = tmp.name
        with st.spinner("📷 Transcribing handwritten work..."):
            from app.vision import transcribe_image
            image_transcription = transcribe_image(tmp_path)
        os.unlink(tmp_path)

        st.markdown("**📝 Transcription:**")
        with st.expander("View transcribed work", expanded=True):
            st.markdown(image_transcription)
    # RAG retrieval
    sources = []
    if profile["collections"]:
        toggle_key = f"active_collections_{st.session_state.active_profile}"
        enabled_collections = [
            c for c in profile["collections"]
            if st.session_state.get(toggle_key, {}).get(c, True)
        ]
        with st.spinner("Searching textbook..."):
            sources = retrieve(
                user_input,
                enabled_collections,
                n_results=CONFIG["retrieval"]["n_results"],
                score_threshold=CONFIG["retrieval"]["score_threshold"],
            )

    # Build context string
    context_parts = []
    if summary:
        context_parts.append(format_summary_as_context(summary))
    if sources:
        context_parts.append(format_context(sources))
    full_context = "\n\n".join(context_parts)

    # Run ReAct agent
    with st.spinner("Thinking..."):
        # Augment user input with transcription if image was uploaded
        augmented_input = user_input
        if image_transcription:
            augmented_input = (
                f"{user_input}\n\n"
                f"[Student's handwritten work — transcribed by LLaVA]\n"
                f"{image_transcription}\n"
                f"[End of transcription]\n\n"
                f"Please identify any errors in the student's work and show the correct solution."
            )

        result = run_agent(
            user_input=augmented_input,
            history=chat_history,
            system=profile["system_prompt"],
            context=full_context,
            thinking=thinking_mode,
        )

    full_response = result["response"]
    trace = result["trace"]

    # Show reasoning trace
    if trace:
        with st.expander("🔍 Reasoning trace", expanded=False):
            for step in trace:
                if step["type"] == "act":
                    st.markdown(f"**⚡ Act** — `{step['tool']}`")
                    st.code(str(step["input"]), language="python")
                elif step["type"] == "observe":
                    st.markdown(f"**👁 Observe** — `{step['tool']}`")
                    st.code(str(step["result"]), language="text")
                elif step["type"] == "think":
                    st.markdown(f"**💭 Think**")
                    st.markdown(step["content"][:500])

    # Render response
    with st.container(border=True):
        render_response(full_response)

    # Sources expander
    if sources:
        with st.expander(f"📖 {len(sources)} source(s) retrieved from textbook"):
            for i, src in enumerate(sources):
                meta      = src["metadata"]
                loc_parts = [f"Page {meta.get('page','?')}"]
                if meta.get("section"): loc_parts.append(meta["section"])
                if meta.get("topic"):   loc_parts.append(meta["topic"])
                st.markdown(f"**[{i+1}]** `{src.get('collection','?')}` · `{' · '.join(loc_parts)}` — relevance: `{src['score']}`")
                st.markdown(f"> {src['text'][:400]}{'...' if len(src['text']) > 400 else ''}")
                if i < len(sources) - 1:
                    st.markdown("---")

    # Save to history
    chat_history.append({"role": "user",      "content": user_input})
    chat_history.append({"role": "assistant", "content": full_response})
    st.session_state.chat_histories[st.session_state.active_profile] = chat_history
