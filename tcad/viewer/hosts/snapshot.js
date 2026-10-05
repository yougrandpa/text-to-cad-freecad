/** Web, embedded hosts and headless captures use the same Viewer Core. */
import { MeshViewport } from "../core/webgl.js";
import { prepareScene } from "../core/scene.js";
import { loadArtifactScene } from "../core/artifact.js";

export function renderSceneSnapshot(canvas, scene, { view = "iso", style = "flat_edges", angle = 0, frame = 0 } = {}) {
  const mesh = prepareScene(scene, { angle, frame });
  const viewport = new MeshViewport(canvas, { background: [1, 1, 1, 1] });
  try {
    if (!viewport.available) throw new Error(viewport.error);
    viewport.setMesh(mesh);
    viewport.setStyle(style);
    viewport.setPreset(view);
    viewport.fit();
    viewport.render();
    return canvas.toDataURL("image/png");
  } finally {
    viewport.dispose();
  }
}

export async function captureArtifact(canvas, selection, settings = {}) {
  const scene = await loadArtifactScene(selection);
  return { artifact_id: scene.artifact_id, version: scene.version, status: scene.status,
    dataUrl: renderSceneSnapshot(canvas, scene, settings) };
}
