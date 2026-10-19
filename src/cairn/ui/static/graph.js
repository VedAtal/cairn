(() => {
  "use strict";
  const picker = document.getElementById("graph-project");
  if (picker) picker.addEventListener("change", () => picker.form.submit());
  const box = document.getElementById("graph");
  if (!box || typeof cytoscape !== "function") return;
  const msg = document.getElementById("graph-msg");
  const tip = document.getElementById("graph-tip");
  const note = document.getElementById("graph-note");
  const filter = document.getElementById("graph-filter");
  const buttons = document.querySelectorAll("[data-layout]");
  const LARGE = 2000;
  let cy = null;
  let summary = "";

  const say = (text) => { msg.textContent = text; msg.hidden = !text; };
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function styles() {
    const c = {accent: css("--accent"), soft: css("--accent-soft"), text: css("--text"), muted: css("--muted"),
               bg: css("--bg"), warn: css("--warn")};
    return [
      {selector: "node", style: {
        "width": "data(size)", "height": "data(size)", "background-color": c.soft, "border-width": 1.5,
        "border-color": c.accent, "label": "data(label)", "color": c.text, "font-size": 11, "text-valign": "bottom",
        "text-margin-y": 4, "text-wrap": "ellipsis", "text-max-width": "140px", "text-background-color": c.bg,
        "text-background-opacity": 0.75, "text-background-padding": "1px", "min-zoomed-font-size": 5,
        "overlay-opacity": 0}},
      {selector: "node.inject", style: {"background-color": c.warn, "border-color": c.warn, "border-width": 3}},
      {selector: "edge", style: {"curve-style": "bezier", "width": 1.2, "line-color": c.muted, "opacity": 0.45,
                                 "overlay-opacity": 0}},
      {selector: "edge.link", style: {
        "line-color": c.accent, "line-style": "dashed", "width": "data(width)", "opacity": 0.9,
        "target-arrow-shape": "triangle", "target-arrow-color": c.accent, "arrow-scale": 0.8}},
      {selector: "node.hit", style: {"border-width": 3, "border-color": c.accent, "font-weight": 600}},
      {selector: ".dim", style: {"opacity": 0.12}},
    ];
  }

  function layout(name) {
    buttons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.layout === name)));
    tip.hidden = true;
    const common = {animate: false, fit: true, padding: 30};
    cy.maxZoom(1.2);  // fitting a handful of nodes would zoom so far in that labels dwarf the graph
    if (name === "tree") {
      const level = {};
      const count = [];
      cy.nodes().forEach((n) => {
        const d = n.data("up") ? level[n.data("up")] + 1 : 0;
        level[n.id()] = d;
        count[d] = (count[d] || 0) + 1;
      });
      // very wide rows fit as an unreadable strip; rings, spread so the fullest one doesn't overlap, stay legible
      const radial = Math.max(...count) > 40;
      const step = Math.max(Math.min(box.clientWidth, box.clientHeight) / 2 / count.length, 64);
      const spread = Math.max(1, ...count.map((k, d) => (k * 20) / (2 * Math.PI * step * (d + 0.5))));
      // path order keeps siblings together; the default sort also pulls nodes toward their links
      cy.elements().not(".link").layout({...common, name: "breadthfirst", directed: true, circle: radial,
                                         roots: cy.nodes().filter((n) => !n.data("up")),
                                         depthSort: (a, b) => a.data("order") - b.data("order"),
                                         spacingFactor: radial ? spread : 1.25, nodeDimensionsIncludeLabels: !radial})
        .run();
    } else {
      cy.layout({...common, name: "cose", nodeDimensionsIncludeLabels: true}).run();
    }
    cy.maxZoom(3);
  }

  function highlight() {
    const q = filter.value.trim().toLowerCase();
    let hits = cy.collection();
    cy.batch(() => {
      cy.elements().removeClass("dim hit");
      if (!q) return;
      hits = cy.nodes().filter((n) => n.data("label").toLowerCase().includes(q));
      hits.addClass("hit");
      cy.elements().not(hits.union(hits.edgesWith(hits))).addClass("dim");
    });
    note.textContent = q ? `${hits.length} of ${cy.nodes().length} nodes match` : summary;
    return hits;
  }

  function showTip(evt, text) {
    tip.textContent = text;
    tip.hidden = false;
    const p = evt.renderedPosition;
    tip.style.left = `${Math.max(8, Math.min(p.x + 14, box.clientWidth - tip.offsetWidth - 8))}px`;
    tip.style.top = `${Math.min(p.y + 14, box.clientHeight - tip.offsetHeight - 8)}px`;
  }

  function linkTip(edge) {
    const d = edge.data();
    const lines = [`${d.count} ${d.count === 1 ? "link" : "links"}: ${edge.source().data("label")} → ` +
                   edge.target().data("label")];
    d.reasons.forEach((r) => lines.push(`• ${r}`));
    if (d.count > d.reasons.length) lines.push(`…and ${d.count - d.reasons.length} more`);
    return lines.join("\n");
  }

  function nodeTip(node) {
    const d = node.data();
    return `${d.label}\n${d.entries} ${d.entries === 1 ? "entry" : "entries"}${d.inject ? " · injected" : ""}\n${d.id}`;
  }

  function draw(g) {
    if (!g.nodes.length) { say("This project has no nodes yet."); return; }
    const large = g.nodes.length > LARGE;
    const els = [];
    g.nodes.forEach((n, order) => {
      els.push({group: "nodes", classes: n.inject ? "inject" : "", data: {
        id: n.ref, label: n.title, up: n.parent, entries: n.entries, inject: n.inject, order,
        size: Math.min(64, 16 + 7 * Math.sqrt(n.entries))}});
      if (n.parent) els.push({group: "edges", classes: "parent", data: {id: `parent ${n.ref}`, source: n.parent,
                                                                       target: n.ref}});
    });
    g.edges.forEach((e, i) => els.push({group: "edges", classes: "link", data: {
      id: `link ${i}`, source: e.source, target: e.target, count: e.count, reasons: e.reasons,
      width: Math.min(6, 1.5 + 0.75 * e.count)}}));
    say("");
    cy = cytoscape({container: box, elements: els, style: styles(), minZoom: 0.02, maxZoom: 3,
                    boxSelectionEnabled: false, textureOnViewport: large, hideEdgesOnViewport: large,
                    pixelRatio: large ? 1 : "auto"});
    summary = `${g.nodes.length} nodes · ${g.edges.length} links` +
              (large ? " · large graph: Tree by default, Force may be slow" : "");
    note.textContent = summary;
    cy.on("mouseover", "node", (evt) => { box.style.cursor = "pointer"; showTip(evt, nodeTip(evt.target)); });
    cy.on("mouseover", "edge.link", (evt) => showTip(evt, linkTip(evt.target)));
    cy.on("mouseout", "node, edge", () => { box.style.cursor = ""; tip.hidden = true; });
    cy.on("viewport", () => { tip.hidden = true; });
    cy.on("tap", "node", (evt) => {
      const url = `/ui/r/${evt.target.id()}`;
      const o = evt.originalEvent;
      if (o && (o.metaKey || o.ctrlKey)) window.open(url); else window.location.href = url;
    });
    buttons.forEach((b) => b.addEventListener("click", () => layout(b.dataset.layout)));
    filter.addEventListener("input", highlight);
    filter.addEventListener("keydown", (evt) => {
      if (evt.key !== "Enter") return;
      const hits = highlight();
      if (hits.length) cy.fit(hits, 60);
    });
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => cy.style(styles()));
    layout(large ? "tree" : "force");
  }

  fetch(`/ui/graph.json?project=${encodeURIComponent(box.dataset.project)}`)
    .then((r) => r.json().then((data) => (r.ok ? draw(data) : say(data.error || "Could not load the graph."))))
    .catch((err) => { console.error(err); say("Could not load the graph; reload the page."); });
})();
