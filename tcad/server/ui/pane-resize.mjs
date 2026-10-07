// Keep adjacent panes inside the workbench while respecting usable minimums.
export function fitPaneWidths(preferred, minimums, total) {
  total = Math.max(total, minimums.reduce((sum, width) => sum + width, 0));
  const widths = Array(preferred.length).fill(0);
  const pending = new Set(preferred.map((_, index) => index));
  let remaining = total;
  while (pending.size) {
    const weight = [...pending].reduce((sum, index) => sum + preferred[index], 0);
    const constrained = [...pending].filter(index => remaining * preferred[index] / weight < minimums[index]);
    if (!constrained.length) {
      for (const index of pending) widths[index] = remaining * preferred[index] / weight;
      break;
    }
    for (const index of constrained) {
      widths[index] = minimums[index];
      remaining -= widths[index];
      pending.delete(index);
    }
  }
  return widths;
}

export function resizeAdjacent(widths, minimums, index, delta) {
  const next = [...widths];
  const movement = Math.max(minimums[index] - widths[index],
    Math.min(delta, widths[index + 1] - minimums[index + 1]));
  next[index] += movement;
  next[index + 1] -= movement;
  return next;
}

const storageKey = "tcad.paneWidths.v1";

export class PaneResizeController {
  constructor({ layout, panes, storage }) {
    Object.assign(this, { layout, panes, storage });
    this.view = layout.ownerDocument.defaultView;
    this.preferences = null;
    this.drag = null;
    try {
      const saved = JSON.parse(storage?.getItem(storageKey) || "null");
      if (saved && panes.every(pane => Number.isFinite(saved[pane.key]) && saved[pane.key] > 0 && saved[pane.key] < 100000)) {
        this.preferences = saved;
      }
    } catch { /* Private browsing or an older/corrupt preference. */ }
    this.handles = panes.slice(0, -1).map((left, index) => {
      const right = panes[index + 1];
      const handle = layout.ownerDocument.createElement("div");
      handle.className = "pane-resizer";
      handle.tabIndex = 0;
      handle.setAttribute("role", "separator");
      handle.setAttribute("aria-orientation", "vertical");
      handle.setAttribute("aria-label", `调整${left.label}与${right.label}列宽`);
      handle.setAttribute("aria-controls", `${left.node.id} ${right.node.id}`);
      handle.title = "左右拖动调整列宽；方向键微调；双击恢复默认";
      handle.addEventListener("pointerdown", event => this.begin(handle, left.key, event));
      handle.addEventListener("pointermove", event => this.move(event));
      handle.addEventListener("pointerup", event => this.finish(event, true));
      handle.addEventListener("pointercancel", event => this.finish(event, false));
      handle.addEventListener("lostpointercapture", event => this.finish(event, false));
      handle.addEventListener("keydown", event => this.keydown(left.key, event));
      handle.addEventListener("dblclick", () => this.reset());
      layout.append(handle);
      return handle;
    });
    this.observer = new this.view.ResizeObserver(() => {
      if (this.layout.clientWidth !== this.lastWidth || this.layout.clientHeight !== this.lastHeight) this.refresh();
      else this.positionHandles();
    });
    this.observer.observe(layout);
    this.refresh();
  }

  refresh() {
    if (this.drag) this.finish(null, false);
    this.lastWidth = this.layout.clientWidth;
    this.lastHeight = this.layout.clientHeight;
    this.visible = this.panes.filter(pane => pane.node.getClientRects().length);
    const style = this.view.getComputedStyle(this.layout);
    const total = Math.max(this.visible.reduce((sum, pane) => sum + pane.minWidth, 0),
      this.lastWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)
      - (this.visible.length - 1) * parseFloat(style.columnGap));
    const fixed = this.visible.reduce((sum, pane) => sum + (pane.defaultWidth || 0), 0);
    const flexible = this.visible.reduce((sum, pane) => sum + (pane.weight || 0), 0);
    const preferred = this.visible.map(pane => this.preferences?.[pane.key]
      || pane.defaultWidth || (total - fixed) * pane.weight / flexible);
    this.minimums = this.visible.map(pane => pane.minWidth);
    this.widths = fitPaneWidths(preferred, this.minimums, total);
    this.apply();
  }

  apply() {
    this.layout.style.gridTemplateColumns = this.widths.map(width => `${width}px`).join(" ");
    this.positionHandles();
  }

  positionHandles() {
    if (!this.visible) return;
    const bounds = this.layout.getBoundingClientRect();
    for (const [index, handle] of this.handles.entries()) {
      const left = this.panes[index], right = this.panes[index + 1];
      const visibleIndex = this.visible.findIndex(pane => pane.key === left.key);
      handle.hidden = visibleIndex < 0;
      if (handle.hidden) continue;
      const a = left.node.getBoundingClientRect(), b = right.node.getBoundingClientRect();
      handle.style.left = `${(a.right + b.left) / 2 - bounds.left + this.layout.scrollLeft - 5}px`;
      handle.style.top = `${a.top - bounds.top}px`;
      handle.style.height = `${a.height}px`;
      handle.setAttribute("aria-valuemin", String(left.minWidth));
      handle.setAttribute("aria-valuemax", String(Math.round(this.widths[visibleIndex] + this.widths[visibleIndex + 1] - right.minWidth)));
      handle.setAttribute("aria-valuenow", String(Math.round(this.widths[visibleIndex])));
      handle.setAttribute("aria-valuetext", `${left.label}宽度 ${Math.round(this.widths[visibleIndex])} 像素`);
    }
  }

  begin(handle, key, event) {
    if (event.button !== 0 || this.drag) return;
    event.preventDefault();
    handle.focus();
    this.drag = { handle, id: event.pointerId, x: event.clientX, widths: [...this.widths],
      index: this.visible.findIndex(pane => pane.key === key) };
    handle.setPointerCapture(event.pointerId);
    this.layout.classList.add("is-resizing");
  }

  move(event) {
    if (!this.drag || event.pointerId !== this.drag.id) return;
    this.widths = resizeAdjacent(this.drag.widths, this.minimums, this.drag.index, event.clientX - this.drag.x);
    this.apply();
  }

  finish(event, commit) {
    if (!this.drag || (event && event.pointerId !== this.drag.id)) return;
    const drag = this.drag;
    this.drag = null;
    if (commit) this.save();
    else { this.widths = drag.widths; this.apply(); }
    this.layout.classList.remove("is-resizing");
    if (drag.handle.hasPointerCapture(drag.id)) drag.handle.releasePointerCapture(drag.id);
  }

  keydown(key, event) {
    if (event.key === "Escape" && this.drag) {
      event.preventDefault(); this.finish(null, false); return;
    }
    if (this.drag || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const index = this.visible.findIndex(pane => pane.key === key);
    const delta = event.key === "Home" ? -Infinity : event.key === "End" ? Infinity
      : (event.key === "ArrowLeft" ? -1 : 1) * (event.shiftKey ? 40 : 10);
    this.widths = resizeAdjacent(this.widths, this.minimums, index, delta);
    this.apply(); this.save();
  }

  save() {
    this.preferences ||= Object.fromEntries(this.panes.map(pane => [pane.key, pane.defaultWidth || pane.minWidth]));
    this.visible.forEach((pane, index) => { this.preferences[pane.key] = this.widths[index]; });
    try { this.storage?.setItem(storageKey, JSON.stringify(this.preferences)); } catch { /* Resizing still works without storage. */ }
  }

  reset() {
    this.preferences = null;
    try { this.storage?.removeItem(storageKey); } catch { /* Optional persistence. */ }
    this.refresh();
  }
}
