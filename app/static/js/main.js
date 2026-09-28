/**
 * PawBalik shared front-end helpers (ES module, loaded on every page by base.html).
 *
 *   import { api, ApiError, toast, escapeHtml, compressImage, config } from "./main.js";
 *
 * - config                 settings from <script id="pawbalik-config"> (map bounds, upload limits ...)
 * - csrfToken()            value of <meta name="csrf-token">
 * - api(path, options)     fetch() wrapper: sends the X-CSRFToken header, JSON in/out,
 *                          throws ApiError(code, message, status, details) for SPEC JSON errors
 * - toast(message, kind)   small notification; kind = "success" | "error" | "info"
 * - escapeHtml(value)      escape text before putting it into innerHTML (prefer textContent!)
 * - compressImage(file)    resize to ~1600 px JPEG before upload (low-end Android friendly)
 * - openModal(id) / closeModal(id)   <dialog class="modal"> helpers
 *
 * Auto-enhancements on DOMContentLoaded: mobile nav toggle, flash dismiss buttons,
 * photo dropzones ([data-dropzone]), modal openers ([data-open-modal="id"]),
 * location pickers ([data-location-picker] -> map.js is imported on demand).
 */

const configElement = document.getElementById("pawbalik-config");
export const config = configElement ? JSON.parse(configElement.textContent || "{}") : {};

export function csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") : "";
}

/** Error thrown by api() when the server answers {"error": {"code", "message"}}. */
export class ApiError extends Error {
  constructor(code, message, status = 0, details = null) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

/**
 * Call a PawBalik endpoint.
 *   await api("/api/scans", { method: "POST", body: formData })
 *   await api("/api/conversations/3/messages", { method: "POST", json: { type: "text", body } })
 *   await api("/api/notifications")               // GET
 * Returns parsed JSON (or null for 204). Throws ApiError on HTTP errors and network failures.
 */
export async function api(path, { method = "GET", json, body, headers = {}, signal } = {}) {
  const init = {
    method,
    credentials: "same-origin",
    headers: { Accept: "application/json", ...headers },
    signal,
  };
  if (method !== "GET" && method !== "HEAD") {
    init.headers["X-CSRFToken"] = csrfToken();
  }
  if (json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  } else if (body !== undefined) {
    init.body = body; // FormData: the browser sets the multipart boundary itself
  }

  let response;
  try {
    response = await fetch(path, init);
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    throw new ApiError("network_error", "No connection. Please check your internet and try again.");
  }
  if (response.status === 204) return null;

  let data = null;
  const type = response.headers.get("Content-Type") || "";
  if (type.includes("application/json")) {
    try {
      data = await response.json();
    } catch {
      data = null;
    }
  }
  if (!response.ok) {
    const error = (data && data.error) || {};
    throw new ApiError(
      error.code || `http_${response.status}`,
      error.message || "Something went wrong. Please try again.",
      response.status,
      error.details || null,
    );
  }
  return data;
}

/** Escape &, <, >, ", ' so a string is safe inside innerHTML / attributes. */
export function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Show a toast. Messages are inserted with textContent (never HTML). */
export function toast(message, kind = "info", { timeout = 5000 } = {}) {
  let region = document.getElementById("toast-region");
  if (!region) {
    region = document.createElement("div");
    region.id = "toast-region";
    region.className = "toast-region";
    region.setAttribute("aria-live", "polite");
    document.body.appendChild(region);
  }
  const item = document.createElement("div");
  item.className = `toast toast--${kind}`;
  item.setAttribute("role", kind === "error" ? "alert" : "status");
  const text = document.createElement("span");
  text.textContent = message;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast__close";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "×";
  close.addEventListener("click", () => item.remove());
  item.append(text, close);
  region.appendChild(item);
  if (timeout) setTimeout(() => item.remove(), timeout);
  return item;
}

/**
 * Resize an image File to at most maxEdge px (long edge) and re-encode as JPEG.
 * Returns a new File (or the original if it is small already / cannot be decoded).
 * EXIF orientation is applied by the browser; the output has no EXIF (no GPS).
 */
export async function compressImage(file, { maxEdge = config.upload?.maxEdge || 1600, quality = 0.85 } = {}) {
  if (!file || !file.type || !file.type.startsWith("image/")) return file;
  let bitmap;
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch {
    return file; // e.g. HEIC on browsers that cannot decode it: let the server handle it
  }
  const scale = Math.min(1, maxEdge / Math.max(bitmap.width, bitmap.height));
  if (scale === 1 && file.type === "image/jpeg" && file.size < 1.5 * 1024 * 1024) {
    bitmap.close?.();
    return file;
  }
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close?.();
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
  if (!blob) return file;
  const name = (file.name || "photo").replace(/\.[^.]+$/, "") + ".jpg";
  return new File([blob], name, { type: "image/jpeg", lastModified: Date.now() });
}

export function openModal(id) {
  const dialog = document.getElementById(id);
  if (dialog && typeof dialog.showModal === "function") dialog.showModal();
  else if (dialog) dialog.setAttribute("open", "");
  return dialog;
}

export function closeModal(id) {
  const dialog = typeof id === "string" ? document.getElementById(id) : id;
  if (dialog && typeof dialog.close === "function") dialog.close();
  else if (dialog) dialog.removeAttribute("open");
}

/* --- Auto-enhancements --------------------------------------------------------------- */

function initNav() {
  const toggle = document.querySelector("[data-nav-toggle]");
  const nav = document.getElementById("mobile-nav");
  if (!toggle || !nav) return;
  toggle.addEventListener("click", () => {
    const open = nav.hidden;
    nav.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
  });
  // Close the account menu when clicking elsewhere.
  document.addEventListener("click", (event) => {
    document.querySelectorAll("details.account-menu[open]").forEach((menu) => {
      if (!menu.contains(event.target)) menu.removeAttribute("open");
    });
  });
}

function initDismiss() {
  document.querySelectorAll("[data-dismiss]").forEach((button) => {
    button.addEventListener("click", () => button.closest(".flash, .callout, .toast")?.remove());
  });
}

function initModals() {
  document.querySelectorAll("[data-open-modal]").forEach((opener) => {
    opener.addEventListener("click", (event) => {
      const dialog = openModal(opener.getAttribute("data-open-modal"));
      if (dialog) event.preventDefault();
    });
  });
  document.querySelectorAll("dialog.modal").forEach((dialog) => {
    dialog.querySelectorAll("[data-close-modal]").forEach((btn) =>
      btn.addEventListener("click", () => closeModal(dialog)),
    );
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) closeModal(dialog); // click on the backdrop
    });
  });
}

/** Photo dropzone: previews, drag & drop, max files, compression before submit. */
function initDropzone(zone) {
  const input = zone.querySelector('input[type="file"]');
  const previews = zone.parentElement.querySelector("[data-dropzone-previews]");
  const maxFiles = parseInt(zone.dataset.maxFiles || "1", 10);
  if (!input) return;

  const render = () => {
    if (!previews) return;
    previews.replaceChildren();
    Array.from(input.files || []).forEach((file, index) => {
      const thumb = document.createElement("div");
      thumb.className = "dropzone__thumb";
      const img = document.createElement("img");
      img.alt = `Selected photo ${index + 1}`;
      img.src = URL.createObjectURL(file);
      img.addEventListener("load", () => URL.revokeObjectURL(img.src), { once: true });
      thumb.appendChild(img);
      if (index === 0 && maxFiles > 1) {
        const label = document.createElement("span");
        label.className = "dropzone__thumb-label";
        label.textContent = "Main";
        thumb.appendChild(label);
      }
      previews.appendChild(thumb);
    });
  };

  const setFiles = async (fileList) => {
    let files = Array.from(fileList).filter((f) => f.type.startsWith("image/") || /\.(heic|heif)$/i.test(f.name));
    if (files.length > maxFiles) {
      toast(`You can add up to ${maxFiles} photo${maxFiles > 1 ? "s" : ""}. Extra photos were left out.`, "info");
      files = files.slice(0, maxFiles);
    }
    const compressed = await Promise.all(files.map((f) => compressImage(f)));
    if (typeof DataTransfer === "function") {
      const transfer = new DataTransfer();
      compressed.forEach((f) => transfer.items.add(f));
      input.files = transfer.files;
    }
    render();
    input.dispatchEvent(new CustomEvent("photos-changed", { bubbles: true }));
  };

  input.addEventListener("change", () => {
    if (input.dataset.settingFiles) return;
    input.dataset.settingFiles = "1";
    setFiles(input.files).finally(() => delete input.dataset.settingFiles);
  });
  ["dragenter", "dragover"].forEach((type) =>
    zone.addEventListener(type, (event) => {
      event.preventDefault();
      zone.classList.add("is-dragover");
    }),
  );
  ["dragleave", "drop"].forEach((type) =>
    zone.addEventListener(type, () => zone.classList.remove("is-dragover")),
  );
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    if (event.dataTransfer?.files?.length) setFiles(event.dataTransfer.files);
  });
}

export function initDropzones(root = document) {
  root.querySelectorAll("[data-dropzone]").forEach((zone) => {
    if (zone.dataset.enhanced) return;
    zone.dataset.enhanced = "1";
    initDropzone(zone);
  });
}

async function initLocationPickers() {
  if (!document.querySelector("[data-location-picker]")) return;
  const map = await import("./map.js");
  map.initLocationPickers();
}

document.addEventListener("DOMContentLoaded", () => {
  initNav();
  initDismiss();
  initModals();
  initDropzones();
  initLocationPickers().catch((err) => console.warn("Location picker unavailable", err));
});
