"""
Code graph MCP tool implementations (tools 14-18).

Uses the graphify code graph (graphify-out/graph.json).
Functions are imported by server.py and registered with @server.tool().
"""

from collections import defaultdict
from typing import Optional

import graph_io
from graph_io import (
    format_graph_response,
    log,
)


def code_get_symbol(symbol_name: str) -> dict:
    """Get code symbol with source location, callers, callees, and imports."""
    try:
        graph_data = graph_io.load_code_graph()
        if not graph_data:
            return format_graph_response("code", {"symbol": symbol_name}, {},
                                        ["Code graph not found"])

        nodes = graph_data.get("nodes", [])
        symbol_node = None

        for node in nodes:
            if symbol_name in node.get("id", "") or symbol_name == node.get("label", ""):
                symbol_node = node
                break

        if not symbol_node:
            return format_graph_response("code", {"symbol": symbol_name}, {},
                                        [f"Symbol {symbol_name} not found"])

        edges = graph_data.get("edges", [])
        callers = []
        callees = []
        imports = []

        node_id = symbol_node.get("id")
        for edge in edges:
            if edge.get("target") == node_id and "calls" in edge.get("relationship", ""):
                callers.append({
                    "source": edge.get("source"),
                    "relationship": edge.get("relationship")
                })
            elif edge.get("source") == node_id and "calls" in edge.get("relationship", ""):
                callees.append({
                    "target": edge.get("target"),
                    "relationship": edge.get("relationship")
                })
            elif edge.get("relationship") == "imports":
                if edge.get("source") == node_id:
                    imports.append(edge.get("target"))
                elif edge.get("target") == node_id:
                    imports.append(edge.get("source"))

        return format_graph_response("code", {"symbol": symbol_name}, {
            "symbol": symbol_node,
            "callers": callers,
            "callees": callees,
            "imports": imports
        })
    except Exception as e:
        log(f"ERROR in code_get_symbol: {e}")
        return format_graph_response("code", {"symbol": symbol_name}, {}, [str(e)])


def code_search_symbols(query: str, symbol_type: Optional[str] = None) -> dict:
    """Search classes, functions, methods, modules, files, interfaces."""
    try:
        graph_data = graph_io.load_code_graph()
        if not graph_data:
            return format_graph_response("code", {"query": query}, {},
                                        ["Code graph not found"])

        nodes = graph_data.get("nodes", [])
        results = []

        for node in nodes:
            node_type = node.get("type", "")
            if symbol_type and symbol_type != node_type:
                continue

            if query.lower() in node.get("id", "").lower() or \
               query.lower() in node.get("label", "").lower():
                results.append({
                    "id": node.get("id"),
                    "label": node.get("label"),
                    "type": node_type,
                    "source_location": node.get("source_location")
                })

        return format_graph_response("code", {"query": query, "type": symbol_type},
                                    {"symbols": results})
    except Exception as e:
        log(f"ERROR in code_search_symbols: {e}")
        return format_graph_response("code", {"query": query}, {}, [str(e)])


def code_get_dependencies(symbol_name: str, depth: int = 2) -> dict:
    """Get incoming and outgoing dependencies with depth control."""
    try:
        graph_data = graph_io.load_code_graph()
        if not graph_data:
            return format_graph_response("code", {"symbol": symbol_name, "depth": depth},
                                        {}, ["Code graph not found"])

        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", [])

        symbol_node = None
        for node in nodes:
            if symbol_name in node.get("id", ""):
                symbol_node = node
                break

        if not symbol_node:
            return format_graph_response("code", {"symbol": symbol_name}, {},
                                        [f"Symbol {symbol_name} not found"])

        # BFS to find dependencies
        adj = defaultdict(list)
        for edge in edges:
            src, tgt = edge.get("source"), edge.get("target")
            rel = edge.get("relationship", "")
            adj[src].append((tgt, rel))
            adj[tgt].append((src, rel))

        visited = set()
        queue = [(symbol_node.get("id"), 0, "start")]
        dependencies = {"incoming": [], "outgoing": []}

        while queue:
            node_id, d, rel = queue.pop(0)
            if d > depth or node_id in visited:
                continue
            visited.add(node_id)

            for neighbor, rel_type in adj.get(node_id, []):
                if neighbor not in visited and d < depth:
                    queue.append((neighbor, d + 1, rel_type))

                    if rel_type in ["depends-on", "imports", "uses"]:
                        dependencies["incoming"].append({
                            "source": neighbor,
                            "relationship": rel_type,
                            "depth": d + 1
                        })
                    else:
                        dependencies["outgoing"].append({
                            "target": neighbor,
                            "relationship": rel_type,
                            "depth": d + 1
                        })

        return format_graph_response("code", {"symbol": symbol_name, "depth": depth},
                                    dependencies)
    except Exception as e:
        log(f"ERROR in code_get_dependencies: {e}")
        return format_graph_response("code", {"symbol": symbol_name}, {}, [str(e)])


def code_find_callers(symbol_name: str, transitive: bool = False,
                      max_depth: int = 5) -> dict:
    """Find direct and transitive callers of a symbol."""
    try:
        graph_data = graph_io.load_code_graph()
        if not graph_data:
            return format_graph_response("code", {"symbol": symbol_name}, {},
                                        ["Code graph not found"])

        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", [])

        symbol_node = None
        for node in nodes:
            if symbol_name in node.get("id", ""):
                symbol_node = node
                break

        if not symbol_node:
            return format_graph_response("code", {"symbol": symbol_name}, {},
                                        [f"Symbol {symbol_name} not found"])

        # Find all call edges pointing to this symbol
        callers = []
        node_id = symbol_node.get("id")

        for edge in edges:
            if edge.get("target") == node_id and "call" in edge.get("relationship", ""):
                callers.append({
                    "caller": edge.get("source"),
                    "relationship": edge.get("relationship"),
                    "depth": 1
                })

        # If transitive, find callers of callers
        if transitive:
            visited = {node_id}
            queue = [(c["caller"], 2) for c in callers]

            while queue and len(callers) < 100:
                curr, d = queue.pop(0)
                if d > max_depth or curr in visited:
                    continue
                visited.add(curr)

                for edge in edges:
                    if edge.get("target") == curr and "call" in edge.get("relationship", ""):
                        caller = edge.get("source")
                        callers.append({
                            "caller": caller,
                            "relationship": edge.get("relationship"),
                            "depth": d
                        })
                        queue.append((caller, d + 1))

        return format_graph_response("code", {"symbol": symbol_name, "transitive": transitive},
                                    {"callers": callers})
    except Exception as e:
        log(f"ERROR in code_find_callers: {e}")
        return format_graph_response("code", {"symbol": symbol_name}, {}, [str(e)])


def code_impact_analysis(symbol_name: str) -> dict:
    """Analyze likely affected code for a symbol change."""
    try:
        graph_data = graph_io.load_code_graph()
        if not graph_data:
            return format_graph_response("code", {"symbol": symbol_name}, {},
                                        ["Code graph not found"])

        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", [])

        symbol_node = None
        for node in nodes:
            if symbol_name in node.get("id", ""):
                symbol_node = node
                break

        if not symbol_node:
            return format_graph_response("code", {"symbol": symbol_name}, {},
                                        [f"Symbol {symbol_name} not found"])

        node_id = symbol_node.get("id")
        impact = {
            "direct_callers": [],
            "modules_affected": [],
            "tests": [],
            "interfaces": [],
            "entry_points": []
        }

        for edge in edges:
            if edge.get("target") == node_id and "call" in edge.get("relationship", ""):
                impact["direct_callers"].append(edge.get("source"))
            elif edge.get("source") == node_id:
                rel = edge.get("relationship", "")
                target = edge.get("target")

                if "test" in target.lower():
                    impact["tests"].append(target)
                elif "interface" in rel or "interface" in target.lower():
                    impact["interfaces"].append(target)
                elif "entry" in rel or "entry" in target.lower():
                    impact["entry_points"].append(target)
                else:
                    impact["modules_affected"].append(target)

        return format_graph_response("code", {"symbol": symbol_name}, impact)
    except Exception as e:
        log(f"ERROR in code_impact_analysis: {e}")
        return format_graph_response("code", {"symbol": symbol_name}, {}, [str(e)])
