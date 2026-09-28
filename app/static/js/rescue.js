/**
 * rescue.js - Rescue Directory ("Use my current location", auto-submitting filters)
 * and Request Rescue Help (copy the summary / phone numbers).
 * OWNER: rescue_admin team.
 *
 * Everything here is progressive enhancement: without JavaScript the filter form
 * and the "Create summary" form still work.
 * User-controlled text only ever goes in through textContent / .value.
 */
import { api, ApiError, toast } from "./main.js";

/* --- Clipboard ------------------------------------------------------------------------- */

/** Copy text with the Clipboard API, falling back to a hidden textarea + execCommand. */
async function copyText(text, sourceField = null) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* fall through to the legacy path */
    }
  }
  const field = sourceField || document.createElement("textarea");
  if (!sourceField) {
    field.value = text;
    field.setAttribute("readonly", "");
    field.className = "visually-hidden";
    document.body.appendChild(field);
  }
  field.focus();
  field.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  if (!sourceField) field.remove();
  return ok;
}

/* --- Directory ------------------------------------------------------------------------- */

function initDirectory(form) {
  const nearInput = form.querySelector("[data-near-input]");
  const status = form.querySelector("[data-location-status]");
  const button = form.querySelector("[data-use-my-location]");

  form.querySelectorAll("[data-auto-submit]").forEach((select) => {
    select.addEventListener("change", () => form.requestSubmit ? form.requestSubmit() : form.submit());
  });

  if (!button) return;
  if (!("geolocation" in navigator)) {
    button.hidden = true;
    return;
  }
  button.addEventListener("click", () => {
    button.disabled = true;
    if (status) status.textContent = "Finding your location…";
    navigator.geolocation.getCurrentPosition(
      (position) => {
        const { latitude, longitude, accuracy } = position.coords;
        nearInput.value = `${latitude.toFixed(6)},${longitude.toFixed(6)}`;
        // A case/sighting origin would win over "near" on the server, so drop it.
        form.querySelectorAll('input[name="lost_report_id"], input[name="sighting_id"]').forEach((el) => el.remove());
        if (status) status.textContent = `Location found (about ${Math.round(accuracy)} m). Sorting by distance…`;
        form.submit();
      },
      (error) => {
        button.disabled = false;
        const message =
          error.code === error.PERMISSION_DENIED
            ? "Location permission was denied. You can still browse the list."
            : "We could not get your location. Please try again.";
        if (status) status.textContent = message;
        toast(message, "error");
      },
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 },
    );
  });
}

/* --- Request Rescue Help --------------------------------------------------------------- */

function initRequestForm(form) {
  const summary = document.querySelector("[data-summary]");
  const copyButton = document.querySelector("[data-copy-summary]");
  const status = document.querySelector("[data-copy-status]");
  if (!summary || !copyButton) return;

  // Editing the form after a save means the next copy must save a new summary.
  form.addEventListener("input", () => {
    summary.dataset.saved = "0";
  });
  form.addEventListener("change", () => {
    summary.dataset.saved = "0";
  });

  copyButton.addEventListener("click", async () => {
    copyButton.disabled = true;
    try {
      if (summary.dataset.saved !== "1") {
        const data = new FormData(form);
        const payload = {
          [form.dataset.subjectKey]: Number(form.dataset.subjectId),
          provider_id: data.get("provider_id") ? Number(data.get("provider_id")) : null,
          note: (data.get("note") || "").toString(),
          include_phone: data.get("include_phone") === "1",
        };
        const result = await api("/api/rescue-requests", { method: "POST", json: payload });
        summary.value = result.summary;
        summary.dataset.saved = "1";
      }
      const ok = await copyText(summary.value, summary);
      const message = ok
        ? "Summary copied. Paste it into SMS or Messenger."
        : "Summary saved. Select the text and copy it manually.";
      if (status) status.textContent = message;
      toast(message, ok ? "success" : "info");
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Could not copy the summary.";
      if (status) status.textContent = message;
      toast(message, "error");
    } finally {
      copyButton.disabled = false;
    }
  });
}

function initCopyButtons(root = document) {
  root.querySelectorAll("[data-copy-text]").forEach((button) => {
    button.addEventListener("click", async () => {
      const ok = await copyText(button.dataset.copyText || "");
      toast(ok ? "Number copied." : "Could not copy. Please copy it manually.", ok ? "success" : "error");
    });
  });
}

const filters = document.querySelector("[data-rescue-filters]");
if (filters) initDirectory(filters);
const requestForm = document.querySelector("[data-rescue-request]");
if (requestForm) initRequestForm(requestForm);
initCopyButtons();
