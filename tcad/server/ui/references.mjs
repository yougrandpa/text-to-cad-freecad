// One owner for reference chips, catalog identity and button availability.
export class ReferenceController {
  constructor(container, { notice, focus, document = globalThis.document }) {
    this.container = container;
    this.document = document;
    this.notice = notice;
    this.focus = focus;
    this.catalog = null;
    this.selected = new Map();
    this.buttons = new Set();
    this.busy = false;
  }

  key(ref) {
    return JSON.stringify([ref.body_id, ref.entity_kind, ref.sketch_id || ref.feature_id || ref.body_id]);
  }

  update(catalog, modelId, version) {
    const next = catalog?.model_id === modelId && catalog.ir_version === version ? catalog : null;
    if (this.selected.size && (!next || [...this.selected.values()].some(({ ref }) =>
      ref.model_id !== next.model_id || ref.artifact_id !== next.artifact_id || ref.ir_version !== next.ir_version))) {
      this.selected.clear();
      this.notice("模型或构建已更新，请重新引用对象。");
    }
    this.catalog = next;
    this.render();
  }

  context() {
    return this.selected.size ? { schema_version: 1,
      selection_refs: [...this.selected.values()].map(({ ref }) => ({ ...ref })) } : null;
  }

  reset() {
    this.catalog = null;
    this.selected.clear();
    this.buttons.clear();
    this.render();
  }

  clear() {
    this.selected.clear();
    this.render();
  }

  setBusy(busy) {
    this.busy = busy;
    for (const button of this.buttons) button.disabled = busy;
    this.render();
  }

  beginTree() {
    this.buttons.clear();
  }

  button(bodyId, kind, id) {
    const target = this.catalog?.targets.find(({ ref }) => ref.body_id === bodyId && ref.entity_kind === kind
      && (ref.sketch_id || ref.feature_id || ref.body_id) === id);
    if (!target) return null;
    const button = this.document.createElement("button");
    button.type = "button";
    button.className = "mini reference-button";
    button.textContent = "引用";
    button.title = `引用 ${target.label}${target.editable ? "" : "（仅可查看）"}`;
    button.disabled = this.busy;
    button.addEventListener("click", () => {
      // A detached tree row may survive an asynchronous refresh. Its closure
      // must not reintroduce a reference from the previous publication.
      if (this.busy || !this.catalog?.targets.includes(target)) return;
      if (this.selected.size >= 8 && !this.selected.has(this.key(target.ref))) {
        this.notice("最多引用 8 个对象，请先移除其他引用。");
        return;
      }
      this.selected.set(this.key(target.ref), target);
      this.render();
      this.focus();
    });
    this.buttons.add(button);
    return button;
  }

  render() {
    if (!this.container) return;
    this.container.replaceChildren();
    this.container.hidden = this.selected.size === 0;
    for (const [key, target] of this.selected) {
      const chip = this.document.createElement("button");
      chip.type = "button";
      chip.className = "reference-chip";
      chip.textContent = `${target.label} · v${target.ref.ir_version} ×`;
      chip.title = `移除引用 ${target.ref.sketch_id || target.ref.feature_id || target.ref.body_id}`;
      chip.disabled = this.busy;
      chip.addEventListener("click", () => {
        if (!this.busy) { this.selected.delete(key); this.render(); }
      });
      this.container.append(chip);
    }
    if (this.selected.size) {
      const clear = this.document.createElement("button");
      clear.type = "button";
      clear.className = "mini";
      clear.textContent = "清空引用";
      clear.disabled = this.busy;
      clear.addEventListener("click", () => { if (!this.busy) this.clear(); });
      this.container.append(clear);
    }
  }
}
