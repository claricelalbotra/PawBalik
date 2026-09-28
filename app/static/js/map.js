/**
 * Maps helpers (SPEC 5.7, 17.1). Mirrors app/utils/maps.py - the ONLY places Maps URLs are built.
 *
 *   import { viewUrl, directionsUrl, inServiceArea, initLocationPicker } from "./map.js";
 *
 * - viewUrl(lat, lng)                 https://www.google.com/maps/search/?api=1&query=LAT,LNG
 * - directionsUrl(lat, lng, mode)     https://www.google.com/maps/dir/?api=1&destination=LAT,LNG&travelmode=MODE
 * - inServiceArea(lat, lng)           inside the Tacloban bounding box from config
 * - initLocationPicker(fieldset)      enhance one [data-location-picker] (partials/forms.html
 *                                     location_picker macro): Leaflet + OSM tap-to-pin map,
 *                                     "Use my current location" (accuracy shown), manual
 *                                     lat/lng sync, bounds check, "Check on Google Maps" link.
 * - initLocationPickers()             enhance every picker on the page (main.js calls it).
 *
 * If Leaflet cannot load (offline, MAP_PICKER=none) the manual fields and the
 * geolocation button still work. Each picker dispatches a "location-change"
 * event with detail {lat, lng, inArea} when the pin moves.
 */
import { config } from "./main.js";

const TRAVEL_MODES = ["driving", "walking", "bicycling", "transit"];

function coords(lat, lng) {
  return `${Number(lat).toFixed(6)},${Number(lng).toFixed(6)}`;
}

export function viewUrl(lat, lng) {
  return `https://www.google.com/maps/search/?api=1&query=${coords(lat, lng)}`;
}

export function directionsUrl(lat, lng, mode = "driving") {
  if (!TRAVEL_MODES.includes(mode)) throw new Error(`mode must be one of ${TRAVEL_MODES.join(", ")}`);
  return `https://www.google.com/maps/dir/?api=1&destination=${coords(lat, lng)}&travelmode=${mode}`;
}

export function inServiceArea(lat, lng) {
  const b = config.bounds;
  if (!b || !Number.isFinite(lat) || !Number.isFinite(lng)) return false;
  return lat >= b.south && lat <= b.north && lng >= b.west && lng <= b.east;
}

/* --- Leaflet loading (script + stylesheet from the CDN in config, with SRI) ------------ */

let leafletPromise = null;

function loadLeaflet() {
  if (window.L) return Promise.resolve(window.L);
  if (leafletPromise) return leafletPromise;
  const leaflet = config.leaflet || {};
  leafletPromise = new Promise((resolve, reject) => {
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = leaflet.css;
    if (leaflet.cssSri) {
      css.integrity = leaflet.cssSri;
      css.crossOrigin = "";
    }
    document.head.appendChild(css);

    const script = document.createElement("script");
    script.src = leaflet.js;
    if (leaflet.jsSri) {
      script.integrity = leaflet.jsSri;
      script.crossOrigin = "";
    }
    script.onload = () => (window.L ? resolve(window.L) : reject(new Error("Leaflet missing")));
    script.onerror = () => reject(new Error("Leaflet failed to load"));
    document.head.appendChild(script);
  });
  return leafletPromise;
}

/* --- Picker ------------------------------------------------------------------------------ */

function parseNumber(value) {
  const n = parseFloat(String(value ?? "").trim());
  return Number.isFinite(n) ? n : null;
}

export async function initLocationPicker(root) {
  if (!root || root.dataset.enhanced) return null;
  root.dataset.enhanced = "1";

  const latInput = root.querySelector("[data-lat]");
  const lngInput = root.querySelector("[data-lng]");
  const statusEl = root.querySelector("[data-location-status]");
  const mapsLink = root.querySelector("[data-check-maps]");
  const locateBtn = root.querySelector("[data-use-location]");
  const mapEl = root.querySelector("[data-map]");

  let map = null;
  let marker = null;
  let accuracyCircle = null;

  const setStatus = (text, kind = "") => {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.classList.toggle("is-error", kind === "error");
    statusEl.classList.toggle("is-ok", kind === "ok");
  };

  const update = (lat, lng, { moveMap = true, source = "" } = {}) => {
    if (lat === null || lng === null) return;
    latInput.value = lat.toFixed(6);
    lngInput.value = lng.toFixed(6);
    const inArea = inServiceArea(lat, lng);
    if (mapsLink) mapsLink.href = viewUrl(lat, lng);
    if (!inArea) {
      setStatus(
        `This spot is outside ${config.serviceAreaName || "the service area"}. PawBalik currently serves Tacloban City only.`,
        "error",
      );
    } else if (source !== "gps") {
      setStatus("Location pinned.", "ok");
    }
    if (map && window.L) {
      if (!marker) {
        marker = window.L.marker([lat, lng], { draggable: true, keyboard: true }).addTo(map);
        marker.on("dragend", () => {
          const p = marker.getLatLng();
          update(p.lat, p.lng, { moveMap: false });
        });
      } else {
        marker.setLatLng([lat, lng]);
      }
      if (moveMap) map.setView([lat, lng], Math.max(map.getZoom(), 16));
    }
    root.dispatchEvent(new CustomEvent("location-change", { bubbles: true, detail: { lat, lng, inArea } }));
  };

  // Manual fine-tuning of the numbers moves the pin.
  const onManual = () => {
    const lat = parseNumber(latInput.value);
    const lng = parseNumber(lngInput.value);
    if (lat !== null && lng !== null) update(lat, lng, { source: "manual" });
  };
  latInput?.addEventListener("change", onManual);
  lngInput?.addEventListener("change", onManual);

  // "Use my current location" (Geolocation API; needs HTTPS or localhost).
  locateBtn?.addEventListener("click", () => {
    if (!("geolocation" in navigator)) {
      setStatus("Your browser cannot share its location. Tap the map or type the coordinates.", "error");
      return;
    }
    setStatus("Finding your location…");
    locateBtn.disabled = true;
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        locateBtn.disabled = false;
        const { latitude, longitude, accuracy } = pos.coords;
        update(latitude, longitude, { source: "gps" });
        if (inServiceArea(latitude, longitude)) {
          setStatus(`Location found (accurate to about ${Math.round(accuracy)} m). Drag the pin to adjust.`, "ok");
        }
        if (map && window.L) {
          accuracyCircle?.remove();
          accuracyCircle = window.L.circle([latitude, longitude], {
            radius: accuracy,
            color: "#4e8b3a",
            weight: 1,
            fillOpacity: 0.12,
          }).addTo(map);
        }
      },
      (err) => {
        locateBtn.disabled = false;
        const reason =
          err.code === err.PERMISSION_DENIED
            ? "Location permission was denied."
            : "We could not get your location.";
        setStatus(`${reason} Tap the map or type the coordinates instead.`, "error");
      },
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 },
    );
  });

  // The tap-to-pin map.
  if (mapEl && config.mapPicker === "leaflet") {
    try {
      const L = await loadLeaflet();
      const b = config.bounds;
      const center = config.center || { lat: 11.2437, lng: 125.0046 };
      map = L.map(mapEl, {
        center: [center.lat, center.lng],
        zoom: config.zoom || 13,
        maxBounds: b ? L.latLngBounds([b.south - 0.1, b.west - 0.1], [b.north + 0.1, b.east + 0.1]) : undefined,
      });
      L.tileLayer(config.leaflet.tiles, {
        maxZoom: 19,
        attribution: config.leaflet.attribution,
      }).addTo(map);
      map.on("click", (event) => update(event.latlng.lat, event.latlng.lng, { moveMap: false }));
      const lat = parseNumber(latInput?.value);
      const lng = parseNumber(lngInput?.value);
      if (lat !== null && lng !== null) update(lat, lng);
      // Maps inside hidden wizard steps / modals need a size refresh when shown.
      new ResizeObserver(() => map.invalidateSize()).observe(mapEl);
    } catch (err) {
      mapEl.hidden = true;
      setStatus("The map could not load. Use your current location or type the coordinates.", "error");
      console.warn(err);
    }
  } else if (mapEl) {
    mapEl.hidden = true;
  }

  return {
    get map() {
      return map;
    },
    setLocation: (lat, lng) => update(lat, lng),
  };
}

export function initLocationPickers(root = document) {
  return Promise.all(Array.from(root.querySelectorAll("[data-location-picker]")).map(initLocationPicker));
}
