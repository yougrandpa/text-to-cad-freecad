// A read-only view of the declared history. The displayed build supplies its
// frozen ir.json; browsing never edits IR or infers feature-to-face ownership.
const operations = {
  pad: "拉伸", pocket: "切除", revolution: "旋转", groove: "旋转切除",
  additive_loft: "放样", subtractive_loft: "放样切除",
  fillet: "圆角", chamfer: "倒角", draft: "拔模", thickness: "抽壳", hole: "孔",
  mirrored: "镜像", linear_pattern: "线性阵列", circular_pattern: "圆周阵列",
  polar_pattern: "极轴阵列", multi_transform: "多重变换", datum_plane: "基准面",
  additive_box: "长方体", additive_cylinder: "圆柱体", additive_sphere: "球体", additive_cone: "圆锥体",
  subtractive_box: "矩形切除", subtractive_cylinder: "圆柱切除",
  subtractive_sphere: "球形切除", subtractive_cone: "圆锥切除",
};
const parameterNames = {
  length: "长度", width: "宽度", height: "高度", radius: "半径", radius1: "半径 1",
  radius2: "半径 2", angle: "角度", angle2: "角度 2", reversed: "反向", type: "方式",
  midplane: "对称", occurrences: "数量", refine: "优化形状", thickness: "壁厚",
};
const linearParameters = new Set(["length", "width", "height", "radius", "radius1", "radius2", "thickness"]);
const angularParameters = new Set(["angle", "angle2"]);
const valueText = value => typeof value === "object" && value !== null ? JSON.stringify(value)
  : typeof value === "boolean" ? (value ? "是" : "否") : String(value);
const keyOf = (bodyId, kind, id) => JSON.stringify([bodyId, kind, id]);

export function buildModelTree(ir) {
  return (ir?.bodies || []).map(body => {
    const consumed = new Set();
    const sketchNode = sketch => ({ key: keyOf(body.id, "sketch", sketch.id), kind: "sketch",
      id: sketch.id, bodyId: body.id, label: sketch.name || sketch.id, type: "草图", data: sketch, children: [] });
    const features = (body.features || []).map((feature, index) => {
      const sketches = [feature.profile_sketch, ...(feature.sections || [])]
        .map(id => (body.sketches || []).find(s => s.id === id)).filter(Boolean);
      const children = sketches.filter(sketch => !consumed.has(sketch.id)).map(sketchNode);
      for (const sketch of sketches) consumed.add(sketch.id);
      return { key: keyOf(body.id, "feature", feature.id), kind: "feature", id: feature.id,
        bodyId: body.id, label: feature.name || feature.id, type: operations[feature.op] || feature.op,
        order: index + 1, suppressed: feature.suppress, data: feature, children };
    });
    return { key: keyOf(body.id, "body", body.id), kind: "body", id: body.id, bodyId: body.id,
      label: body.name || body.id, type: body.part_ref ? "引用零件" : "零件",
      data: body, children: [...(body.sketches || []).filter(s => !consumed.has(s.id)).map(sketchNode), ...features] };
  });
}

// Keep matching ancestors, and show the entire branch when a parent matches.
export function filterModelTree(nodes, query) {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return nodes;
  return nodes.flatMap(node => {
    const own = `${node.label} ${node.id} ${node.type} ${node.data.op || ""}`.toLocaleLowerCase().includes(needle);
    const children = own ? node.children : filterModelTree(node.children, query);
    return own || children.length ? [{ ...node, children }] : [];
  });
}

export class StructureController {
  constructor({ tree, detail, search, meta, count, expand, collapse }, {
    document = globalThis.document, onSelect = () => {}, referenceButton = () => null,
    beginReferences = () => {},
  } = {}) {
    Object.assign(this, { tree, detail, search, meta, count, expand, collapse, document,
      onSelect, referenceButton, beginReferences });
    this.nodes = [];
    this.openState = new Map();
    this.selected = null;
    this.identity = null;
    this.search.addEventListener("input", () => this.render());
    this.expand.addEventListener("click", () => this.setExpanded(true));
    this.collapse.addEventListener("click", () => this.setExpanded(false));
  }

  element(tag, className = "", text = null) {
    const node = this.document.createElement(tag);
    node.className = className;
    if (text !== null) node.textContent = text;
    return node;
  }

  walk(nodes = this.nodes) {
    return nodes.flatMap(node => [node, ...this.walk(node.children)]);
  }

  message(text) {
    this.update(null);
    this.tree.replaceChildren(this.element("div", "structure-empty", text));
  }

  update(ir, { artifactId = null, status = null, sourceVersion = null } = {}) {
    const identity = ir ? `${ir.model_id}:${artifactId || "source"}:${ir.version}` : null;
    if (identity !== this.identity) {
      this.selected = null;
      this.onSelect(null, null);
      if (!ir || this.modelId !== ir.model_id) {
        this.openState.clear();
        this.search.value = "";
      }
    }
    this.identity = identity;
    this.modelId = ir?.model_id;
    this.ir = ir;
    this.artifactId = artifactId;
    this.status = status;
    this.sourceVersion = sourceVersion;
    this.nodes = buildModelTree(ir);
    this.render();
  }

  setSourceVersion(version) {
    this.sourceVersion = version;
    this.renderMeta();
  }

  renderMeta() {
    if (!this.ir) { this.meta.textContent = "等待生成模型"; return; }
    this.meta.textContent = this.artifactId
      ? `画布构建 v${this.ir.version} · ${this.status === "verified" ? "几何已验证" : "几何未验证"}`
      : `编辑版本 v${this.ir.version} · 尚未显示构建`;
    if (this.artifactId && this.sourceVersion != null && this.sourceVersion > this.ir.version) {
      this.meta.textContent += ` · 编辑 v${this.sourceVersion} 待构建`;
    }
  }

  setExpanded(open) {
    for (const node of this.walk()) if (node.children.length) this.openState.set(node.key, open);
    this.render();
  }

  select(node, { notify = true } = {}) {
    this.selected = node?.key || null;
    let selectedButton = null;
    for (const button of this.tree.querySelectorAll(".structure-select")) {
      const selected = button.dataset.key === this.selected;
      button.classList.toggle("selected", selected);
      button.setAttribute("aria-pressed", String(selected));
      if (selected && !selectedButton) selectedButton = button;
    }
    this.renderDetail(node);
    // Opening the detail panel shrinks the tree; keep the active row visible.
    selectedButton?.scrollIntoView({ block: "nearest", inline: "nearest" });
    if (notify) this.onSelect(node, this.artifactId);
  }

  selectBody(bodyId, artifactId) {
    if (artifactId !== this.artifactId) return;
    const body = this.nodes.find(node => node.bodyId === bodyId);
    if (body) this.select(body, { notify: false });
  }

  selectReference(ref) {
    if (ref.artifact_id !== this.artifactId) return;
    const kind = ["face", "edge"].includes(ref.entity_kind) ? "body" : ref.entity_kind;
    const id = ref.feature_id || ref.sketch_id || ref.body_id;
    const node = this.walk().find(node => node.bodyId === ref.body_id && node.kind === kind && node.id === id);
    if (node) this.select(node, { notify: false });
  }

  selectButton(node) {
    const button = this.element("button", `structure-select${this.selected === node.key ? " selected" : ""}`);
    button.type = "button";
    button.dataset.key = node.key;
    button.title = `${node.label} · ${node.type}${node.suppressed ? " · 已抑制" : ""}`;
    button.setAttribute("aria-pressed", String(this.selected === node.key));
    const icon = this.element("span", `structure-icon ${node.kind}`, node.kind === "body" ? "◇" : node.kind === "sketch" ? "▱" : "▰");
    icon.setAttribute("aria-hidden", "true");
    button.append(icon, this.element("span", "structure-name", node.label),
      this.element("span", "structure-type", node.suppressed ? "已抑制" : node.type));
    button.addEventListener("click", event => { event.preventDefault(); event.stopPropagation(); this.select(node); });
    return button;
  }

  row(node) {
    const li = this.element("li", node.suppressed ? "structure-node suppressed" : "structure-node");
    if (node.children.length) {
      const disclosure = this.element("details", "structure-branch");
      disclosure.open = this.search.value.trim() ? true : this.openState.get(node.key) ?? node.kind === "body";
      const summary = this.element("summary");
      summary.setAttribute("aria-label", `${node.label}，展开或折叠子项`);
      const arrow = this.element("span", "structure-chevron", "›");
      arrow.setAttribute("aria-hidden", "true");
      summary.append(arrow, this.selectButton(node));
      const children = this.element("ul", "structure-children");
      for (const child of node.children) children.append(this.row(child));
      disclosure.append(summary, children);
      disclosure.addEventListener("toggle", () => {
        if (disclosure.isConnected && !this.search.value.trim()) this.openState.set(node.key, disclosure.open);
      });
      summary.addEventListener("keydown", event => {
        if (event.target !== summary || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
        event.preventDefault(); disclosure.open = event.key === "ArrowRight";
      });
      li.append(disclosure);
    } else {
      const row = this.element("div", "structure-leaf");
      row.append(this.selectButton(node));
      li.append(row);
    }
    return li;
  }

  render() {
    this.renderMeta();
    const disabled = !this.nodes.length;
    const searching = Boolean(this.search.value.trim());
    this.search.disabled = disabled;
    this.expand.disabled = disabled || searching;
    this.collapse.disabled = disabled || searching;
    this.expand.title = searching ? "搜索结果自动展开，清空搜索后可展开全部" : "展开全部结构";
    this.collapse.title = searching ? "搜索结果自动展开，清空搜索后可折叠全部" : "折叠全部结构";
    const all = this.walk();
    this.count.textContent = `${this.nodes.length} 个零件 · ${all.filter(n => n.kind === "feature").length} 个特征`;
    this.tree.replaceChildren();
    if (disabled) {
      this.tree.append(this.element("div", "structure-empty", this.ir
        ? "模型中还没有零件。生成后会显示特征与草图。" : "生成模型后，在这里查看零件、特征与草图的关系。"));
      this.renderDetail(null);
      return;
    }
    const matches = filterModelTree(this.nodes, this.search.value);
    if (!matches.length) this.tree.append(this.element("div", "structure-empty", "没有匹配的零件或特征。"));
    else {
      this.tree.append(this.element("h4", "structure-section-title", "特征历史"));
      const history = this.element("ul", "structure-history chain");
      for (const node of matches) history.append(this.row(node));
      this.tree.append(history);
      if (this.ir.assembly) {
        const assembly = this.element("details", "structure-assembly");
        const joints = this.ir.assembly.joints || [];
        assembly.append(this.element("summary", "structure-section-title", `装配关系 · ${joints.length} 个约束`));
        for (const joint of joints) {
          const names = [joint.side1.body_id, joint.side2.body_id].map(id => this.nodes.find(n => n.id === id)?.label || id);
          assembly.append(this.element("div", "structure-joint", `${joint.id} · ${joint.type}${joint.suppressed ? " · 已抑制" : ""}\n${names.join(" ↔ ")}`));
        }
        if (!joints.length) assembly.append(this.element("div", "structure-joint", this.ir.assembly.rotation ? "旋转机构" : "未声明装配约束"));
        this.tree.append(assembly);
      }
      this.tree.append(this.element("h4", "structure-section-title parts-title", `零件 · ${this.nodes.length}`));
      const parts = this.element("ul", "structure-parts");
      for (const node of matches) {
        const li = this.element("li");
        li.append(this.selectButton(node)); parts.append(li);
      }
      this.tree.append(parts);
    }
    this.renderDetail(all.find(n => n.key === this.selected));
  }

  renderDetail(node) {
    this.beginReferences();
    this.detail.replaceChildren();
    this.detail.hidden = !node;
    if (!node) return;
    const heading = this.element("div", "structure-detail-heading");
    heading.append(this.element("strong", "", node.label));
    const reference = this.referenceButton(node, this.artifactId);
    if (reference) heading.append(reference);
    this.detail.append(heading);
    const row = (label, value) => {
      const item = this.element("div", "structure-property");
      item.append(this.element("span", "", label), this.element("span", "", valueText(value)));
      this.detail.append(item);
    };
    row("类型", node.type);
    row("标识", node.id);
    const nameOf = id => this.walk().find(n => n.id === id)?.label || id;
    const data = node.data;
    if (node.kind === "body") {
      row("特征 / 草图", `${data.features?.length || 0} / ${data.sketches?.length || 0}`);
      if (data.part_ref) row("来源模型", `${data.part_ref.model_id} / ${data.part_ref.body_id}`);
      if (data.motion) row("转动比例", data.motion.ratio);
    } else if (node.kind === "feature") {
      row("建模顺序", node.order);
      if (node.suppressed) row("状态", "已抑制");
      for (const [key, value] of Object.entries(data.params || {})) {
        const unit = linearParameters.has(key) ? " (mm)" : angularParameters.has(key) ? " (°)" : "";
        row((parameterNames[key] || key) + unit, value);
      }
      if (data.profile_sketch) row("轮廓草图", nameOf(data.profile_sketch));
      if (data.sections?.length) row("放样截面", data.sections.map(nameOf).join(" → "));
      const refs = [...new Set([...(data.refs || []), data.base_feature, data.plane?.feature_id].filter(Boolean))];
      if (refs.length) row("依赖特征", refs.map(nameOf).join("、"));
      if (data.sub_elements?.length) row("子元素", data.sub_elements.join("、"));
      if (data.placement) row("位置 (mm)", Object.values(data.placement.position).join(", "));
    } else {
      const plane = data.plane;
      row("附着平面", plane?.plane || [plane?.feature_id ? nameOf(plane.feature_id) : null, plane?.sub].filter(Boolean).join(" · ") || "—");
      row("几何 / 约束", `${data.geometry?.length || 0} / ${data.constraints?.length || 0}`);
      row("约束要求", data.require_fully_constrained ? "完全约束" : "允许自由度");
    }
    this.detail.append(this.element("p", "structure-detail-note", this.artifactId
      ? node.kind === "body" ? "三维视图高亮整个零件。" : node.kind === "sketch"
        ? "三维视图仅高亮此草图轮廓（包含被实体遮挡的线）。"
        : node.suppressed ? "此特征已抑制，没有可高亮几何。"
          : "三维视图仅高亮此特征产生且保留在当前零件上的轮廓。"
      : "当前显示编辑定义，构建后可与三维视图联动。"));
  }
}
