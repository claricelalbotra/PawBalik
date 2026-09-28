/**
 * lost_pet.js - Lost Pet Report pages (SPEC 5.1, 5.5, 16.8). Progressive enhancement only:
 * every page works without this file.
 *
 * - Report wizard  (form[data-report-wizard], lost_pet/create_report.html)
 *     5 steps with Back/Next, per-step validation, a review summary built from the form,
 *     and a final check on submit. Without JS the same form is one long page.
 * - Photo manager  (photo inputs inside the wizard and the edit form)
 *     keeps the chosen photos (main.js compresses them first), shows previews with
 *     "make main", move earlier/later and remove buttons. The first photo is the main one.
 * - Breed suggestions filtered by the chosen species.
 * - Case gallery   ([data-gallery], lost_pet/details.html) thumbnail -> main photo.
 * - "Near me"      ([data-browse-filters], lost_pet/browse.html) fills near=lat,lng.
 *
 * User-entered text is always inserted with textContent (never innerHTML).
 */
import { config, toast } from "./main.js";
import { inServiceArea } from "./map.js";

const ready = (fn) =>
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", fn) : fn();

/* --- Field errors ------------------------------------------------------------------------- */

let errorCounter = 0;

function fieldContainer(el) {
  if (el.name === "lat" || el.name === "lng") return el.closest("[data-location-picker]");
  if (el.type === "radio") return el.closest("fieldset.field") || el.closest(".field");
  return el.closest(".field") || el.closest("fieldset");
}

function setFieldError(el, message) {
  const field = fieldContainer(el);
  if (!field) return;
  field.classList.add("has-error");
  let note = field.querySelector(":scope > [data-js-error]");
  if (!note) {
    note = document.createElement("p");
    note.className = "field__error";
    note.dataset.jsError = "1";
    note.id = `js-error-${++errorCounter}`;
    field.appendChild(note);
  }
  note.textContent = message;
  el.setAttribute("aria-invalid", "true");
  const described = (el.getAttribute("aria-describedby") || "").split(" ").filter(Boolean);
  if (!described.includes(note.id)) el.setAttribute("aria-describedby", [...described, note.id].join(" "));
}

function clearFieldError(el) {
  const field = el && fieldContainer(el);
  if (!field) return;
  field.classList.remove("has-error");
  field.querySelectorAll(":scope > .field__error").forEach((note) => note.remove());
  field.querySelectorAll("[aria-invalid]").forEach((input) => input.removeAttribute("aria-invalid"));
}

function messageFor(el) {
  const v = el.validity;
  if (v.valueMissing) {
    if (el.type === "radio") return "Please choose an option.";
    if (el.name === "lat" || el.name === "lng") return "Pin the place on the map or type the latitude and longitude.";
    return "This field is required.";
  }
  if (v.typeMismatch && el.type === "email") return "Enter a valid email address.";
  if (v.rangeUnderflow) return el.name === "reward_amount" ? "The reward cannot be negative." : "This value is too small.";
  if (v.rangeOverflow) return el.type === "date" ? "The date cannot be in the future." : "This value is too large.";
  if (v.tooLong) return `Use at most ${el.maxLength} characters.`;
  if (v.badInput || v.stepMismatch) return "Please enter a valid value.";
  return el.validationMessage || "Please check this field.";
}

/* --- Validation shared by the wizard steps and the final submit ---------------------------- */

function todayIso() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** Returns a list of [element, message] problems inside `scope`. */
function findProblems(scope) {
  const problems = [];
  const seenRadios = new Set();
  scope.querySelectorAll("input, select, textarea").forEach((el) => {
    if (el.disabled || el.type === "hidden" || el.type === "file") return;
    if (el.type === "radio") {
      if (seenRadios.has(el.name)) return;
      seenRadios.add(el.name);
    }
    if (!el.checkValidity()) problems.push([el, messageFor(el)]);
  });

  scope.querySelectorAll('input[type="file"][name="photos"]').forEach((input) => {
    if (input.required && (!input.files || input.files.length === 0)) {
      problems.push([input, "Add at least one clear photo of your pet."]);
    }
  });

  const lat = scope.querySelector('input[name="lat"]');
  const lng = scope.querySelector('input[name="lng"]');
  if (lat && lng && lat.value !== "" && lng.value !== "" && lat.checkValidity() && lng.checkValidity()) {
    const inArea = inServiceArea(parseFloat(lat.value), parseFloat(lng.value));
    if (!inArea && config.outOfAreaPolicy !== "warn") {
      problems.push([lat, `This location is outside ${config.serviceAreaName || "Tacloban City"}. PawBalik currently serves Tacloban City only.`]);
    }
  }

  const date = scope.querySelector('input[name="last_seen_date"]');
  const time = scope.querySelector('input[name="last_seen_time"]');
  if (date && date.value && date.checkValidity()) {
    if (date.value > todayIso()) {
      problems.push([date, "The date cannot be in the future."]);
    } else if (time && time.value && date.value === todayIso()) {
      const [h, m] = time.value.split(":").map(Number);
      const now = new Date();
      if (h * 60 + m > now.getHours() * 60 + now.getMinutes() + 10) {
        problems.push([time, "The time cannot be in the future."]);
      }
    }
  }
  return problems;
}

function showProblems(problems) {
  problems.forEach(([el, message]) => setFieldError(el, message));
  if (problems.length) {
    const first = problems[0][0];
    const target = first.type === "file" ? first.closest("[data-dropzone]") || first : first;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    first.focus({ preventScroll: true });
  }
}

/* --- Photo manager ------------------------------------------------------------------------ */

function initPhotoManager(input) {
  if (typeof DataTransfer !== "function" || input.dataset.photoManager) return;
  input.dataset.photoManager = "1";
  const zone = input.closest("[data-dropzone]");
  const previews = zone?.parentElement?.querySelector("[data-dropzone-previews]");
  const max = parseInt(zone?.dataset.maxFiles || "5", 10);
  if (!previews) return;
  previews.tabIndex = -1;
  let files = [];

  const sync = () => {
    const transfer = new DataTransfer();
    files.forEach((file) => transfer.items.add(file));
    input.files = transfer.files; // does not fire "change"
    render();
    input.dispatchEvent(new CustomEvent("photos-updated", { bubbles: true }));
  };

  const move = (from, to) => {
    const [file] = files.splice(from, 1);
    files.splice(to, 0, file);
    sync();
    previews.focus();
  };

  const button = (text, label, onClick) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "photo-thumb__btn";
    btn.textContent = text;
    btn.setAttribute("aria-label", label);
    btn.title = label;
    btn.addEventListener("click", onClick);
    return btn;
  };

  function render() {
    previews.replaceChildren();
    files.forEach((file, index) => {
      const n = index + 1;
      const thumb = document.createElement("div");
      thumb.className = "dropzone__thumb photo-thumb";
      const img = document.createElement("img");
      img.alt = index === 0 ? `Photo ${n} (main photo)` : `Photo ${n}`;
      img.src = URL.createObjectURL(file);
      img.addEventListener("load", () => URL.revokeObjectURL(img.src), { once: true });
      thumb.appendChild(img);
      if (index === 0) {
        const label = document.createElement("span");
        label.className = "dropzone__thumb-label";
        label.textContent = "Main";
        thumb.appendChild(label);
      }
      const tools = document.createElement("div");
      tools.className = "photo-thumb__tools";
      if (index > 0) {
        tools.appendChild(button("★", `Make photo ${n} the main photo`, () => move(index, 0)));
        tools.appendChild(button("←", `Move photo ${n} earlier`, () => move(index, index - 1)));
      }
      if (index < files.length - 1) {
        tools.appendChild(button("→", `Move photo ${n} later`, () => move(index, index + 1)));
      }
      tools.appendChild(
        button("×", `Remove photo ${n}`, () => {
          files.splice(index, 1);
          sync();
          previews.focus();
        }),
      );
      thumb.appendChild(tools);
      previews.appendChild(thumb);
    });
    if (files.length) {
      const note = document.createElement("p");
      note.className = "field__hint photo-thumb__note";
      note.textContent = `${files.length} of ${max} photos. The first photo is the main photo used for matching.`;
      previews.appendChild(note);
    }
  }

  // main.js compresses a new selection and then fires "photos-changed": add it to the list.
  input.addEventListener("photos-changed", () => {
    const key = (file) => `${file.name}|${file.size}`;
    const known = new Set(files.map(key));
    const added = Array.from(input.files || []).filter((file) => !known.has(key(file)));
    let merged = files.concat(added);
    if (merged.length > max) {
      toast(`You can add up to ${max} photos. Extra photos were left out.`, "info");
      merged = merged.slice(0, max);
    }
    files = merged;
    sync();
    if (files.length) clearFieldError(input);
  });
}

/* --- Breed suggestions ------------------------------------------------------------------- */

function initBreedOptions(form) {
  const list = form.querySelector("[data-breed-options]");
  if (!list) return;
  const all = Array.from(list.querySelectorAll("option")).map((opt) => ({
    value: opt.value,
    species: opt.dataset.species,
  }));
  const update = () => {
    const species = form.querySelector('input[name="species"]:checked')?.value;
    const seen = new Set();
    const options = all
      .filter((o) => (!species || o.species === species) && !seen.has(o.value) && seen.add(o.value))
      .map((o) => {
        const opt = document.createElement("option");
        opt.value = o.value;
        return opt;
      });
    list.replaceChildren(...options);
  };
  form.querySelectorAll('input[name="species"]').forEach((radio) => radio.addEventListener("change", update));
  update();
}

/* --- Wizard ---------------------------------------------------------------------------------- */

function checkedLabel(form, name) {
  const radio = form.querySelector(`input[name="${name}"]:checked`);
  return radio ? radio.closest("label")?.textContent.trim() || radio.value : "";
}

function buildReview(form, container, goTo) {
  const value = (name) => (form.elements[name]?.value || "").trim();
  const photoInput = form.querySelector('input[type="file"][name="photos"]');
  const photos = Array.from(photoInput?.files || []);
  const date = value("last_seen_date");
  const time = value("last_seen_time");
  const reward = value("reward_amount");
  const groups = [
    [1, "Pet Information", [
      ["Type of pet", checkedLabel(form, "species")],
      ["Name", value("pet_name")],
      ["Breed", value("breed")],
      ["Fur color / pattern", value("colors")],
      ["Size", checkedLabel(form, "size")],
      ["Sex", checkedLabel(form, "sex")],
      ["Estimated age", value("age_text")],
      ["Photos", photos.length ? `${photos.length} photo${photos.length === 1 ? "" : "s"}` : ""],
    ]],
    [2, "Identifying Features", [["Features", value("identifying_features") || "None added"]]],
    [3, "Where & When Lost", [
      ["Place", value("place")],
      ["Map pin", value("lat") && value("lng") ? `${value("lat")}, ${value("lng")}` : ""],
      ["Last seen", date ? `${date}${time ? ` at ${time}` : ""}` : ""],
    ]],
    [4, "Contact & Reward", [
      ["Contact name", value("contact_name")],
      ["Email", value("contact_email")],
      ["Mobile number", value("contact_phone")],
      ["Phone shown publicly", form.elements.show_phone?.checked ? "Yes" : "No (masked)"],
      ["Reward", reward ? `₱${Number(reward).toLocaleString("en-PH")}` : "None"],
      ["Reward details", value("reward_text")],
    ]],
  ];

  container.replaceChildren();
  groups.forEach(([step, title, rows]) => {
    const group = document.createElement("div");
    group.className = "review-group";
    const head = document.createElement("div");
    head.className = "review-group__head";
    const h = document.createElement("h3");
    h.className = "h4 mb-0";
    h.textContent = title;
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "btn btn-link btn-sm";
    edit.textContent = "Edit";
    edit.setAttribute("aria-label", `Edit ${title}`);
    edit.addEventListener("click", () => goTo(step, true));
    head.append(h, edit);
    const list = document.createElement("dl");
    list.className = "details-list";
    rows.forEach(([label, text]) => {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = text || "—";
      list.append(dt, dd);
    });
    group.append(head, list);
    if (step === 1 && photos.length) {
      const strip = document.createElement("div");
      strip.className = "review-photos";
      photos.forEach((file, i) => {
        const img = document.createElement("img");
        img.alt = i === 0 ? "Main photo" : `Photo ${i + 1}`;
        img.src = URL.createObjectURL(file);
        img.addEventListener("load", () => URL.revokeObjectURL(img.src), { once: true });
        strip.appendChild(img);
      });
      group.appendChild(strip);
    }
    container.appendChild(group);
  });
}

function initWizard(form) {
  const steps = Array.from(form.querySelectorAll("[data-wizard-step]"));
  if (steps.length < 2) return;
  const progress = document.querySelector("[data-wizard-progress]");
  const stepper = progress?.querySelectorAll(".stepper__step") || [];
  const count = progress?.querySelector("[data-step-count]");
  const review = form.querySelector("[data-review-summary]");
  const submit = form.querySelector("[data-wizard-submit]");
  let current = 1;

  form.noValidate = true; // this script validates step by step instead
  if (progress) progress.hidden = false;
  form.querySelectorAll("[data-wizard-nav], [data-wizard-prev]").forEach((el) => (el.hidden = false));
  form.querySelectorAll("[data-nojs-only]").forEach((el) => (el.hidden = true));
  form.classList.add("is-wizard");

  function goTo(n, focus = false) {
    current = Math.min(Math.max(1, n), steps.length);
    steps.forEach((section) => {
      section.hidden = Number(section.dataset.wizardStep) !== current;
    });
    stepper.forEach((item, index) => {
      const number = index + 1;
      item.classList.toggle("is-done", number < current);
      item.classList.toggle("is-active", number === current);
      if (number === current) item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
      const circle = item.querySelector(".stepper__circle");
      if (circle) circle.textContent = number < current ? "✓" : String(number);
    });
    if (count) count.textContent = `Step ${current} of ${steps.length}`;
    if (current === steps.length && review) buildReview(form, review, goTo);
    if (focus) {
      (progress || form).scrollIntoView({ behavior: "smooth", block: "start" });
      steps[current - 1].querySelector("h2")?.focus({ preventScroll: true });
    }
  }

  form.addEventListener("click", (event) => {
    const next = event.target.closest("[data-wizard-next]");
    const prev = event.target.closest("[data-wizard-prev]");
    if (next) {
      const problems = findProblems(steps[current - 1]);
      if (problems.length) {
        showProblems(problems);
        toast("Please complete the highlighted fields.", "error");
        return;
      }
      goTo(current + 1, true);
    } else if (prev) {
      goTo(current - 1, true);
    }
  });

  ["input", "change"].forEach((type) =>
    form.addEventListener(type, (event) => {
      if (event.target.matches?.("input, select, textarea")) clearFieldError(event.target);
    }),
  );
  form.addEventListener("location-change", () => {
    const lat = form.querySelector('input[name="lat"]');
    if (lat) clearFieldError(lat);
  });

  form.addEventListener("submit", (event) => {
    for (const section of steps) {
      const problems = findProblems(section);
      if (problems.length) {
        event.preventDefault();
        goTo(Number(section.dataset.wizardStep));
        showProblems(problems);
        toast("Please complete the highlighted fields.", "error");
        return;
      }
    }
    if (submit) {
      // Disable after the browser has captured the submission (prevents double reports).
      setTimeout(() => {
        submit.disabled = true;
        submit.textContent = "Submitting…";
      }, 0);
    }
  });

  const start = parseInt(form.dataset.startStep || "1", 10);
  goTo(start, start > 1);
}

function initEditForm(form) {
  ["input", "change"].forEach((type) =>
    form.addEventListener(type, (event) => {
      if (event.target.matches?.("input, select, textarea")) clearFieldError(event.target);
    }),
  );
  form.addEventListener("submit", () => {
    const button = form.querySelector('button[type="submit"]');
    if (button) setTimeout(() => (button.disabled = true), 0);
  });
}

/* --- Case gallery -------------------------------------------------------------------------- */

function initGallery(root) {
  const main = root.querySelector("[data-gallery-main]");
  const link = root.querySelector("[data-gallery-link]");
  const thumbs = Array.from(root.querySelectorAll("[data-gallery-thumb]"));
  if (!main || thumbs.length < 2) return;
  thumbs.forEach((thumb) =>
    thumb.addEventListener("click", (event) => {
      event.preventDefault();
      main.src = thumb.dataset.full;
      main.alt = thumb.dataset.alt || "";
      if (link) link.href = thumb.dataset.full;
      thumbs.forEach((t) => {
        t.classList.toggle("is-active", t === thumb);
        if (t === thumb) t.setAttribute("aria-current", "true");
        else t.removeAttribute("aria-current");
      });
    }),
  );
}

/* --- Browse: "Near me" ------------------------------------------------------------------- */

function initNearMe(form) {
  const button = form.querySelector("[data-near-me]");
  const input = form.querySelector("[data-near-input]");
  const status = form.querySelector("[data-near-status]");
  if (!button || !input || !("geolocation" in navigator)) return;
  button.hidden = false;
  button.addEventListener("click", () => {
    button.disabled = true;
    if (status) status.textContent = "Finding your location…";
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const { latitude, longitude } = pos.coords;
        input.value = `${latitude.toFixed(5)},${longitude.toFixed(5)}`;
        if (!inServiceArea(latitude, longitude)) {
          toast("You seem to be outside Tacloban City, so few cases may be nearby.", "info");
        }
        form.submit();
      },
      (err) => {
        button.disabled = false;
        if (status) {
          status.textContent =
            err.code === 1
              ? "Location permission was denied. You can still search by place or barangay name."
              : "We could not get your location. Please try again.";
        }
      },
      { enableHighAccuracy: false, timeout: 15000, maximumAge: 60000 },
    );
  });
}

ready(() => {
  document.querySelectorAll("form[data-report-wizard], form[data-report-edit]").forEach((form) => {
    form.querySelectorAll('input[type="file"][name="photos"]').forEach(initPhotoManager);
    initBreedOptions(form);
    if (form.matches("[data-report-wizard]")) initWizard(form);
    else initEditForm(form);
  });
  document.querySelectorAll("[data-gallery]").forEach(initGallery);
  document.querySelectorAll("form[data-browse-filters]").forEach(initNearMe);
});
