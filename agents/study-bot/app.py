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
    with open(Path(__file__).parent / "config/config.yaml") as f:
        return yaml.safe_load(f)

CONFIG      = load_config()
PROFILES    = CONFIG["profiles"]
EMBED_MODEL = CONFIG["models"]["embed"]

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Study Bot",
    page_icon="📐",
    layout="centered",
    initial_sidebar_state="expanded",
)

# ── CSS — Claude.ai dark theme ────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    font-size: 15px;
}

.stApp { background-color: #0d0d0d; color: #ececec; }

/* Hide Streamlit chrome */
#MainMenu, footer { visibility: hidden; }
header[data-testid="stHeader"] { background: transparent; }

/* Main content area */
.main .block-container {
    padding-top: 2rem;
    padding-bottom: 6rem;
    max-width: 760px;
}

/* Sidebar */
[data-testid="stSidebar"] {
    background-color: #111111 !important;
    border-right: 1px solid #222222;
}
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] .stMarkdownContainer { color: #9a9a9a; }
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3 { color: #c8c8c8; }

/* Chat messages — no background, no border */
[data-testid="stChatMessage"] {
    background-color: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0.75rem 0 !important;
    gap: 1rem !important;
}

/* User message content — subtle tint */
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"] > div {
    background-color: #1a1a1a;
    border-radius: 12px;
    padding: 0.75rem 1rem;
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) .stMarkdownContainer p {
    color: #d4d4d4;
}

/* Assistant message content — no background */
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) [data-testid="stChatMessageContent"] {
    background-color: transparent;
}

/* Avatars */
[data-testid="stChatMessageAvatarUser"] {
    background-color: #2a2a2a !important;
    color: #9a9a9a !important;
}
[data-testid="stChatMessageAvatarAssistant"] {
    background-color: #cc785c !important;
    color: #ffffff !important;
}

/* Chat input */
[data-testid="stChatInputContainer"] {
    background-color: #0d0d0d !important;
    border-top: 1px solid #1e1e1e !important;
    padding: 1rem 0 !important;
}
[data-testid="stChatInput"] {
    background-color: #1a1a1a !important;
    border: 1px solid #333333 !important;
    border-radius: 12px !important;
    color: #ececec !important;
}
[data-testid="stChatInput"]:focus-within {
    border-color: #555555 !important;
    box-shadow: 0 0 0 1px #444444 !important;
}
[data-testid="stChatInput"] textarea {
    color: #ececec !important;
    font-family: inherit !important;
    font-size: 0.95rem !important;
}
[data-testid="stChatInput"] textarea::placeholder { color: #555555 !important; }

/* Buttons */
.stButton > button {
    background-color: #1a1a1a !important;
    border: 1px solid #2e2e2e !important;
    color: #9a9a9a !important;
    border-radius: 6px !important;
    font-size: 0.88rem !important;
    font-family: inherit !important;
    width: 100% !important;
    text-align: left !important;
    justify-content: flex-start !important;
}
.stButton > button:hover {
    background-color: #222222 !important;
    border-color: #3e3e3e !important;
    color: #d4d4d4 !important;
}

/* Toggles */
[data-testid="stToggle"] label { color: #9a9a9a !important; font-size: 0.9rem; }

/* Expanders */
[data-testid="stExpander"] {
    border: 1px solid #222222 !important;
    border-radius: 8px !important;
    background-color: #111111 !important;
    overflow: hidden;
}
[data-testid="stExpander"] summary {
    color: #6b6b6b !important;
    font-size: 0.82rem !important;
    padding: 0.5rem 0.75rem !important;
}
[data-testid="stExpander"] summary:hover { color: #9a9a9a !important; }

/* Expander code blocks inside trace */
[data-testid="stExpander"] .stCode {
    background-color: #161616 !important;
    border: 1px solid #2a2a2a !important;
    border-radius: 6px !important;
}

/* Dividers */
hr { border-color: #1e1e1e !important; margin: 0.5rem 0 !important; }

/* Caption / small text */
.stCaption, [data-testid="stCaptionContainer"] { color: #555555 !important; font-size: 0.78rem; }

/* Alerts / error */
[data-testid="stAlert"] {
    background-color: #1e1010 !important;
    border: 1px solid #3e1e1e !important;
    color: #e88 !important;
    border-radius: 6px !important;
}

/* Spinner */
[data-testid="stSpinner"] { color: #555555 !important; }

/* App title */
.app-title {
    font-size: 1.4rem;
    font-weight: 600;
    color: #ececec;
    letter-spacing: -0.01em;
    margin: 0;
}
.app-subtitle {
    font-size: 0.78rem;
    color: #3e3e3e;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    margin: 0 0 1.5rem;
}
.profile-badge {
    display: inline-block;
    background-color: #1a1a1a;
    border: 1px solid #2e2e2e;
    border-radius: 999px;
    padding: 0.2rem 0.75rem;
    font-size: 0.78rem;
    color: #666666;
    letter-spacing: 0.03em;
}
.sidebar-section { color: #555555; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em; margin: 1rem 0 0.4rem; }
.profile-active {
    background-color: #1e1e1e;
    border: 1px solid #444444;
    border-radius: 6px;
    padding: 0.4rem 0.75rem;
    font-size: 0.88rem;
    color: #c8c8c8;
    margin-bottom: 0.25rem;
    box-shadow: 0 0 0 1px rgba(255,255,255,0.12), 0 0 10px rgba(255,255,255,0.06);
    box-sizing: border-box;
    width: 100%;
    display: block;
}
</style>
""", unsafe_allow_html=True)

# ── Embedding model ───────────────────────────────────────────────────────────
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
    return chromadb.PersistentClient(path=str(Path(__file__).parent / "chroma_db"))

@st.cache_resource
def get_embedding_function():
    return OllamaEmbeddingFunction(model=EMBED_MODEL)

def get_collection(name):
    try:
        return get_chroma_client().get_collection(name, embedding_function=get_embedding_function())
    except Exception as e:
        st.error(f"Could not load collection '{name}': {e}")
        return None

def retrieve(query, collections, n_results=8, score_threshold=0.3, filter_meta=None):
    """
    Returns up to n_results chunks above score_threshold, sorted by score.
    Use score_threshold=0.3 so display always has candidates; callers filter
    to >= 0.45 for the agent context to keep quality high.
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
                        "text":       doc,
                        "metadata":   meta,
                        "score":      score,
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

# ── Renderer ──────────────────────────────────────────────────────────────────
def normalize_latex(text: str) -> str:
    text = re.sub(r'\\\[(.*?)\\\]', r'$$\1$$', text, flags=re.DOTALL)
    text = re.sub(r'\\\((.*?)\\\)', r'$\1$',   text, flags=re.DOTALL)
    return text

def prettify_collection(name: str) -> str:
    return name.replace("_", " ").title()

def build_sources_html(sources: list) -> str:
    if not sources:
        return ""
    seen = set()
    items = []
    for src in sources[:5]:
        meta    = src["metadata"]
        page    = meta.get("page", "?")
        section = meta.get("section", "")
        col     = prettify_collection(src.get("collection", ""))
        key     = (page, section, col)
        if key in seen:
            continue
        seen.add(key)
        label = f"Page {page}"
        if section:
            label += f" &middot; {section}"
        if col:
            label += f" &middot; {col}"
        items.append(
            f'<li style="color:#606060;font-size:0.82em;margin:0.2rem 0;'
            f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',system-ui,sans-serif;">'
            f'{label}</li>'
        )
    items_html = "\n".join(items)
    return f"""
<div style="margin-top:1.5rem;padding-top:1rem;border-top:1px solid #1e1e1e;">
  <p style="color:#3e3e3e;font-size:0.72em;text-transform:uppercase;letter-spacing:0.08em;
            margin:0 0 0.4rem;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
            font-weight:500;">Sources</p>
  <ul style="margin:0;padding-left:1.1rem;list-style-type:disc;">
    {items_html}
  </ul>
</div>
"""

def estimate_height(raw_text: str, sources: list = None) -> int:
    """Estimate iframe height from the original (pre-HTML) text."""
    code_blocks  = re.findall(r'```[\s\S]*?```', raw_text)
    clean        = re.sub(r'```[\s\S]*?```', '', raw_text)
    text_lines   = clean.count('\n') + 1
    code_lines   = sum(cb.count('\n') for cb in code_blocks)

    display_blocks = re.findall(r'\$\$[\s\S]*?\$\$', raw_text)
    display_count  = len(display_blocks)
    # Each \\ inside a display block is a matrix row — much taller than regular math
    matrix_rows    = sum(b.count(r'\\') for b in display_blocks)

    source_lines = (len(sources) + 4) if sources else 0

    height = (
        text_lines   * 28 +
        code_lines   * 22 +
        display_count * 60 +
        matrix_rows  * 38 +
        source_lines * 24 +
        180
    )
    return max(300, height)

def render_response(text: str, sources: list = None):
    raw_text = text  # saved before transformation for accurate height estimation
    text = normalize_latex(text)

    def replace_code_block(m):
        lang = m.group(1) or ""
        code = m.group(2)
        lang_class = f"language-{lang}" if lang else ""
        return (
            f'<pre style="background:#161616;border:1px solid #2a2a2a;border-radius:8px;'
            f'padding:1rem;overflow-x:auto;margin:0.75rem 0;">'
            f'<code class="{lang_class}" style="font-family:\'Fira Code\',\'Cascadia Code\','
            f'Consolas,monospace;font-size:0.84em;white-space:pre;">{code}</code></pre>'
        )
    text = re.sub(r'```(\w+)?\n?(.*?)```', replace_code_block, text, flags=re.DOTALL)

    def replace_inline_lang_block(m):
        lang = m.group(1) or ""
        code = m.group(2)
        lang_class = f"language-{lang}" if lang else ""
        return (
            f'<pre style="background:#161616;border:1px solid #2a2a2a;border-radius:8px;'
            f'padding:1rem;overflow-x:auto;margin:0.75rem 0;">'
            f'<code class="{lang_class}" style="font-family:\'Fira Code\',\'Cascadia Code\','
            f'Consolas,monospace;font-size:0.84em;white-space:pre;">{code}</code></pre>'
        )
    text = re.sub(r'`(csharp|python|bash|js|cpp|c)([^`]+)`', replace_inline_lang_block, text, flags=re.DOTALL)

    text = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.*?)\*',     r'<em>\1</em>',         text)
    text = re.sub(r'^### (.+)$', r'<h3 style="color:#ececec;font-weight:600;font-size:1em;margin:1.25rem 0 0.4rem;">\1</h3>', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.+)$',  r'<h2 style="color:#ececec;font-weight:600;font-size:1.1em;margin:1.25rem 0 0.4rem;">\1</h2>', text, flags=re.MULTILINE)
    text = re.sub(r'^# (.+)$',   r'<h1 style="color:#ececec;font-weight:600;font-size:1.2em;margin:1.25rem 0 0.4rem;">\1</h1>', text, flags=re.MULTILINE)
    text = re.sub(
        r'`(.*?)`',
        r'<code style="font-family:\'Fira Code\',Consolas,monospace;background:#1e1e1e;'
        r'border:1px solid #2a2a2a;padding:0.1rem 0.35rem;border-radius:4px;font-size:0.85em;">\1</code>',
        text
    )

    parts = re.split(r'(<pre.*?</pre>)', text, flags=re.DOTALL)
    processed = []
    for part in parts:
        if part.startswith('<pre'):
            processed.append(part)
        else:
            processed.append(part.replace('\n\n', '<br><br>').replace('\n', '<br>'))
    text = ''.join(processed)

    sources_html = build_sources_html(sources or [])
    height = estimate_height(raw_text, sources)

    html = f"""
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css">
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
    document.addEventListener('DOMContentLoaded', function() {{ hljs.highlightAll(); }});
    </script>
    <script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
    <div style="
        color: #d4d4d4;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
        font-size: 0.95rem;
        line-height: 1.75;
        padding: 0.1rem 0.25rem 0.5rem;
        background: transparent;
    ">
    {text}
    {sources_html}
    </div>
    """
    components.html(html, height=height, scrolling=True)

# ── Session state ─────────────────────────────────────────────────────────────
if "active_profile" not in st.session_state:
    st.session_state.active_profile = "Math Tutor"
if "chat_histories" not in st.session_state:
    st.session_state.chat_histories = {p: [] for p in PROFILES}
if "summaries" not in st.session_state:
    st.session_state.summaries = {p: "" for p in PROFILES}

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown('<p class="app-title">Study Bot</p>', unsafe_allow_html=True)
    st.markdown('<p class="app-subtitle">local · private · offline</p>', unsafe_allow_html=True)

    st.markdown('<p class="sidebar-section">Profile</p>', unsafe_allow_html=True)
    for profile_name, profile in PROFILES.items():
        if profile_name == st.session_state.active_profile:
            st.markdown(
                f'<div class="profile-active">{profile["icon"]} {profile_name}</div>',
                unsafe_allow_html=True
            )
        else:
            if st.button(f'{profile["icon"]} {profile_name}', key=f"btn_{profile_name}"):
                st.session_state.active_profile = profile_name
                st.rerun()

    st.markdown("---")
    active = PROFILES[st.session_state.active_profile]

    if active["collections"]:
        st.markdown('<p class="sidebar-section">Collections</p>', unsafe_allow_html=True)
        toggle_key = f"active_collections_{st.session_state.active_profile}"
        if toggle_key not in st.session_state:
            st.session_state[toggle_key] = {c: True for c in active["collections"]}
        for col_name in active["collections"]:
            col   = get_collection(col_name)
            count = col.count() if col else 0
            enabled = st.toggle(
                f"{col_name} · {count:,} chunks",
                value=st.session_state[toggle_key].get(col_name, True),
                key=f"toggle_{st.session_state.active_profile}_{col_name}",
            )
            st.session_state[toggle_key][col_name] = enabled

    st.markdown("---")
    st.markdown('<p class="sidebar-section">Settings</p>', unsafe_allow_html=True)
    thinking_mode = st.toggle(
        "Thinking mode",
        value=False,
        help="Deeper reasoning. Slower but better for hard problems.",
    )

    uploaded_image = None
    if st.session_state.active_profile == "Math Tutor":
        st.markdown("---")
        st.markdown('<p class="sidebar-section">Handwritten Math</p>', unsafe_allow_html=True)
        uploaded_image = st.file_uploader(
            "Upload a photo of your work",
            help="LLaVA will transcribe your handwriting and check for errors.",
        )

    st.markdown("---")
    if st.button("Clear chat"):
        st.session_state.chat_histories[st.session_state.active_profile] = []
        st.rerun()

    st.markdown("---")
    st.caption(f"Chat  · qwen3:8b")
    st.caption(f"Embed · {EMBED_MODEL.split('/')[-1]}")
    st.caption("Store · ChromaDB (local)")

# ── Main chat area ────────────────────────────────────────────────────────────
profile      = PROFILES[st.session_state.active_profile]
chat_history = st.session_state.chat_histories[st.session_state.active_profile]

# Render history
for msg in chat_history:
    if msg["role"] == "user":
        with st.chat_message("user"):
            st.markdown(msg["content"])
    else:
        with st.chat_message("assistant"):
            render_response(msg["content"], sources=msg.get("sources", []))

# Input
user_input = st.chat_input(profile["placeholder"])

if user_input:
    with st.chat_message("user"):
        st.markdown(user_input)

    # Memory
    summary = st.session_state.summaries[st.session_state.active_profile]
    chat_history, summary = build_history_with_memory(chat_history, summary)
    st.session_state.summaries[st.session_state.active_profile] = summary

    # Vision
    image_transcription = None
    if uploaded_image is not None:
        import tempfile, os
        allowed = {".jpg", ".jpeg", ".png", ".webp"}
        ext = Path(uploaded_image.name).suffix.lower()
        if ext not in allowed:
            st.error(f"Unsupported file type: {ext}. Please upload a jpg, png, or webp.")
            uploaded_image = None
        else:
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                tmp.write(uploaded_image.read())
                tmp_path = tmp.name
            with st.spinner("Transcribing handwritten work..."):
                from app.vision import transcribe_image
                image_transcription = transcribe_image(tmp_path)
            os.unlink(tmp_path)
            with st.expander("Transcription", expanded=True):
                st.markdown(image_transcription)

    # Retrieval — broad pool for display, high-quality subset for agent context
    sources = []          # all retrieved (shown in UI)
    context_sources = []  # score >= 0.45 only (sent to agent)
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
                n_results=8,
                score_threshold=0.3,
            )
        context_sources = [s for s in sources if s["score"] >= 0.45]

    # Context
    context_parts = []
    if summary:
        context_parts.append(format_summary_as_context(summary))
    if context_sources:
        context_parts.append(format_context(context_sources))
    full_context = "\n\n".join(context_parts)

    # Agent
    augmented_input = user_input
    if image_transcription:
        augmented_input = (
            f"{user_input}\n\n"
            f"[Student's handwritten work — transcribed by LLaVA]\n"
            f"{image_transcription}\n"
            f"[End of transcription]\n\n"
            f"Please identify any errors in the student's work and show the correct solution."
        )

    with st.spinner("Thinking..."):
        result = run_agent(
            user_input=augmented_input,
            history=chat_history,
            system=profile["system_prompt"],
            context=full_context,
            thinking=thinking_mode,
        )

    full_response = result["response"]
    trace         = result["trace"]

    with st.chat_message("assistant"):
        render_response(full_response, sources=sources)
        if trace:
            with st.expander("Reasoning trace", expanded=False):
                for step in trace:
                    if step["type"] == "act":
                        st.markdown(f"**Act** — `{step['tool']}`")
                        st.code(str(step["input"]), language="python")
                    elif step["type"] == "observe":
                        st.markdown(f"**Observe** — `{step['tool']}`")
                        st.code(str(step["result"]), language="text")
                    elif step["type"] == "think":
                        st.markdown("**Think**")
                        st.markdown(step["content"][:500])

    # Save to history
    chat_history.append({"role": "user",      "content": user_input})
    chat_history.append({"role": "assistant", "content": full_response, "sources": sources})
    st.session_state.chat_histories[st.session_state.active_profile] = chat_history
