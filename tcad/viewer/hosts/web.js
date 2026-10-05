/** Portable Web host: no chat, settings, IR store or FreeCAD dependency. */
import { MeshViewport } from "../core/webgl.js";
import { loadArtifactScene } from "../core/artifact.js";

export class ArtifactViewport {
  constructor(canvas, options = {}) {
    this.viewport = new MeshViewport(canvas, options);
    this.sequence = 0;
    this.request = null;
    this.artifact = null;
    this.disposed = false;
  }
  async load(selection) {
    if (this.disposed) throw new Error("Artifact viewport is disposed");
    this.request?.abort();
    const sequence = ++this.sequence;
    const request = this.request = new AbortController();
    const scene = await loadArtifactScene({ ...selection, signal: request.signal });
    if (sequence !== this.sequence) return null;
    this.viewport.setMesh(scene.mesh, scene.motion || [], scene.animation || null);
    this.artifact = Object.freeze({ id: scene.artifact_id, version: scene.version, status: scene.status });
    return this.artifact;
  }
  capture() {
    if (this.disposed || !this.artifact) throw new Error("Load an active artifact before capture");
    this.viewport.render();
    return { ...this.artifact, dataUrl: this.viewport.canvas.toDataURL("image/png") };
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.artifact = null;
    ++this.sequence;
    this.request?.abort();
    this.viewport.dispose();
  }
}
