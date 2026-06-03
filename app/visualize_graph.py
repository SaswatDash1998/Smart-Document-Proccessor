"""
GraphRAG Knowledge Graph Visualization with Pyvis
Generates an interactive HTML visualization of the knowledge graph.
"""

import pandas as pd
from pathlib import Path
from pyvis.network import Network
import colorsys
from typing import Dict, Set


def generate_colors(n: int) -> list[str]:
    """Generate n visually distinct colors."""
    colors = []
    for i in range(n):
        hue = i / n
        rgb = colorsys.hsv_to_rgb(hue, 0.7, 0.9)
        hex_color = '#{:02x}{:02x}{:02x}'.format(
            int(rgb[0] * 255),
            int(rgb[1] * 255),
            int(rgb[2] * 255)
        )
        colors.append(hex_color)
    return colors


def visualize_graphrag(
    output_dir: Path,
    output_file: str = "graph_visualization.html",
    height: str = "900px",
    width: str = "100%",
) -> Path:
    """
    Create an interactive Pyvis visualization of the GraphRAG knowledge graph.
    
    Args:
        output_dir: Directory containing GraphRAG parquet outputs
        output_file: Name of the output HTML file
        height: Height of the visualization
        width: Width of the visualization
        
    Returns:
        Path to the generated HTML file
    """
    
    # Load data
    print("📊 Loading GraphRAG data...")
    entities = pd.read_parquet(output_dir / "entities.parquet")
    relationships = pd.read_parquet(output_dir / "relationships.parquet")
    
    # Optional: Load communities for coloring
    try:
        communities = pd.read_parquet(output_dir / "communities.parquet")
        has_communities = True
    except FileNotFoundError:
        has_communities = False
        print("⚠️ Communities file not found, using default coloring")
    
    print(f"✅ Loaded {len(entities)} entities and {len(relationships)} relationships")
    
    # Create network
    net = Network(
        height=height,
        width=width,
        bgcolor="#0f172a",
        font_color="#e2e8f0",
        directed=True,
    )
    
    # Configure physics for better layout
    net.set_options("""
    {
        "physics": {
            "barnesHut": {
                "gravitationalConstant": -30000,
                "centralGravity": 0.3,
                "springLength": 200,
                "springConstant": 0.04,
                "damping": 0.09,
                "avoidOverlap": 0.5
            },
            "minVelocity": 0.75,
            "solver": "barnesHut"
        },
        "interaction": {
            "hover": true,
            "tooltipDelay": 100,
            "navigationButtons": true,
            "keyboard": true
        },
        "edges": {
            "smooth": {
                "type": "continuous",
                "roundness": 0.5
            }
        }
    }
    """)
    
    # Create community color map if available
    # Check if 'community' column exists in entities
    has_community_column = 'community' in entities.columns
    
    if has_communities and has_community_column:
        try:
            unique_communities = entities['community'].dropna().unique()
            if len(unique_communities) > 0:
                community_colors = generate_colors(len(unique_communities))
                color_map = dict(zip(unique_communities, community_colors))
            else:
                has_community_column = False
        except Exception as e:
            print(f"⚠️ Could not use community column: {e}")
            has_community_column = False
    
    if not has_community_column:
        # Fallback: color by entity type
        entity_types = entities['type'].unique() if 'type' in entities.columns else ['UNKNOWN']
        type_colors = {
            'ORGANIZATION': '#3b82f6',  # blue
            'PERSON': '#10b981',         # green
            'GEO': '#f59e0b',           # amber
            'EVENT': '#ef4444',          # red
        }
        color_map = {et: type_colors.get(et, '#6366f1') for et in entity_types}
    
    # Add nodes (entities)
    print("🔵 Adding entity nodes...")
    entity_lookup: Dict[str, dict] = {}
    
    for _, entity in entities.iterrows():
        entity_id = str(entity.get('id', entity.get('name', entity.get('title', ''))))
        entity_name = str(entity.get('name', entity.get('title', entity_id)))
        entity_type = str(entity.get('type', 'UNKNOWN'))
        description = str(entity.get('description', 'No description'))[:200]
        
        # Determine color
        if has_community_column and 'community' in entity and pd.notna(entity['community']):
            color = color_map.get(entity['community'], '#6366f1')
            community_label = f"Community {entity['community']}"
        else:
            color = color_map.get(entity_type, '#6366f1')
            community_label = entity_type
        
        # Calculate node size based on degree (if available) or rank
        size = 10
        if 'degree' in entity and pd.notna(entity['degree']):
            size = max(10, min(50, int(entity['degree']) * 2))
        elif 'rank' in entity and pd.notna(entity['rank']):
            size = max(10, min(50, int(entity['rank'] * 30)))
        
        # Create tooltip
        title = f"""
        <div style="font-family: Arial; padding: 10px; max-width: 300px;">
            <h3 style="margin: 0 0 10px 0; color: {color};">{entity_name}</h3>
            <p style="margin: 5px 0;"><strong>Type:</strong> {entity_type}</p>
            <p style="margin: 5px 0;"><strong>{community_label}</strong></p>
            <p style="margin: 5px 0; color: #94a3b8;">{description}</p>
        </div>
        """
        
        net.add_node(
            entity_id,
            label=entity_name,
            title=title,
            color=color,
            size=size,
            borderWidth=2,
            borderWidthSelected=4,
        )
        
        entity_lookup[entity_id] = {
            'name': entity_name,
            'type': entity_type,
            'color': color,
        }
    
    # Add edges (relationships)
    print("🔗 Adding relationship edges...")
    edge_count = 0
    
    for _, rel in relationships.iterrows():
        source = str(rel.get('source', ''))
        target = str(rel.get('target', ''))
        
        if not source or not target:
            continue
            
        if source not in entity_lookup or target not in entity_lookup:
            continue
        
        # Get relationship properties
        description = str(rel.get('description', 'Related'))[:100]
        weight = rel.get('weight', 1.0)
        
        # Normalize weight for edge thickness (1-10)
        if pd.notna(weight):
            thickness = max(1, min(10, weight * 3))
        else:
            thickness = 2
        
        # Edge tooltip
        edge_title = f"{entity_lookup[source]['name']} → {entity_lookup[target]['name']}\n{description}"
        
        net.add_edge(
            source,
            target,
            title=edge_title,
            width=thickness,
            color={'color': '#475569', 'highlight': '#38bdf8'},
            arrows='to',
        )
        edge_count += 1
    
    print(f"✅ Added {edge_count} relationship edges")
    
    # Generate HTML with custom styling
    output_path = output_dir / output_file
    net.save_graph(str(output_path))
    
    # Enhance the HTML with custom header and legend
    with open(output_path, 'r', encoding='utf-8') as f:
        html_content = f.read()
    
    # Create legend HTML
    legend_items = []
    if has_community_column:
        legend_title = "Communities"
        for comm, color in sorted(color_map.items()):
            legend_items.append(
                f'<div style="display: flex; align-items: center; margin: 5px 0;">'
                f'<div style="width: 20px; height: 20px; background: {color}; '
                f'border-radius: 50%; margin-right: 10px;"></div>'
                f'<span>Community {comm}</span></div>'
            )
    else:
        legend_title = "Entity Types"
        for etype, color in color_map.items():
            legend_items.append(
                f'<div style="display: flex; align-items: center; margin: 5px 0;">'
                f'<div style="width: 20px; height: 20px; background: {color}; '
                f'border-radius: 50%; margin-right: 10px;"></div>'
                f'<span>{etype}</span></div>'
            )
    
    legend_html = f"""
    <div style="position: absolute; top: 20px; right: 20px; background: rgba(15, 23, 42, 0.95); 
                padding: 15px; border-radius: 10px; border: 1px solid #334155; 
                color: #e2e8f0; font-family: Arial; min-width: 200px; z-index: 1000;">
        <h3 style="margin: 0 0 10px 0; color: #38bdf8; font-size: 16px;">{legend_title}</h3>
        {''.join(legend_items)}
        <hr style="border: none; border-top: 1px solid #334155; margin: 15px 0;">
        <div style="font-size: 12px; color: #94a3b8;">
            <p style="margin: 5px 0;"><strong>Nodes:</strong> {len(entities)}</p>
            <p style="margin: 5px 0;"><strong>Edges:</strong> {edge_count}</p>
        </div>
    </div>
    """
    
    # Insert header and legend
    header_html = """
    <div style="text-align: center; padding: 20px; background: #1e293b; color: #e2e8f0;">
        <h1 style="margin: 0; color: #38bdf8;">🕸️ GraphRAG Knowledge Graph</h1>
        <p style="margin: 10px 0 0 0; color: #94a3b8;">Interactive visualization of entities and relationships</p>
    </div>
    """
    
    html_content = html_content.replace(
        '<body>',
        f'<body style="margin: 0; font-family: Arial, sans-serif;">{header_html}{legend_html}'
    )
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print(f"✨ Visualization saved to: {output_path}")
    return output_path


if __name__ == "__main__":
    # For testing
    import sys
    
    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path("data/output")
    
    if not output_dir.exists():
        print(f"❌ Output directory not found: {output_dir}")
        sys.exit(1)
    
    visualize_graphrag(output_dir)
    print("✅ Done! Open the HTML file in your browser.")
