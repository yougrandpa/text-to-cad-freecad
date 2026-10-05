/** Pure artifact contract; usable by Web, embedded hosts and capture tools. */
export function validateArtifactScene(body, { modelId, version = null, artifactId = null } = {}) {
  if (body.model_id !== modelId || !Number.isInteger(body.version) || body.version < 0 ||
      (version != null && body.version !== version)) throw new Error("网格响应与当前模型版本不匹配");
  if (typeof body.artifact_id !== "string" || !/^sha256:[0-9a-f]{64}$/.test(body.artifact_id) ||
      !["building", "draft", "verifying", "verified", "failed"].includes(body.status)) {
    throw new Error("几何响应缺少构建身份或验证状态");
  }
  if (artifactId != null && body.artifact_id !== artifactId) throw new Error("几何响应与请求的构建身份不匹配");
  return body;
}

export async function loadArtifactScene({ modelId, artifactId, baseUrl = "", signal, fetcher = fetch }) {
  const query = new URLSearchParams({ model_id: modelId });
  const response = await fetcher(`${baseUrl}/artifact-sets/${encodeURIComponent(artifactId)}/scene?${query}`, { signal });
  if (!response.ok) throw new Error(`Scene unavailable (${response.status})`);
  return validateArtifactScene(await response.json(), { modelId, artifactId });
}
