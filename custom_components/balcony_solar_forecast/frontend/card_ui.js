/** Accessible native controls shared by the two cards; no framework required. */
export function labelControl(label, control, id) {
  control.id = id;
  label.htmlFor = id;
}
export function pressed(button, active) {
  button.setAttribute("aria-pressed", String(active));
}
export function describeChart(chart, text) {
  chart.setAttribute("role", "img");
  chart.setAttribute("aria-label", text);
}
export function dataTable(title, headings, rows) {
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = title;
  details.appendChild(summary);
  const table = document.createElement("table");
  table.style.width = "100%";
  const caption = document.createElement("caption");
  caption.textContent = title;
  table.appendChild(caption);
  const head = document.createElement("tr");
  for (const heading of headings) {
    const th = document.createElement("th"); th.scope = "col"; th.textContent = heading;
    head.appendChild(th);
  }
  table.appendChild(head);
  for (const values of rows) {
    const row = document.createElement("tr");
    for (const value of values) {
      const cell = document.createElement("td"); cell.textContent = String(value);
      row.appendChild(cell);
    }
    table.appendChild(row);
  }
  const scroll = document.createElement("div");
  scroll.style.overflowX = "auto";
  scroll.appendChild(table);
  details.appendChild(scroll);
  return details;
}

/** Keep keyboard position and expanded tables through a state-driven render. */
export function preserveUiState(root, render) {
  const walk = (node) => [node, ...Array.from(node.children || []).flatMap(walk)];
  const controls = (nodes) => nodes.filter((node) =>
    ["BUTTON", "INPUT", "SELECT", "SUMMARY"].includes(node.tagName?.toUpperCase()));
  const before = walk(root);
  const active = root.activeElement;
  const position = controls(before).indexOf(active);
  const open = before.filter((node) => node.tagName?.toUpperCase() === "DETAILS").map((node) => node.open);
  const selection = active && typeof active.selectionStart === "number"
    ? [active.selectionStart, active.selectionEnd] : null;
  const result = render();
  const after = walk(root);
  after.filter((node) => node.tagName?.toUpperCase() === "DETAILS")
    .forEach((node, index) => { if (index < open.length) node.open = open[index]; });
  const target = active?.id ? after.find((node) => node.id === active.id) : controls(after)[position];
  if (position >= 0 && target?.tagName === active?.tagName && typeof target.focus === "function") {
    target.focus({ preventScroll: true });
    if (selection && typeof target.setSelectionRange === "function") {
      target.setSelectionRange(...selection);
    }
  }
  return result;
}
