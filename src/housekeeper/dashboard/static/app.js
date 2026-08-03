// Dashboard graph: explore (lazy folders) and projections on one canvas.
// External script (CSP: script-src 'self').
// Explore starts at source roots; folder click fetches children. Projections use /api/graph/projection.
// Force layout; node size by connectivity; labels on zoom; hover highlights neighborhood.
(() => {
  'use strict';

  let graph = null;
  let mode = 'explore';
  const expanded = new Set();
  let linkDistance = 80;
  let repelForce = 4500;

  const byId = (id) => document.getElementById(id);
  const status = (message) => { byId('graph-status').textContent = message; };

  // ---------------------------------------------------------------- theme tokens
  // Node/edge colors live in CSS custom properties so every dashboard theme (including the dark
  // ones) styles the graph; the palette below is the validated fallback.
  const tokens = () => {
    const css = getComputedStyle(document.documentElement);
    const read = (name, fallback) => (css.getPropertyValue(name) || '').trim() || fallback;
    const dark = ['dark', 'hybrid-dark', 'hybrid-solar'].includes(
      document.documentElement.getAttribute('data-theme'));
    return {
      bg: read('--hk-graph-bg', dark ? '#16161d' : '#fcfcfb'),
      ink: read('--hk-graph-ink', dark ? '#e8e8f0' : '#1c1c22'),
      muted: read('--hk-graph-muted', dark ? '#8f8f9d' : '#6d6d76'),
      edge: read('--hk-graph-edge', dark ? '#3d3d4d' : '#d4d4dc'),
      container: read('--hk-graph-container', dark ? '#9085e9' : '#4a3aa7'),
      file: read('--hk-graph-file', dark ? '#6c6c7a' : '#9a9aa6'),
      duplicate: read('--hk-graph-dup', dark ? '#d95926' : '#eb6834'),
      entity: read('--hk-graph-entity', dark ? '#199e70' : '#1baf7a'),
    };
  };

  const styleFor = (t) => [
    { selector: 'node', style: {
      'background-color': t.file, width: 'data(size)', height: 'data(size)',
      label: 'data(label)', color: t.muted, 'font-size': 9, 'min-zoomed-font-size': 11,
      'text-valign': 'bottom', 'text-halign': 'center', 'text-margin-y': 5,
      'text-wrap': 'ellipsis', 'text-max-width': 110, 'border-width': 0,
      'transition-property': 'opacity', 'transition-duration': '0.15s',
    } },
    { selector: 'node[kind = "container"]', style: { 'background-color': t.container } },
    { selector: 'node[kind = "entity"]', style: { 'background-color': t.entity } },
    { selector: 'node[?duplicate]', style: { 'background-color': t.duplicate } },
    { selector: 'node[kind = "overflow"]', style: {
      'background-color': t.bg, 'border-width': 1, 'border-color': t.muted,
      'border-style': 'dashed', color: t.muted,
    } },
    // Collapsed-but-expandable folders wear a halo ring: the visual cue that a click opens them.
    { selector: 'node.can-expand', style: { 'border-width': 2, 'border-color': t.container, 'border-opacity': 0.55 } },
    { selector: 'node.hovered', style: { color: t.ink, 'font-size': 11, 'z-index': 10 } },
    { selector: 'node.faded', style: { opacity: 0.08, 'text-opacity': 0 } },
    { selector: 'node.dim', style: { opacity: 0.12, 'text-opacity': 0 } },
    { selector: 'edge', style: {
      'curve-style': 'haystack', 'haystack-radius': 0, width: 1,
      'line-color': t.edge, opacity: 0.55,
      'transition-property': 'opacity, line-color', 'transition-duration': '0.15s',
    } },
    { selector: 'edge.hl', style: { 'line-color': t.container, opacity: 0.9, width: 1.5 } },
    { selector: 'edge.faded', style: { opacity: 0.05 } },
    { selector: 'edge.dim', style: { opacity: 0.05 } },
    // Selection wears neutral ink — orange is reserved for duplicate identity.
    { selector: ':selected', style: { 'border-width': 2, 'border-color': t.ink } },
  ];

  // ---------------------------------------------------------------- helpers
  // Always refit: the graph grows and shrinks as folders open and close, and keeping the whole
  // constellation in view while it resettles is what makes expansion feel organic.
  const forceLayout = () => ({
    name: 'cose', animate: true, fit: true, padding: 40, randomize: false,
    idealEdgeLength: () => linkDistance, nodeRepulsion: () => repelForce,
    nodeOverlap: 8, gravity: 60, numIter: 700,
  });

  const runLayout = () => { graph.layout(forceLayout()).run(); };

  // Size by connectivity; collapsed folders count hidden children so heavy folders read heavy closed.
  const resize = () => {
    graph.nodes().forEach((node) => {
      const potential = node.data('kind') === 'overflow' ? 0 : (node.data('childCount') || 0);
      const weight = node.degree(false) + Math.min(potential, 400);
      let size = 8 + 4 * Math.sqrt(weight);
      if (node.data('nodeType') === 'SOURCE_ROOT') size = Math.max(size, 22);
      node.data('size', Math.max(8, Math.min(46, size)));
    });
  };

  const human = (bytes) => {
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const power = Math.min(units.length - 1, Math.floor(Math.log2(bytes) / 10));
    return `${(bytes / 2 ** (10 * power)).toFixed(power ? 1 : 0)} ${units[power]}`;
  };

  const kindOf = (node) => {
    if (node.node_type === 'OVERFLOW') return 'overflow';
    if (['SOURCE_ROOT', 'DIRECTORY', 'DIRECTORY_CLUSTER'].includes(node.node_type)) return 'container';
    if (node.node_type === 'FILE') return 'file';
    return 'entity';
  };

  const toElements = (payload) => {
    const present = new Set(graph ? graph.nodes().map((n) => n.id()) : []);
    payload.nodes.forEach((n) => present.add(n.id));
    return [
      ...payload.nodes.map((n) => ({ data: {
        id: n.id, label: n.label, nodeType: n.node_type, kind: kindOf(n),
        childCount: (n.attributes || {}).child_count || 0,
        expandable: Boolean((n.attributes || {}).expandable),
        duplicate: Boolean((n.attributes || {}).duplicate),
        attributes: n.attributes || {}, size: 10,
      } })),
      ...payload.edges
        .filter((e) => present.has(e.source) && present.has(e.target))
        .map((e) => ({ data: {
          id: e.id, source: e.source, target: e.target,
          label: e.edge_type, confidence: e.confidence, evidence: e.evidence,
        } })),
    ];
  };

  const fetchJson = async (url) => {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`Graph request failed: ${response.status}`);
    return response.json();
  };

  // ---------------------------------------------------------------- interactions
  const showDetail = (node) => {
    const a = node.data('attributes') || {};
    const lines = [`${node.data('nodeType')}: ${node.data('label')}`];
    if (a.path) lines.push(`path: ${a.path}`);
    if (a.size_bytes !== undefined) lines.push(`size: ${human(a.size_bytes)}`);
    if (a.child_count) lines.push(`children: ${a.child_count.toLocaleString()}`);
    if (a.member_count) lines.push(`members: ${a.member_count.toLocaleString()}`);
    if (node.data('duplicate')) lines.push('part of an exact-duplicate group');
    if (mode === 'explore' && node.data('expandable')) {
      lines.push(expanded.has(node.id()) ? 'click to collapse' : 'click to expand');
    }
    byId('graph-detail').textContent = lines.join('\n');
  };

  const markExpandables = () => {
    graph.nodes().forEach((node) => {
      const can = mode === 'explore' && node.data('expandable') && !expanded.has(node.id());
      node.toggleClass('can-expand', can);
    });
  };

  const neighborhoodHighlight = () => {
    graph.on('mouseover', 'node', (event) => {
      const node = event.target;
      const hood = node.closedNeighborhood();
      graph.elements().not(hood).addClass('faded');
      hood.nodes().addClass('hovered');
      hood.edges().addClass('hl');
    });
    graph.on('mouseout', 'node', () => {
      graph.elements().removeClass('faded hovered hl');
    });
  };

  const removeSubtree = (node) => {
    // The explorer graph is a forest, so following CONTAINS edges outward is the whole subtree.
    const descendants = node.successors('node');
    descendants.forEach((d) => expanded.delete(d.id()));
    graph.remove(descendants);
  };

  const expandNode = async (node) => {
    status(`Loading ${node.data('label')}…`);
    const payload = await fetchJson(`/api/graph/children?node=${encodeURIComponent(node.id())}`);
    const fresh = toElements(payload).filter((el) => !graph.getElementById(el.data.id).length);
    // Seed children in a ring around their parent so the force simulation unfolds them outward
    // from the click, instead of teleporting them in from the origin.
    const origin = node.position();
    let index = 0;
    const added = graph.add(fresh.map((el) => {
      if (el.data.source) return el;
      const angle = (2 * Math.PI * index) / Math.max(1, fresh.length);
      index += 1;
      return { ...el, position: { x: origin.x + 40 * Math.cos(angle), y: origin.y + 40 * Math.sin(angle) } };
    }));
    expanded.add(node.id());
    resize();
    markExpandables();
    runLayout();
    const note = payload.truncated ? ' (largest shown; the rest are aggregated)' : '';
    status(`${graph.nodes().length} nodes · ${graph.edges().length} links${note}`);
    return added;
  };

  const onTap = async (event) => {
    const node = event.target;
    showDetail(node);
    if (mode !== 'explore' || !node.data('expandable')) return;
    try {
      if (expanded.has(node.id())) {
        expanded.delete(node.id());
        removeSubtree(node);
        resize();
        markExpandables();
        runLayout();
        status(`${graph.nodes().length} nodes · ${graph.edges().length} links`);
      } else {
        await expandNode(node);
      }
    } catch (error) { status(error.message); }
  };

  // ---------------------------------------------------------------- graph lifecycle
  const build = (elements) => {
    if (graph) graph.destroy();
    graph = cytoscape({
      container: byId('cy'), elements, wheelSensitivity: 0.2,
      // Without a zoom ceiling a fit() on one or two nodes fills the screen with a single dot.
      minZoom: 0.05, maxZoom: 2.5,
      style: styleFor(tokens()),
    });
    window.__hkGraph = graph; // debugging/testing seam; the dashboard is loopback-only
    neighborhoodHighlight();
    graph.on('tap', 'node', onTap);
    graph.on('tap', 'edge', (event) => {
      const data = event.target.data();
      byId('graph-detail').textContent =
        `${data.label} (${data.confidence})\n${JSON.stringify(data.evidence || {}, null, 2)}`;
    });
    if (mode !== 'explore') {
      graph.on('dbltap', 'node', async (event) => {
        const data = event.target.data();
        const id = String(data.id).split(':', 2)[1];
        try {
          const payload = await fetchJson(
            `/api/graph/projection?projection_type=${encodeURIComponent(mode)}`
            + `&minimum_confidence=${encodeURIComponent(byId('graph-confidence').value)}`
            + `&root_type=${encodeURIComponent(data.nodeType)}&root_id=${encodeURIComponent(id)}`
            + '&depth=2&max_nodes=500&max_edges=2000');
          graph.add(toElements(payload).filter((el) => !graph.getElementById(el.data.id).length));
          resize();
          runLayout();
          status(`Expanded ${data.label}; ${graph.nodes().length} nodes.`);
        } catch (error) { status(error.message); }
      });
    }
    resize();
    markExpandables();
    runLayout();
  };

  const load = async () => {
    mode = byId('graph-projection').value;
    document.querySelectorAll('.graph-projection-only').forEach((el) => {
      el.style.display = mode === 'explore' ? 'none' : '';
    });
    expanded.clear();
    try {
      if (mode === 'explore') {
        const payload = await fetchJson('/api/graph/children');
        build(toElements(payload));
        status(payload.nodes.length
          ? `${payload.nodes.length} source root${payload.nodes.length === 1 ? '' : 's'} — click one to start exploring`
          : 'Nothing scanned yet — run a scan first, then explore it here.');
      } else {
        const confidence = byId('graph-confidence').value;
        const payload = await fetchJson(
          `/api/graph/projection?projection_type=${encodeURIComponent(mode)}`
          + `&minimum_confidence=${encodeURIComponent(confidence)}`);
        build(toElements(payload));
        status(`${payload.nodes.length} nodes, ${payload.edges.length} edges`
          + `${payload.truncated ? ' (truncated; double-click a node to expand)' : ''}`);
      }
    } catch (error) { status(error.message); }
  };

  // ---------------------------------------------------------------- wiring
  document.addEventListener('DOMContentLoaded', () => {
    byId('graph-load').addEventListener('click', load);
    byId('graph-projection').addEventListener('change', load);
    byId('graph-confidence').addEventListener('change', load);
    byId('graph-collapse-all').addEventListener('click', load);
    byId('graph-search').addEventListener('input', (event) => {
      if (!graph) return;
      const value = event.target.value.toLowerCase();
      graph.batch(() => {
        graph.nodes().forEach((node) => {
          const miss = value && !String(node.data('label')).toLowerCase().includes(value);
          node.toggleClass('dim', miss);
          node.connectedEdges().toggleClass('dim', miss);
        });
      });
    });
    let forceTimer = null;
    const reforce = () => {
      linkDistance = Number(byId('graph-force-distance').value);
      repelForce = Number(byId('graph-force-repel').value);
      if (!graph) return;
      clearTimeout(forceTimer);
      forceTimer = setTimeout(() => runLayout(), 250);
    };
    byId('graph-force-distance').addEventListener('input', reforce);
    byId('graph-force-repel').addEventListener('input', reforce);
    byId('graph-export').addEventListener('click', () => {
      if (!graph) return;
      const link = document.createElement('a');
      link.href = graph.png({ full: true, bg: tokens().bg, scale: 2 });
      link.download = 'housekeeper-graph.png';
      link.click();
    });
    // Re-skin the live graph when the dashboard theme changes.
    new MutationObserver(() => { if (graph) graph.style(styleFor(tokens())); })
      .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    load();
  });
})();
