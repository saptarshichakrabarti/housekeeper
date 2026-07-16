(() => {
  let graph;
  let activeProjection = 'universe';
  let activeLayout = 'concentric';

  const status = (message) => { document.getElementById('graph-status').textContent = message; };
  const request = async (suffix = '') => {
    const confidence = document.getElementById('graph-confidence').value;
    const response = await fetch(`/api/graph/projection?projection_type=${encodeURIComponent(activeProjection)}&minimum_confidence=${encodeURIComponent(confidence)}${suffix}`);
    if (!response.ok) throw new Error(`Graph request failed: ${response.status}`);
    return response.json();
  };
  const elementsFor = (payload) => {
    const ids = new Set(payload.nodes.map((node) => node.id));
    return [
      ...payload.nodes.map((node) => ({ data: { id: node.id, label: node.label, nodeType: node.node_type, attributes: node.attributes } })),
      ...payload.edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target)).map((edge) => ({ data: { id: edge.id, source: edge.source, target: edge.target, label: edge.edge_type, confidence: edge.confidence, evidence: edge.evidence } }))
    ];
  };
  const layout = () => ({ name: activeLayout, animate: false, fit: true, padding: 40, concentric: (node) => node.degree(), levelWidth: () => 2 });
  async function loadGraph(projection = 'universe', layoutName = 'concentric') {
    activeProjection = projection;
    activeLayout = layoutName;
    const payload = await request();
    if (graph) graph.destroy();
    graph = cytoscape({
      container: document.getElementById('cy'), elements: elementsFor(payload), wheelSensitivity: 0.2,
      style: [
        { selector: 'node', style: { 'background-color': '#4f86b8', label: 'data(label)', color: '#17202a', 'font-size': 10, 'text-wrap': 'ellipsis', 'text-max-width': 100, width: 18, height: 18, 'border-width': 1, 'border-color': '#24506f' } },
        { selector: 'node[nodeType = "DUPLICATE_GROUP"]', style: { 'background-color': '#d28b45', shape: 'round-rectangle', width: 26, height: 26 } },
        { selector: 'node[nodeType = "SOURCE_ROOT"]', style: { 'background-color': '#5f9f72', width: 32, height: 32, 'font-weight': 'bold' } },
        { selector: 'edge', style: { width: 'mapData(confidence, 0, 1, 1, 5)', 'line-color': '#a7b6c2', 'target-arrow-color': '#a7b6c2', 'target-arrow-shape': 'triangle', 'curve-style': 'bezier', label: 'data(label)', 'font-size': 7, opacity: 0.75 } },
        { selector: ':selected', style: { 'border-width': 3, 'border-color': '#d94841', 'line-color': '#d94841', 'target-arrow-color': '#d94841' } }
      ], layout: layout()
    });
    graph.on('tap', 'node', (event) => {
      const data = event.target.data();
      document.getElementById('graph-detail').textContent = `${data.nodeType}: ${data.label}\n${JSON.stringify(data.attributes || {}, null, 2)}\nDouble-click to expand.`;
    });
    graph.on('tap', 'edge', (event) => {
      const data = event.target.data();
      document.getElementById('graph-detail').textContent = `${data.label} (${data.confidence})\n${JSON.stringify(data.evidence || {}, null, 2)}`;
    });
    graph.on('dbltap', 'node', async (event) => {
      const data = event.target.data();
      const id = String(data.id).split(':', 2)[1];
      try {
        const payload = await request(`&root_type=${encodeURIComponent(data.nodeType)}&root_id=${encodeURIComponent(id)}&depth=2&max_nodes=500&max_edges=2000`);
        graph.add(elementsFor(payload).filter((element) => !graph.getElementById(element.data.id).length));
        graph.layout(layout()).run();
        status(`Expanded ${data.label}; ${graph.nodes().length} nodes.`);
      } catch (error) { status(error.message); }
    });
    status(`${payload.nodes.length} nodes, ${payload.edges.length} edges${payload.truncated ? ' (truncated; select a node to expand)' : ''}`);
  }

  document.addEventListener('DOMContentLoaded', () => {
    const load = () => loadGraph(document.getElementById('graph-projection').value, document.getElementById('graph-layout').value).catch((error) => status(error.message));
    document.getElementById('graph-load').addEventListener('click', load);
    document.getElementById('graph-confidence').addEventListener('change', load);
    document.getElementById('graph-search').addEventListener('input', (event) => { if (!graph) return; const value = event.target.value.toLowerCase(); graph.nodes().forEach((node) => node.style('display', !value || node.data('label').toLowerCase().includes(value) ? 'element' : 'none')); });
    document.getElementById('graph-export').addEventListener('click', () => { if (!graph) return; const link = document.createElement('a'); link.href = graph.png({ full: true, bg: '#ffffff', scale: 2 }); link.download = 'housekeeper-graph.png'; link.click(); });
    load();
  });
})();
