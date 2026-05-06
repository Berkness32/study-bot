"""
app/memory.py — Session summary memory
Keeps a rolling summary of the conversation so context
doesn't grow unbounded across a long study session.
"""

from langchain_ollama import ChatOllama
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

SUMMARY_MODEL = ChatOllama(model="qwen3:8b", temperature=0)

SUMMARY_PROMPT = """You are summarizing a tutoring session. 
Given the existing summary and new exchanges, write a concise updated summary 
that captures: topics covered, problems solved, key results, and any 
misconceptions corrected. Be brief — 3-5 sentences max.

Existing summary:
{existing}

New exchanges:
{new}

Updated summary:"""

MAX_HISTORY_BEFORE_SUMMARY = 10  # messages before we summarize


def should_summarize(history: list) -> bool:
    return len(history) >= MAX_HISTORY_BEFORE_SUMMARY


def summarize(history: list, existing_summary: str = "") -> str:
    """Summarize recent history into a compact string."""
    new_exchanges = "\n".join(
        f"{m['role'].upper()}: {m['content'][:300]}"
        for m in history[-MAX_HISTORY_BEFORE_SUMMARY:]
    )
    prompt = SUMMARY_PROMPT.format(
        existing=existing_summary or "None yet.",
        new=new_exchanges,
    )
    try:
        response = SUMMARY_MODEL.invoke([HumanMessage(content=prompt)])
        return response.content.strip()
    except Exception as e:
        return existing_summary  # fail gracefully, keep old summary


def build_history_with_memory(
    history: list,
    summary: str = "",
) -> tuple[list, str]:
    """
    Returns (trimmed_history, updated_summary).
    If history is long, summarizes old messages and keeps only recent ones.
    """
    if not should_summarize(history):
        return history, summary

    updated_summary = summarize(history, summary)
    # Keep only the last 4 messages (2 exchanges) after summarizing
    trimmed = history[-4:]
    return trimmed, updated_summary


def format_summary_as_context(summary: str) -> str:
    if not summary:
        return ""
    return f"[SESSION SUMMARY SO FAR]\n{summary}\n[END SUMMARY]\n"
