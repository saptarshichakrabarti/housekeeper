/* Space treemap: squarified layout over /api/treemap/children.
 *
 * No library: squarify is the fifty lines below. One level per request, exactly like the graph
 * explorer, so a million-entry drive costs the same as a small one. Read-only — this page fetches
 * and draws, nothing else.
 */
(function () {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const view = document.getElementById("treemap");
  if (!view) return;
  const crumbs = document.getElementById("treemap-crumbs");
  const note = document.getElementById("treemap-note");
  const tableBody = document.querySelector("#treemap-table tbody");
  const trail = [{ node: null, label: "All sources" }];

  const bytes = (value) => {
    const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    let size = Number(value) || 0;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    return `${unit === 0 ? size : size.toFixed(1)} ${units[unit]}`;
  };

  // The union of duplicate and reviewable bytes, computed in SQL — not their sum. A redundant copy
  // is usually also classified REVIEW_*, so adding the two counts it twice and a folder of nothing
  // but redundant copies reads as 200% reclaimable.
  const share = (tile) =>
    tile.size_bytes > 0 ? tile.reclaimable_bytes / tile.size_bytes : 0;

  // Sequential, one hue, light -> dark (dark mode substitutes its own steps in CSS). Magnitude gets
  // one hue by rule; a rainbow would imply categories that do not exist here.
  const fill = (tile) => `var(--hk-tree-${Math.min(4, Math.floor(share(tile) * 5))})`;

  /* Squarified treemap (Bruls, Huizing, van Wijk): lay each row of tiles along the shorter side,
   * extending the row while doing so lowers the worst aspect ratio. Areas are proportional and no
   * two tiles overlap — the two properties the layout test pins. */
  function squarify(items, x, y, width, height) {
    const out = [];
    let rest = items.filter((item) => item.value > 0);
    const total = rest.reduce((sum, item) => sum + item.value, 0);
    if (!rest.length || total <= 0 || width <= 0 || height <= 0) return out;
    let scale = (width * height) / total;
    let area = { x, y, width, height };

    const worst = (row, side, rowSum) => {
      const sideSquared = side * side;
      const sum = rowSum * rowSum;
      const min = Math.min(...row);
      const max = Math.max(...row);
      return Math.max((sideSquared * max) / sum, sum / (sideSquared * min));
    };

    while (rest.length) {
      const side = Math.min(area.width, area.height);
      const row = [];
      let rowSum = 0;
      while (rest.length) {
        const next = rest[0].value * scale;
        const candidate = row.length ? worst(row.concat(next), side, rowSum + next) : Infinity;
        if (row.length && candidate > worst(row, side, rowSum)) break;
        row.push(next);
        rowSum += next;
        rest = rest.slice(1);
      }
      const thickness = rowSum / side;
      let offset = 0;
      const horizontal = area.width >= area.height;
      row.forEach((value) => {
        const length = value / thickness;
        out.push(
          horizontal
            ? { x: area.x, y: area.y + offset, width: thickness, height: length }
            : { x: area.x + offset, y: area.y, width: length, height: thickness }
        );
        offset += length;
      });
      if (horizontal) {
        area = { x: area.x + thickness, y: area.y, width: area.width - thickness, height: area.height };
      } else {
        area = { x: area.x, y: area.y + thickness, width: area.width, height: area.height - thickness };
      }
      if (area.width <= 0 || area.height <= 0) break;
      const remaining = rest.reduce((sum, item) => sum + item.value, 0);
      if (remaining <= 0) break;
      scale = (area.width * area.height) / remaining;
    }
    return out;
  }

  function draw(payload) {
    const tiles = (payload.children || [])
      .map((child) => ({ ...child, value: child.size_bytes }))
      .filter((child) => child.value > 0)
      .sort((a, b) => b.value - a.value);
    view.textContent = "";
    const width = Math.max(320, view.clientWidth || 900);
    const height = Math.round(width * 0.55);
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", String(height));
    const boxes = squarify(tiles, 0, 0, width, height);
    tiles.forEach((tile, index) => {
      const box = boxes[index];
      if (!box) return;
      const group = document.createElementNS(SVG_NS, "g");
      group.setAttribute("class", "treemap__tile" + (tile.expandable ? " treemap__tile--open" : ""));
      if (tile.expandable) {
        group.setAttribute("tabindex", "0");
        group.setAttribute("role", "button");
        const open = () => load(tile.node, tile.name);
        group.addEventListener("click", open);
        group.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            open();
          }
        });
      }
      const rect = document.createElementNS(SVG_NS, "rect");
      // 2px surface gap between fills, so adjacent tiles never read as one shape.
      rect.setAttribute("x", String(box.x + 1));
      rect.setAttribute("y", String(box.y + 1));
      rect.setAttribute("width", String(Math.max(0, box.width - 2)));
      rect.setAttribute("height", String(Math.max(0, box.height - 2)));
      rect.setAttribute("rx", "2");
      rect.setAttribute("fill", fill(tile));
      group.appendChild(rect);
      const title = document.createElementNS(SVG_NS, "title");
      title.textContent =
        `${tile.name} — ${bytes(tile.size_bytes)} on disk · ` +
        `${bytes(tile.reclaimable_bytes)} reclaimable ` +
        `(${bytes(tile.duplicate_bytes)} duplicate, ${bytes(tile.reviewable_bytes)} reviewable; ` +
        `a file can be both) · estimates`;
      group.appendChild(title);
      // Direct labels only where they fit: a clipped label is worse than a tooltip.
      if (box.width > 90 && box.height > 34) {
        const label = document.createElementNS(SVG_NS, "text");
        label.setAttribute("x", String(box.x + 8));
        label.setAttribute("y", String(box.y + 20));
        label.setAttribute("class", "treemap__label");
        label.textContent = tile.name;
        group.appendChild(label);
        const value = document.createElementNS(SVG_NS, "text");
        value.setAttribute("x", String(box.x + 8));
        value.setAttribute("y", String(box.y + 36));
        value.setAttribute("class", "treemap__value");
        value.textContent = bytes(tile.size_bytes);
        group.appendChild(value);
      }
      svg.appendChild(group);
    });
    if (!tiles.length) {
      view.textContent = "Nothing with a recorded size here.";
    } else {
      view.appendChild(svg);
    }
    note.textContent = payload.truncated
      ? "Showing the largest children only — this folder has more than the request limit."
      : "";
    tableBody.textContent = "";
    tiles.forEach((tile) => {
      const row = document.createElement("tr");
      const percent = Math.round(share(tile) * 100);
      [
        tile.name,
        bytes(tile.size_bytes),
        bytes(tile.duplicate_bytes),
        bytes(tile.reviewable_bytes),
        `${bytes(tile.reclaimable_bytes)} (${percent}%)`,
      ].forEach((text) => {
        const cell = document.createElement("td");
        cell.textContent = text;
        row.appendChild(cell);
      });
      tableBody.appendChild(row);
    });
  }

  function renderCrumbs() {
    crumbs.textContent = "";
    trail.forEach((step, index) => {
      if (index) crumbs.appendChild(document.createTextNode(" / "));
      if (index === trail.length - 1) {
        const here = document.createElement("strong");
        here.textContent = step.label;
        crumbs.appendChild(here);
        return;
      }
      const link = document.createElement("a");
      link.href = "#";
      link.textContent = step.label;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        trail.length = index + 1;
        load(step.node, step.label, true);
      });
      crumbs.appendChild(link);
    });
  }

  async function load(node, label, isBack) {
    if (!isBack && (trail.length === 0 || trail[trail.length - 1].node !== node)) {
      trail.push({ node, label });
    }
    renderCrumbs();
    view.textContent = "Loading…";
    const query = node ? `?node=${encodeURIComponent(node)}` : "";
    try {
      const response = await fetch(`/api/treemap/children${query}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      draw(await response.json());
    } catch (error) {
      view.textContent = `Could not load this folder: ${error.message}`;
    }
  }

  load(null, "All sources", true);
  window.addEventListener("resize", () => {
    const current = trail[trail.length - 1];
    load(current.node, current.label, true);
  });
})();
