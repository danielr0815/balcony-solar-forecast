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
