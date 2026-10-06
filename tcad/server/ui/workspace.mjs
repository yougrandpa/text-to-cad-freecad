// Narrow windows show one bounded work area instead of stacking full panes.
// CSS owns visibility; this controller keeps tabs and accessibility in sync.
export class WorkspaceController {
  constructor({ layout, tabs, panels, media, onModeChange = () => {}, onSelect = () => {} }) {
    Object.assign(this, { layout, tabs, panels, media, onModeChange, onSelect });
    this.active = "chat";
    this.userSelected = false;
    this.compact = media.matches;
    const names = Object.keys(tabs);
    for (const [index, name] of names.entries()) {
      tabs[name].addEventListener("click", () => this.select(name));
      tabs[name].addEventListener("keydown", event => {
        const target = { ArrowLeft: (index + names.length - 1) % names.length,
          ArrowRight: (index + 1) % names.length, Home: 0, End: names.length - 1 }[event.key];
        if (target === undefined) return;
        event.preventDefault(); this.select(names[target]); tabs[names[target]].focus();
      });
    }
    media.addEventListener("change", event => {
      this.compact = event.matches;
      this.sync();
      this.onModeChange(this.compact);
    });
    this.sync();
    this.onModeChange(this.compact);
  }

  select(name, { userInitiated = true } = {}) {
    if (!Object.hasOwn(this.tabs, name)) return;
    this.active = name;
    if (userInitiated) this.userSelected = true;
    this.sync();
    this.onSelect(name, this.compact);
  }

  showModel() {
    if (!this.userSelected) this.select("model", { userInitiated: false });
  }

  sync() {
    this.layout.dataset.workspace = this.active;
    for (const [name, tab] of Object.entries(this.tabs)) {
      tab.setAttribute("aria-selected", String(name === this.active));
      tab.tabIndex = name === this.active ? 0 : -1;
      const panel = this.panels[name];
      if (this.compact) {
        panel.setAttribute("role", "tabpanel");
        panel.setAttribute("aria-labelledby", tab.id);
      } else {
        panel.removeAttribute("role");
        panel.removeAttribute("aria-labelledby");
      }
    }
  }
}
