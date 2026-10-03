// Minimal DOM boundary for real card constructors and event dispatch.
// Deliberately contains no card state/logic; tests assert the generated tree.
export class Element {
  constructor(tag = "element") {
    this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {};
    this.style = {}; this.isConnected = true; this._text = "";
    this.classList = {
      add: (...names) => { this.className = [...new Set([...this.className.split(" "), ...names])].join(" "); },
      remove: (...names) => { this.className = this.className.split(" ").filter((n) => !names.includes(n)).join(" "); },
      contains: (name) => this.className.split(" ").includes(name),
    };
  }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  set className(value) { this.attributes.class = value; }
  get className() { return this.attributes.class || ""; }
  set id(value) { this.attributes.id = value; }
  get id() { return this.attributes.id; }
  set htmlFor(value) { this.attributes.for = value; }
  get htmlFor() { return this.attributes.for; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  appendChild(child) { if (child.parentNode) child.parentNode.removeChild(child); this.children.push(child); child.parentNode = this; return child; }
  removeChild(child) { this.children = this.children.filter((c) => c !== child); child.parentNode = null; }
  get firstChild() { return this.children[0]; }
  attachShadow() { this.shadowRoot = new Element("shadow-root"); return this.shadowRoot; }
  focus() {
    let root = this;
    while (root.parentNode) root = root.parentNode;
    root.activeElement = this;
  }
  addEventListener(event, callback) { (this.listeners[event] ||= []).push(callback); }
  dispatch(event, properties = {}) { for (const cb of this.listeners[event] || []) cb({ target: this, preventDefault() {}, ...properties }); }
  getBoundingClientRect() { return { left: 0, top: 0, width: 700, height: 380 }; }
}
export function installDOM() {
  const classes = new Map();
  globalThis.window = { customCards: [] };
  globalThis.document = { createElement: (tag) => new Element(tag), createElementNS: (_, tag) => new Element(tag) };
  globalThis.HTMLElement = Element;
  globalThis.customElements = { define: (tag, cls) => classes.set(tag, cls), get: (tag) => classes.get(tag) };
  return classes;
}
export function elements(root, predicate) {
  return [root, ...root.children.flatMap((c) => elements(c, () => true))].filter(predicate);
}
export const settle = async () => { for (let i = 0; i < 12; i++) await new Promise((r) => setImmediate(r)); };
