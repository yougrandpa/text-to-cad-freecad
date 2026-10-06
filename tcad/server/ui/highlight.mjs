// One selection request at a time, pinned to the geometry currently on screen.
export class TreeHighlighter {
  constructor(viewer, { fetch = globalThis.fetch, notice = () => {} } = {}) {
    Object.assign(this, { viewer, notice });
    this.fetch = fetch.bind(globalThis);
    this.generation = 0;
    this.request = null;
    this.cache = new Map();
  }

  async select(entity, artifactId, modelId) {
    const generation = ++this.generation;
    this.request?.abort();
    this.viewer.selectedEntity = null;
    this.viewer.schedule();
    if (!entity || artifactId !== this.viewer.artifactId || !this.viewer.hasMesh) return;
    if (["body", "face", "edge"].includes(entity.entity_kind)) {
      this.viewer.selectedEntity = entity;
      this.viewer.schedule();
      return;
    }
    if (!["sketch", "feature"].includes(entity.entity_kind)) return;
    const nodeId = entity.sketch_id || entity.feature_id;
    const key = JSON.stringify([artifactId, entity.body_id, entity.entity_kind, nodeId]);
    const request = this.request = new AbortController();
    const current = () => generation === this.generation && artifactId === this.viewer.artifactId && this.viewer.hasMesh;
    try {
      let overlay = this.cache.get(key);
      if (!overlay) {
        const params = new URLSearchParams({ model_id: modelId, body_id: entity.body_id,
          kind: entity.entity_kind, node_id: nodeId });
        const response = await this.fetch(`/artifact-sets/${encodeURIComponent(artifactId)}/highlight?${params}`, { signal: request.signal });
        if (!current()) return;
        if (!response.ok) throw new Error("无法读取选中项的几何，请稍后重新选择");
        overlay = await response.json();
        if (!current()) return;
        if (overlay.artifact_id !== artifactId || overlay.body_id !== entity.body_id ||
            overlay.entity_kind !== entity.entity_kind || overlay.node_id !== nodeId ||
            !Array.isArray(overlay.vertices) || overlay.vertices.length > 100000 || overlay.vertices.length % 2 ||
            overlay.vertices.some(p => !Array.isArray(p) || p.length !== 3 || !p.every(Number.isFinite))) {
          throw new Error("选中项几何与当前构建不匹配");
        }
        this.cache.set(key, overlay);
        if (this.cache.size > 32) this.cache.delete(this.cache.keys().next().value);
      }
      if (!current()) return;
      this.viewer.selectedEntity = { ...entity, overlay };
      this.viewer.schedule();
    } catch (error) {
      if (current() && error.name !== "AbortError") this.notice(error.message);
    }
  }
}
