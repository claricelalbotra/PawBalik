/**
 * scanner.js - Pet Scanner pages (SPEC 5.2, 16.4-16.7).
 *
 * scanner/choose.html ([data-scanner]):
 *   - Upload / Capture tiles: choosing a file starts the scan right away.
 *   - Optional live camera preview (getUserMedia) with a shutter button; falls back to the
 *     native camera input when the browser has no camera access or permission is denied.
 *   - Optional "Use my current location" (ranks results by distance).
 *   - Lost Pet Scanner: "Scan with the report photo" for the selected report.
 *   - Photos are compressed in the browser (main.js compressImage) and sent to POST /api/scans;
 *     the scanning state (scanner/scan.html) shows staged progress, then we open the results page.
 * scanner/results.html: the "Report a Pet Sighting" modal is opened by main.js
 *   ([data-open-modal]); here we only guard against double submits.
 *
 * Without JavaScript the same page works as a normal form (POST /scan).
 * User-controlled text is only ever written with textContent.
 */
import { api, compressImage, toast } from "./main.js";
import { inServiceArea } from "./map.js";

const STAGE_INTERVAL_MS = 1600;

function initScanner(root) {
  const form = root.querySelector("[data-scan-form]");
  const progress = root.querySelector("[data-scan-progress]");
  if (!form || !progress) return;

  const mode = root.dataset.mode;
  const apiUrl = root.dataset.api;
  let stages = [];
  try {
    stages = JSON.parse(root.dataset.stages || "[]");
  } catch {
    stages = [];
  }

  const inputs = Array.from(root.querySelectorAll("[data-scan-input]"));
  const latInput = form.querySelector("[data-scan-lat]");
  const lngInput = form.querySelector("[data-scan-lng]");
  const preview = progress.querySelector("[data-scan-preview]");
  const viewfinder = progress.querySelector("[data-scan-viewfinder]");
  const bar = progress.querySelector("[data-scan-bar]");
  const progressbar = progress.querySelector("[data-scan-progressbar]");
  const statusEl = progress.querySelector("[data-scan-status]");
  const errorBox = progress.querySelector("[data-scan-error]");
  const errorText = progress.querySelector("[data-scan-error-text]");
  const retryBtn = progress.querySelector("[data-scan-retry]");
  const reportBtn = form.querySelector("[data-scan-report-photo]");

  root.classList.add("is-enhanced");
  form.querySelector("[data-scan-nojs]")?.setAttribute("hidden", "");

  let busy = false;
  let stageTimer = null;
  let previewUrl = null;

  /* --- Progress UI ------------------------------------------------------------------- */

  const setProgress = (percent) => {
    bar.style.width = `${percent}%`;
    progressbar.setAttribute("aria-valuenow", String(Math.round(percent)));
  };

  const startStages = () => {
    let index = 0;
    statusEl.textContent = stages[0] || "Scanning for matches…";
    setProgress(8);
    stageTimer = window.setInterval(() => {
      index = Math.min(index + 1, Math.max(stages.length - 1, 0));
      if (stages[index]) statusEl.textContent = stages[index];
      // Creep towards 90 % until the server answers.
      const current = parseFloat(bar.style.width) || 0;
      setProgress(Math.min(90, current + (90 - current) * 0.35));
    }, STAGE_INTERVAL_MS);
  };

  const stopStages = () => {
    if (stageTimer) window.clearInterval(stageTimer);
    stageTimer = null;
  };

  const showScanning = (imageSrc) => {
    form.hidden = true;
    progress.hidden = false;
    errorBox.hidden = true;
    viewfinder.classList.add("is-scanning");
    if (imageSrc) {
      preview.src = imageSrc;
      preview.hidden = false;
    } else {
      preview.hidden = true;
    }
    progress.scrollIntoView({ behavior: "smooth", block: "start" });
    startStages();
  };

  const showError = (message) => {
    stopStages();
    viewfinder.classList.remove("is-scanning");
    setProgress(0);
    statusEl.textContent = "";
    errorText.textContent = message;
    errorBox.hidden = false;
    retryBtn.focus();
  };

  const reset = () => {
    busy = false;
    stopStages();
    progress.hidden = true;
    form.hidden = false;
    inputs.forEach((input) => {
      input.value = "";
    });
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
  };

  retryBtn?.addEventListener("click", reset);

  /* --- Scanning ---------------------------------------------------------------------- */

  const selectedReport = () => form.querySelector('input[name="lost_report_id"]:checked');

  async function startScan(file) {
    if (busy) return;
    busy = true;
    const data = new FormData();
    data.append("mode", mode);
    if (latInput.value && lngInput.value) {
      data.append("lat", latInput.value);
      data.append("lng", lngInput.value);
    }
    const report = selectedReport();
    if (report && report.value) data.append("lost_report_id", report.value);

    let imageSrc = null;
    if (file) {
      const compressed = await compressImage(file);
      data.append("photo", compressed, compressed.name || "photo.jpg");
      previewUrl = URL.createObjectURL(compressed);
      imageSrc = previewUrl;
    } else {
      data.append("use_report_photo", "1");
      imageSrc = report?.closest("label")?.querySelector("img")?.src || null;
    }

    showScanning(imageSrc);
    try {
      const result = await api(apiUrl, { method: "POST", body: data });
      stopStages();
      setProgress(100);
      statusEl.textContent = "Scan complete. Opening your results…";
      window.location.assign(result.url);
    } catch (err) {
      busy = false;
      showError(err.message || "Something went wrong. Please try again.");
    }
  }

  inputs.forEach((input) =>
    input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      if (file) startScan(file);
    }),
  );

  /* --- Lost Pet Scanner: scan with the selected report's photo -------------------------- */

  const syncReportButton = () => {
    if (!reportBtn) return;
    const report = selectedReport();
    const usable = Boolean(report && report.value && report.dataset.hasPhoto === "1");
    reportBtn.disabled = !usable;
  };
  form.querySelectorAll('input[name="lost_report_id"]').forEach((radio) =>
    radio.addEventListener("change", syncReportButton),
  );
  syncReportButton();
  reportBtn?.addEventListener("click", (event) => {
    event.preventDefault();
    startScan(null);
  });

  // Without a file, submitting the form (Enter key) would scan nothing: guide the user instead.
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    toast("Please upload or capture a photo of the pet.", "info");
  });

  /* --- Optional location --------------------------------------------------------------- */

  const locateBtn = form.querySelector("[data-scan-locate]");
  const locateStatus = form.querySelector("[data-scan-location-status]");
  if (locateBtn && "geolocation" in navigator) {
    locateBtn.hidden = false;
    locateBtn.addEventListener("click", () => {
      locateStatus.textContent = "Finding your location…";
      locateBtn.disabled = true;
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          locateBtn.disabled = false;
          const { latitude, longitude, accuracy } = pos.coords;
          if (!inServiceArea(latitude, longitude)) {
            latInput.value = "";
            lngInput.value = "";
            locateStatus.textContent =
              "You seem to be outside Tacloban City, so results will not be ranked by distance.";
            return;
          }
          latInput.value = latitude.toFixed(6);
          lngInput.value = longitude.toFixed(6);
          locateStatus.textContent = `Location added (accurate to about ${Math.round(accuracy)} m). Results will show distances from you.`;
        },
        (err) => {
          locateBtn.disabled = false;
          locateStatus.textContent =
            err.code === err.PERMISSION_DENIED
              ? "Location permission was denied. You can still scan without it."
              : "We could not get your location. You can still scan without it.";
        },
        { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 },
      );
    });
  }

  /* --- Optional live camera preview ---------------------------------------------------- */

  const liveBtn = form.querySelector("[data-live-camera]");
  const camera = form.querySelector("[data-camera]");
  const video = camera?.querySelector("[data-camera-video]");
  const tiles = form.querySelector("[data-scan-choose]");
  const captureInput = form.querySelector("#scan-capture");
  let stream = null;

  const stopCamera = () => {
    stream?.getTracks().forEach((track) => track.stop());
    stream = null;
    if (video) video.srcObject = null;
    if (camera) camera.hidden = true;
    if (tiles) tiles.hidden = false;
  };

  if (liveBtn && camera && video && navigator.mediaDevices?.getUserMedia && window.isSecureContext) {
    liveBtn.hidden = false;
    liveBtn.addEventListener("click", async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: "environment" }, width: { ideal: 1600 } },
          audio: false,
        });
      } catch {
        toast("The camera is not available. Opening your phone's camera instead.", "info");
        captureInput?.click();
        return;
      }
      video.srcObject = stream;
      tiles.hidden = true;
      camera.hidden = false;
      camera.querySelector("[data-camera-shutter]")?.focus();
    });

    camera.querySelector("[data-camera-cancel]")?.addEventListener("click", stopCamera);
    camera.querySelector("[data-camera-shutter]")?.addEventListener("click", () => {
      if (!video.videoWidth) {
        toast("The camera is still starting. Please try again.", "info");
        return;
      }
      const canvas = document.createElement("canvas");
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
      canvas.toBlob(
        (blob) => {
          stopCamera();
          if (!blob) {
            toast("Could not take the photo. Please try again.", "error");
            return;
          }
          startScan(new File([blob], "camera.jpg", { type: "image/jpeg", lastModified: Date.now() }));
        },
        "image/jpeg",
        0.9,
      );
    });
    window.addEventListener("pagehide", stopCamera);
  }
}

/* Results page: avoid double submits of the "Report a Pet Sighting" form. */
function initSightingForms() {
  document.querySelectorAll("form.modal__form, form[data-sighting-form]").forEach((form) => {
    form.addEventListener("submit", () => {
      form.querySelectorAll('button[type="submit"]').forEach((button) => {
        button.disabled = true;
      });
    });
  });
}

function init() {
  document.querySelectorAll("[data-scanner]").forEach(initScanner);
  initSightingForms();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
