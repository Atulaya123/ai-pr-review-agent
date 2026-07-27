from langgraph.graph import END, START, StateGraph

from backend.orchestrator.nodes import (
    aggregate_node,
    build_context,
    docs_node,
    quality_node,
    security_node,
    tests_node,
)
from backend.orchestrator.state import ReviewState


def build_review_graph():
    """Wires the L3 agentic fan-out: build_context -> {4 specialists in parallel} -> aggregate.

    LangGraph's Pregel-style execution runs all nodes whose predecessors have
    completed in the same superstep, so the four specialist nodes execute
    concurrently; aggregate_node only fires once all four have returned
    (the join is encoded in the graph, not hand-orchestrated).
    """
    graph = StateGraph(ReviewState)

    graph.add_node("build_context", build_context)
    graph.add_node("security", security_node)
    graph.add_node("quality", quality_node)
    graph.add_node("tests", tests_node)
    graph.add_node("docs", docs_node)
    graph.add_node("aggregate", aggregate_node)

    graph.add_edge(START, "build_context")
    for specialist in ("security", "quality", "tests", "docs"):
        graph.add_edge("build_context", specialist)
        graph.add_edge(specialist, "aggregate")
    graph.add_edge("aggregate", END)

    return graph
