/**
 * messages.js - chat thread polling, send text / location pin / photo (SPEC 5.6).
 * OWNER: messages team. Loaded by templates/messages/thread.html.
 *
 * The page is fully server-rendered (and works without JavaScript: the composer
 * forms post to the API and are redirected back). This module adds:
 *   - polling GET /api/conversations/<id>/messages?after=<last id> every
 *     config.messagePollSeconds while the tab is visible (paused when hidden),
 *   - POST /api/conversations/<id>/read when new messages arrive,
 *   - sending without a page reload, "Seen" receipts, a photo viewer.
 *
 * Security: every piece of user content is written with textContent or as an
 * attribute value on a cloned <template>; never through innerHTML.
 */
import { api, ApiError, closeModal, compressImage, config, openModal, toast } from "./main.js";
import { directionsUrl, inServiceArea, viewUrl } from "./map.js";

const chat = document.querySelector("[data-chat]");

/** Only same-site paths or https URLs may become href/src values. */
function safeUrl(value) {
  const url = String(value || "");
  if (url.startsWith("/") && !url.startsWith("//")) return url;
  if (url.startsWith("https://")) return url;
  return "#";
}

function slot(root, name) {
  return root.querySelector(`[data-slot="${name}"]`);
}

/** Build one bubble from a serialized message (see message_service.serialize_message). */
function buildBubble(message) {
  const template =
    document.getElementById(`msg-tpl-${message.type}`) || document.getElementById("msg-tpl-text");
  const bubble = template.content.firstElementChild.cloneNode(true);
  const side = message.type === "system" ? "system" : message.mine ? "mine" : "theirs";
  bubble.classList.remove("bubble--theirs", "bubble--mine", "bubble--system");
  bubble.classList.add(`bubble--${side}`);
  bubble.dataset.messageId = String(message.id);
  bubble.id = `m-${message.id}`;

  const text = slot(bubble, "text");
  if (message.type === "system") {
    const systemText = slot(bubble, "system-text");
    if (systemText) systemText.textContent = message.body || "";
  } else if (text) {
    text.textContent = message.body || "";
    text.hidden = !message.body;
  }

  if (message.type === "location_pin" && message.location) {
    const { lat, lng, label } = message.location;
    slot(bubble, "pin-title").textContent = label || "Shared location";
    slot(bubble, "pin-coords").textContent = `${Number(lat).toFixed(6)}, ${Number(lng).toFixed(6)}`;
    slot(bubble, "pin-view").href = viewUrl(lat, lng);
    slot(bubble, "pin-directions").href = directionsUrl(lat, lng);
  }

  if (message.type === "image" && message.photo) {
    slot(bubble, "photo-link").href = safeUrl(message.photo.full_url);
    slot(bubble, "photo-img").src = safeUrl(message.photo.thumb_url);
  }

  const time = slot(bubble, "time");
  time.textContent = message.time_label || "";
  time.dateTime = message.created_at || "";
  time.title = message.date_label || "";
  slot(bubble, "seen").hidden = true;
  return bubble;
}

function initThread() {
  const conversationId = chat.dataset.conversationId;
  const messagesUrl = chat.dataset.messagesUrl;
  const readUrl = chat.dataset.readUrl;
  const sendUrl = chat.dataset.sendUrl;
  const list = chat.querySelector("[data-messages]");
  const anchor = list.querySelector("#latest");
  const basePoll = Math.max(2, Number(config.messagePollSeconds) || 4) * 1000;

  let lastId = parseInt(chat.dataset.lastId || "0", 10) || 0;
  let lastReadMineId = parseInt(chat.dataset.lastReadMineId || "0", 10) || 0;
  const known = new Set(
    Array.from(list.querySelectorAll("[data-message-id]"), (el) => el.dataset.messageId),
  );
  let timer = null;
  let delay = basePoll;
  let stopped = false;
  let polling = false;

  const nearBottom = () => list.scrollHeight - list.scrollTop - list.clientHeight < 120;
  const scrollToBottom = () => {
    list.scrollTop = list.scrollHeight;
  };

  function showSeen() {
    list.querySelectorAll('[data-slot="seen"]').forEach((el) => {
      el.hidden = true;
    });
    if (!lastReadMineId) return;
    const bubble = list.querySelector(`[data-message-id="${lastReadMineId}"]`);
    const seen = bubble && slot(bubble, "seen");
    if (seen) seen.hidden = false;
  }

  /** Append messages we have not shown yet. Returns true if any came from the other side. */
  function append(messages, { forceScroll = false } = {}) {
    const stick = forceScroll || nearBottom();
    let fromOthers = false;
    messages.forEach((message) => {
      const key = String(message.id);
      if (known.has(key)) return;
      known.add(key);
      list.querySelector("[data-empty]")?.remove();
      list.insertBefore(buildBubble(message), anchor);
      if (!message.mine) fromOthers = true;
    });
    if (stick) scrollToBottom();
    return fromOthers;
  }

  function updateBadges(unreadTotal) {
    document.querySelectorAll('a[href$="/messages"] .badge-count').forEach((badge) => {
      if (!unreadTotal) badge.remove();
      else badge.textContent = unreadTotal < 100 ? String(unreadTotal) : "99+";
    });
  }

  async function markRead() {
    if (document.visibilityState !== "visible") return;
    try {
      const data = await api(readUrl, { method: "POST", json: {} });
      if (data) updateBadges(data.unread_total);
    } catch {
      /* not important - the next visit marks them read */
    }
  }

  function lockComposer(reason) {
    const composer = document.querySelector("[data-composer]");
    if (!composer) return;
    const note = document.createElement("div");
    note.className = "chat__readonly";
    const box = document.createElement("div");
    box.className = "callout callout--warn";
    const text = document.createElement("p");
    text.className = "mb-0";
    text.textContent = reason || "You can no longer send messages in this conversation.";
    box.appendChild(text);
    note.appendChild(box);
    composer.replaceWith(note);
    ["pin-modal", "photo-modal"].forEach((id) => {
      const dialog = document.getElementById(id);
      if (dialog) closeModal(dialog);
    });
  }

  function schedule(ms = delay) {
    clearTimeout(timer);
    if (!stopped && document.visibilityState === "visible") timer = setTimeout(poll, ms);
  }

  async function poll() {
    if (polling || stopped) return;
    polling = true;
    try {
      const data = await api(`${messagesUrl}?after=${encodeURIComponent(lastId)}`);
      const messages = data.messages || [];
      if (messages.length) lastId = Math.max(lastId, ...messages.map((m) => m.id));
      const fromOthers = append(messages);
      if (data.last_read_mine_id !== undefined && data.last_read_mine_id !== lastReadMineId) {
        lastReadMineId = data.last_read_mine_id;
      }
      showSeen();
      if (fromOthers) markRead();
      if (data.can_send === false && document.querySelector("[data-composer]")) {
        lockComposer(data.read_only_reason);
      }
      delay = basePoll;
    } catch (err) {
      if (err instanceof ApiError && [401, 403, 404].includes(err.status)) {
        stopped = true; // logged out or no longer allowed: stop quietly
        toast(err.message, "error");
      } else {
        delay = Math.min(delay * 2, 60000); // network trouble: back off
      }
    } finally {
      polling = false;
      schedule();
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      delay = basePoll;
      poll();
    } else {
      clearTimeout(timer);
    }
  });

  /** After our own send: show it right away (the poll skips it later by id). */
  function showSent(message) {
    append([message], { forceScroll: true });
    showSeen();
  }

  async function send(payload, button) {
    if (button) button.disabled = true;
    try {
      const data = await api(sendUrl, { method: "POST", ...payload });
      showSent(data.message);
      return true;
    } catch (err) {
      if (err instanceof ApiError && err.status === 403 && err.code === "read_only") {
        lockComposer(err.message);
      }
      toast(err.message || "Your message was not sent. Please try again.", "error");
      return false;
    } finally {
      if (button) button.disabled = false;
    }
  }

  // --- Text composer -----------------------------------------------------------------------
  const composer = document.querySelector("[data-composer]");
  if (composer) {
    const input = composer.querySelector("[data-composer-input]");
    const sendButton = composer.querySelector("[data-composer-send]");
    const autosize = () => {
      input.style.height = "auto";
      const wanted = input.scrollHeight + 2;
      input.style.height = `${Math.min(wanted, 140)}px`;
      input.style.overflowY = wanted > 140 ? "auto" : "hidden";
    };
    input.addEventListener("input", autosize);
    const finePointer = window.matchMedia && window.matchMedia("(pointer: fine)").matches;
    input.addEventListener("keydown", (event) => {
      // Desktop: Enter sends, Shift+Enter adds a line. Phones: Ctrl/Cmd+Enter sends.
      if (event.key === "Enter" && !event.shiftKey && (finePointer || event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        composer.requestSubmit();
      }
    });
    composer.addEventListener("submit", async (event) => {
      event.preventDefault();
      const body = input.value.trim();
      if (!body) {
        input.focus();
        return;
      }
      const ok = await send({ json: { type: "text", body } }, sendButton);
      if (ok) {
        input.value = "";
        autosize();
        input.focus();
      }
    });
  }

  // --- Location pin ------------------------------------------------------------------------
  const pinForm = document.querySelector("[data-pin-form]");
  if (pinForm) {
    pinForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const lat = parseFloat(pinForm.elements.lat?.value);
      const lng = parseFloat(pinForm.elements.lng?.value);
      if (!Number.isFinite(lat) || !Number.isFinite(lng)) {
        toast("Tap the map, use your current location, or type the coordinates first.", "error");
        return;
      }
      if (!inServiceArea(lat, lng) && config.outOfAreaPolicy !== "warn") {
        toast("That spot is outside Tacloban City. Please pin a location inside the city.", "error");
        return;
      }
      const button = pinForm.querySelector('button[type="submit"]');
      const ok = await send(
        {
          json: {
            type: "location_pin",
            lat,
            lng,
            location_label: pinForm.elements.place?.value || "",
            body: pinForm.elements.body?.value || "",
          },
        },
        button,
      );
      if (ok) {
        if (pinForm.elements.body) pinForm.elements.body.value = "";
        closeModal("pin-modal");
      }
    });
  }

  // --- Photo -------------------------------------------------------------------------------
  const photoForm = document.querySelector("[data-photo-form]");
  if (photoForm) {
    photoForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = photoForm.querySelector('input[type="file"]');
      const file = input?.files?.[0];
      if (!file) {
        toast("Choose a photo to send.", "error");
        return;
      }
      const maxBytes = config.upload?.maxBytes || 8 * 1024 * 1024;
      const upload = await compressImage(file);
      if (upload.size > maxBytes) {
        toast("That photo is too large. Please choose one under 8 MB.", "error");
        return;
      }
      const form = new FormData();
      form.set("type", "image");
      form.set("photo", upload, upload.name || "photo.jpg");
      form.set("body", photoForm.elements.body?.value || "");
      const ok = await send({ body: form }, photoForm.querySelector('button[type="submit"]'));
      if (ok) {
        photoForm.reset();
        photoForm.querySelector("[data-dropzone-previews]")?.replaceChildren();
        closeModal("photo-modal");
      }
    });
  }

  // --- Modals: "Cancel"/"×" are links for the no-JS page; with JS they just close. ---------
  document.querySelectorAll("dialog a[data-close-modal]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      closeModal(link.closest("dialog"));
    });
  });
  if (new URLSearchParams(window.location.search).has("compose")) {
    // ?compose=location|photo renders the dialog already open (no-JS fallback);
    // reopen it as a real modal and tidy the address bar.
    document.querySelectorAll("dialog.modal[open]").forEach((dialog) => {
      if (typeof dialog.showModal !== "function") return;
      dialog.close();
      dialog.showModal();
    });
    window.history.replaceState(null, "", window.location.pathname);
  }

  // --- Photo viewer ------------------------------------------------------------------------
  const viewerImg = document.querySelector("[data-photo-viewer-img]");
  list.addEventListener("click", (event) => {
    const link = event.target.closest("[data-photo-viewer]");
    if (!link || !viewerImg || !document.getElementById("photo-viewer")) return;
    event.preventDefault();
    viewerImg.src = safeUrl(link.getAttribute("href"));
    openModal("photo-viewer");
  });

  scrollToBottom();
  showSeen();
  schedule();
  chat.dataset.enhanced = conversationId;
}

if (chat) initThread();
