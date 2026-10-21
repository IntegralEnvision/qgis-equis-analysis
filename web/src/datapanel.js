/**
 * The Data panel over the map: what is loaded, and its warnings.
 *
 * Step 1 of the overlay shell (docs/overlay-shell.md): the panel's Data tab
 * is still Qt, so this panel SHOWS the panel's state and asks for the Qt
 * controls when the user wants to change it. It decides nothing. In
 * particular the cached/offline and cut-short warnings are the panel's
 * banner (dialog._update_active_label) word for word: CLAUDE.md requires
 * cached data to be marked everywhere it appears, and the overlay is now
 * one of those places - with ONE authority for what counts as cached.
 */

export class DataPanel {
  /** `ask(what)` sends a request to the host: "data", "dock", "hide". */
  constructor(element, ask) {
    this.element = element;
    this.ask = ask;
    this.update({});
  }

  update({ loaded = false, facility = "", source = "", banner = "",
           warn = false, status = "", level = "info" } = {}) {
    const node = (tag, cls, text) => {
      const el = document.createElement(tag);
      if (cls) el.className = cls;
      if (text !== undefined) el.textContent = text;
      return el;
    };
    const parts = [];
    const what = node("div", "data-what");
    what.append(node("b", "", loaded ? (facility || "Data loaded") : "Nothing loaded"));
    if (source) what.append(node("span", "data-source", ` · ${source}`));
    parts.push(what);
    if (warn && banner) parts.push(node("div", "data-banner", banner));
    if (status) parts.push(node("div", `data-status ${String(level).toLowerCase()}`, status));
    const actions = node("div", "data-actions");
    const button = (label, what_, title) => {
      const b = node("button", "", label);
      b.type = "button";
      b.title = title;
      b.addEventListener("click", () => this.ask(what_));
      return b;
    };
    actions.append(
      button(loaded ? "Change data…" : "Load data…", "data",
             "Open the panel's data controls: source, facility, report, filters"),
      button("⇲ Into the panel", "dock", "Put the charts back in the side panel"),
      button("Hide", "hide", "Hide the overlay; the toolbar button brings it back"),
    );
    parts.push(actions);
    parts.push(node("div", "data-hint",
                    "Drag a header to any edge · – or double-click to tuck it away · Ctrl reaches the map"));
    this.element.replaceChildren(...parts);
  }
}
