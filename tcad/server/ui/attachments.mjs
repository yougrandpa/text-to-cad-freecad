const MAX_ATTACHMENTS = 4;
const MAX_TEXT_BYTES = 256 * 1024;
const MAX_BYTES = 5 * 1024 * 1024;
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);

function readImage(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error(`无法读取图片「${file.name}」`));
    reader.readAsDataURL(file);
  });
}

function isTextFile(file) { return /\.(txt|md)$/i.test(file.name || ""); }

async function readTextFile(file) {
  let content;
  try { content = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer()); }
  catch { throw new Error(`无法读取「${file.name}」，请使用 UTF-8 编码的文本文件。`); }
  if (/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(content)) throw new Error(`「${file.name}」含有二进制内容，请选择文本文件。`);
  return content;
}

export class AttachmentInput {
  constructor({ input, composer, picker, button, previews, error, changed = () => {},
    read = readImage, readText = readTextFile, document = previews.ownerDocument || globalThis.document }) {
    Object.assign(this, { input, composer, picker, button, previews, error, changed, read, readText, document });
    this.attachments = [];
    this.pending = false;
    this.busy = false;
    button.addEventListener("click", () => picker.click());
    picker.addEventListener("change", () => {
      const files = Array.from(picker.files || []);
      picker.value = "";
      this.add(files);
    });
    input.addEventListener("paste", event => {
      const items = Array.from(event.clipboardData?.items || []);
      const files = items.filter(item => item.kind === "file")
        .map(item => item.getAsFile()).filter(Boolean);
      if (!files.length) return;
      // Preserve plain text in a mixed clipboard; file-only paste is ours.
      if (!event.clipboardData.getData("text/plain")) event.preventDefault();
      this.add(files);
    });
    composer.addEventListener("dragover", event => {
      if (!Array.from(event.dataTransfer?.types || []).includes("Files")) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = this.busy ? "none" : "copy";
      composer.classList.toggle("attachment-drop", !this.busy);
    });
    composer.addEventListener("dragleave", event => {
      if (!composer.contains(event.relatedTarget)) composer.classList.remove("attachment-drop");
    });
    composer.addEventListener("drop", event => {
      composer.classList.remove("attachment-drop");
      const files = Array.from(event.dataTransfer?.files || []);
      if (!files.length) return;
      event.preventDefault();
      this.add(files);
    });
  }

  async add(files) {
    if (this.busy || this.pending || !files.length) return;
    this.pending = true;
    this.update();
    try {
      if (this.attachments.length + files.length > MAX_ATTACHMENTS) throw new Error("每条消息最多添加 4 个附件，请先移除部分附件。");
      for (const file of files) {
        if (isTextFile(file)) {
          if (file.size > MAX_TEXT_BYTES) throw new Error(`「${file.name}」不能超过 256 KB。`);
        } else {
          if (!IMAGE_TYPES.has(file.type)) throw new Error("请选择 .txt、.md 文件，或 PNG、JPEG、WebP、GIF 图片。");
          if (!file.size || file.size > MAX_BYTES) throw new Error(`「${file.name}」不能超过 5 MB。`);
        }
      }
      const attachments = [];
      for (const file of files) {
        const name = (file.name || "粘贴的图片").slice(-255);
        attachments.push(isTextFile(file)
          ? { name, content: await this.readText(file) }
          : { name, data_url: await this.read(file) });
      }
      this.attachments.push(...attachments);
      this.render();
    } catch (err) {
      this.error(err.message);
    } finally {
      this.pending = false;
      this.update();
    }
  }

  snapshot() { return this.attachments.slice(); }

  consume(attachments) {
    this.attachments = this.attachments.filter(attachment => !attachments.includes(attachment));
    this.render();
  }

  setBusy(busy) { this.busy = busy; this.update(); }

  update() {
    this.button.disabled = this.busy || this.pending || this.input.disabled;
    this.picker.disabled = this.button.disabled;
    for (const button of this.previews.querySelectorAll("button")) button.disabled = this.busy;
    this.changed(this.pending);
  }

  render() {
    this.previews.replaceChildren();
    this.previews.hidden = !this.attachments.length;
    for (const attachment of this.attachments) {
      const card = this.document.createElement("div");
      card.className = "input-attachment";
      const thumbnail = this.document.createElement(attachment.data_url ? "img" : "span");
      if (attachment.data_url) { thumbnail.src = attachment.data_url; thumbnail.alt = attachment.name; }
      else {
        card.classList.add("text-attachment");
        thumbnail.className = "attachment-file-icon";
        thumbnail.textContent = attachment.name.split(".").at(-1).toUpperCase();
        thumbnail.setAttribute("aria-hidden", "true");
        card.title = `${attachment.name} · ${new TextEncoder().encode(attachment.content).length} 字节`;
      }
      const label = this.document.createElement("span");
      label.textContent = attachment.name;
      label.title = attachment.name;
      const remove = this.document.createElement("button");
      remove.type = "button";
      remove.className = "attachment-remove";
      remove.textContent = "×";
      remove.setAttribute("aria-label", `移除附件 ${attachment.name}`);
      remove.addEventListener("click", () => { if (!this.busy) this.consume([attachment]); });
      card.append(thumbnail, label, remove);
      this.previews.append(card);
    }
    this.update();
  }
}
