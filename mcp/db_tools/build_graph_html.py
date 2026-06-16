import json
from pathlib import Path

from pyvis.network import Network


GRAPH_FILE = Path(".agent-os/db/db_graph.json")
OUTPUT_FILE = Path(".agent-os/db/db_graph.html")


NODE_COLORS = {
    "Table": "#4f8cff",
    "Column": "#8f9baa",
    "Function": "#9b6cff",
    "Procedure": "#ff9f43",
}

NODE_SIZES = {
    "Table": 30,
    "Column": 12,
    "Function": 24,
    "Procedure": 24,
}

EDGE_COLORS = {
    "Stores": "#777777",
    "Links": "#ff5c5c",
    "Creates": "#54c785",
}


def build_tooltip(node: dict) -> str:
    lines = [
        f"<b>{node.get('qualified_name', node['id'])}</b>",
        f"Type: {node.get('node_type', 'Unknown')}",
    ]

    if node.get("node_type") == "Column":
        lines.extend(
            [
                f"SQL type: {node.get('type_display', '')}",
                f"Nullable: {node.get('nullable', '')}",
                f"Primary key: {node.get('is_primary_key', '')}",
                f"Foreign key: {node.get('is_foreign_key', '')}",
            ]
        )

    return "<br>".join(lines)


def main() -> None:
    if not GRAPH_FILE.exists():
        raise FileNotFoundError(
            f"{GRAPH_FILE} does not exist. Run build_db_graph.py first."
        )

    graph_data = json.loads(
        GRAPH_FILE.read_text(encoding="utf-8")
    )

    network = Network(
        height="900px",
        width="100%",
        directed=True,
        bgcolor="#111827",
        font_color="#f3f4f6",
        select_menu=True,
        filter_menu=True,
        cdn_resources="in_line",
    )

    for node in graph_data["nodes"]:
        node_type = node.get("node_type", "Unknown")

        shape = {
            "Table": "box",
            "Column": "dot",
            "Procedure": "diamond",
            "Function": "triangle",
        }.get(node_type, "dot")

        network.add_node(
            node["id"],
            label=node.get("name", node["id"]),
            title=build_tooltip(node),
            color=NODE_COLORS.get(node_type, "#cccccc"),
            size=NODE_SIZES.get(node_type, 15),
            shape=shape,
            node_type=node_type,
        )

    for edge_number, edge in enumerate(graph_data["edges"]):
        relationship = edge.get("relationship", "Unknown")

        network.add_edge(
            edge["source"],
            edge["target"],
            id=f"edge-{edge_number}",
            label=relationship,
            title=relationship,
            color=EDGE_COLORS.get(relationship, "#999999"),
            width={
                "Stores": 1,
                "Links": 3,
                "Creates": 2,
            }.get(relationship, 1),
            arrows="to",
            relationship=relationship,
        )

    network.set_options(
        """
        {
          "interaction": {
            "hover": true,
            "navigationButtons": true,
            "keyboard": true,
            "multiselect": true
          },
          "physics": {
            "enabled": true,
            "solver": "forceAtlas2Based",
            "forceAtlas2Based": {
              "gravitationalConstant": -80,
              "centralGravity": 0.01,
              "springLength": 120,
              "springConstant": 0.08,
              "damping": 0.4,
              "avoidOverlap": 1
            },
            "stabilization": {
              "enabled": true,
              "iterations": 1000
            }
          }
        }
        """
    )

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    network.write_html(
        str(OUTPUT_FILE),
        open_browser=False,
    )

    print(f"Visualization created: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()