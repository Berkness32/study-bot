"""
app/agent.py — ReAct agent (Think → Act → Observe loop)
Uses LangGraph to drive qwen3:8b with math tools.
"""

from typing import Annotated, TypedDict, Union
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from app.tools import MATH_TOOLS

# ── Model ─────────────────────────────────────────────────────────────────────
def get_model(thinking: bool = False):
    """Return qwen3:8b with thinking on or off."""
    options = {}
    if not thinking:
        options["num_ctx"] = 4096
    return ChatOllama(
        model="qwen3:8b",
        temperature=0,
        extra_body={"think": thinking},
        **({"options": options} if options else {}),
    ).bind_tools(MATH_TOOLS)


# ── Agent state ───────────────────────────────────────────────────────────────
class AgentState(TypedDict):
    messages: list
    thinking: bool
    profile: str
    context: str          # RAG context injected before each turn
    reasoning_trace: list # captured Think→Act→Observe steps


# ── Nodes ─────────────────────────────────────────────────────────────────────
def agent_node(state: AgentState) -> AgentState:
    """Think step — model decides whether to call a tool or respond."""
    thinking = state.get("thinking", False)
    model    = get_model(thinking=thinking)

    messages = state["messages"]

    # Inject RAG context as a system message if present
    context = state.get("context", "")
    if context:
        messages = [SystemMessage(content=context)] + messages

    response = model.invoke(messages)

    # Capture reasoning trace entry
    trace = state.get("reasoning_trace", [])
    if response.tool_calls:
        for tc in response.tool_calls:
            trace.append({
                "type": "act",
                "tool": tc["name"],
                "input": tc["args"],
            })
    else:
        trace.append({
            "type": "think",
            "content": response.content,
        })

    return {
        **state,
        "messages": state["messages"] + [response],
        "reasoning_trace": trace,
    }


def observe_node(state: AgentState) -> AgentState:
    """Observe step — capture tool results into the trace."""
    trace    = state.get("reasoning_trace", [])
    messages = state["messages"]

    # Last message(s) after tool execution are ToolMessages
    for msg in reversed(messages):
        if isinstance(msg, ToolMessage):
            trace.append({
                "type": "observe",
                "tool": msg.name,
                "result": msg.content,
            })
            break

    return {**state, "reasoning_trace": trace}


def should_continue(state: AgentState) -> str:
    """Route: if last AI message has tool calls → run tools, else → end."""
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return END


# ── Graph ─────────────────────────────────────────────────────────────────────
def build_agent():
    tool_node = ToolNode(MATH_TOOLS)

    graph = StateGraph(AgentState)
    graph.add_node("agent",   agent_node)
    graph.add_node("tools",   tool_node)
    graph.add_node("observe", observe_node)

    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {
        "tools": "tools",
        END:     END,
    })
    graph.add_edge("tools",   "observe")
    graph.add_edge("observe", "agent")

    return graph.compile()


AGENT = build_agent()


# ── Public interface ──────────────────────────────────────────────────────────
def run_agent(
    user_input:  str,
    history:     list,
    system:      str  = "",
    context:     str  = "",
    thinking:    bool = False,
) -> dict:
    """
    Run one turn of the ReAct agent.
    Returns: {response: str, trace: list, messages: list}
    """
    messages = []
    if system:
        messages.append(SystemMessage(content=system))
    for msg in history:
        if msg["role"] == "user":
            messages.append(HumanMessage(content=msg["content"]))
        elif msg["role"] == "assistant":
            messages.append(AIMessage(content=msg["content"]))
    messages.append(HumanMessage(content=user_input))

    state = {
        "messages":       messages,
        "thinking":       thinking,
        "profile":        "",
        "context":        context,
        "reasoning_trace": [],
    }

    result = AGENT.invoke(state)

    # Extract final text response
    final = ""
    for msg in reversed(result["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            final = msg.content
            break

    return {
        "response": final,
        "trace":    result["reasoning_trace"],
        "messages": result["messages"],
    }
