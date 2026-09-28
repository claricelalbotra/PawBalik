/**
 * notifications.js - "Enable match alerts" (Firebase web push), notification list
 * mark-as-read, and the OTP resend countdown. Loaded as a module by the notifications
 * pages and by the notifications/_enable_alerts.html partial (modules run once per page).
 *
 * Push uses the Firebase JS *compat* SDK from the official CDN (www.gstatic.com), the same
 * SDK style and version as the service worker at /firebase-messaging-sw.js (a classic
 * service worker can only importScripts() the compat build). The version comes from
 * config.firebase.jsVersion (FIREBASE_JS_VERSION, pinned in app/config.py).
 *
 * Notification permission is requested ONLY after a click on "Enable match alerts".
 * Everything stays hidden when push is not configured or the browser cannot do web push.
 */
import { api, ApiError, config, toast } from "./main.js";

const TOKEN_KEY = "pawbalik.fcmToken";
const firebaseSettings = (config && config.firebase) || {};

// --- Small helpers -------------------------------------------------------------------------

function storedToken() {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function storeToken(token) {
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode: nothing to remember */
  }
}

function pushSupported() {
  return (
    Boolean(firebaseSettings.enabled && firebaseSettings.config && firebaseSettings.vapidKey) &&
    "serviceWorker" in navigator &&
    "Notification" in window &&
    "PushManager" in window &&
    window.isSecureContext
  );
}

/** Update the unread badge in the header (and the chip on the list page). */
function setUnreadCount(count) {
  const n = Math.max(0, Number(count) || 0);
  document.querySelectorAll("[data-unread-notifications]").forEach((badge) => {
    if (n === 0) badge.remove();
    else badge.textContent = n < 100 ? String(n) : "99+";
  });
  document.querySelectorAll("[data-unread-chip]").forEach((chip) => {
    chip.hidden = n === 0;
    const label = chip.querySelector("[data-unread-label]");
    if (label) label.textContent = String(n);
  });
  document.querySelectorAll("[data-mark-all-form]").forEach((form) => {
    form.hidden = n === 0;
  });
}

// --- Firebase (compat SDK, loaded on demand) -----------------------------------------------

let firebaseLoading = null;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[data-firebase-src="${src}"]`);
    if (existing && existing.dataset.loaded === "1") {
      resolve();
      return;
    }
    const script = existing || document.createElement("script");
    script.addEventListener("load", () => {
      script.dataset.loaded = "1";
      resolve();
    });
    script.addEventListener("error", () => reject(new Error(`Could not load ${src}`)));
    if (!existing) {
      script.src = src;
      script.async = true;
      script.dataset.firebaseSrc = src;
      document.head.appendChild(script);
    }
  });
}

/** Load the Firebase compat scripts once and return firebase.messaging(). */
function loadMessaging() {
  if (!firebaseLoading) {
    const base = `https://www.gstatic.com/firebasejs/${firebaseSettings.jsVersion}`;
    firebaseLoading = loadScript(`${base}/firebase-app-compat.js`)
      .then(() => loadScript(`${base}/firebase-messaging-compat.js`))
      .then(() => {
        const fb = window.firebase;
        if (!fb.apps.length) fb.initializeApp(firebaseSettings.config);
        const messaging = fb.messaging();
        // Alerts that arrive while the page is open: show a toast (the OS shows the rest).
        messaging.onMessage((payload) => {
          const note = payload.notification || {};
          const data = payload.data || {};
          toast(note.title || data.title || "New PawBalik update", "info", { timeout: 8000 });
          const badge = document.querySelector("[data-unread-notifications]");
          if (badge) setUnreadCount((parseInt(badge.textContent, 10) || 0) + 1);
        });
        return messaging;
      })
      .catch((err) => {
        firebaseLoading = null;
        throw err;
      });
  }
  return firebaseLoading;
}

async function getPushToken(swUrl) {
  const registration = await navigator.serviceWorker.register(swUrl, { scope: "/" });
  const messaging = await loadMessaging();
  return messaging.getToken({
    vapidKey: firebaseSettings.vapidKey,
    serviceWorkerRegistration: registration,
  });
}

// --- "Enable match alerts" card ---------------------------------------------------------------

function initAlertsCard(card) {
  const enableBtn = card.querySelector("[data-alerts-enable]");
  const disableBtn = card.querySelector("[data-alerts-disable]");
  const status = card.querySelector("[data-alerts-status]");
  const swUrl = card.dataset.swUrl || "/firebase-messaging-sw.js";
  const devicesUrl = card.dataset.devicesUrl || "/api/devices";

  const show = (message, { on = false } = {}) => {
    if (status) status.textContent = message || "";
    if (enableBtn) enableBtn.hidden = on;
    if (disableBtn) disableBtn.hidden = !on;
  };

  const registerToken = async (token) => {
    await api(devicesUrl, { method: "POST", json: { token } });
    storeToken(token);
  };

  card.hidden = false;
  if (Notification.permission === "denied") {
    show("Notifications are blocked for this site. Allow them in your browser settings to get alerts.");
    if (enableBtn) enableBtn.disabled = true;
  } else if (Notification.permission === "granted" && storedToken()) {
    show("Match alerts are on for this device.", { on: true });
    // Refresh quietly: tokens can rotate, and this also attaches the foreground listener.
    getPushToken(swUrl)
      .then((token) => (token && token !== storedToken() ? registerToken(token) : null))
      .catch(() => {});
  }

  enableBtn?.addEventListener("click", async () => {
    enableBtn.disabled = true;
    try {
      // Ask first, directly inside the click (some browsers require the user gesture).
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        show(
          permission === "denied"
            ? "Notifications are blocked. You can allow them later in your browser settings."
            : "No problem - you can enable alerts any time.",
        );
        enableBtn.disabled = permission === "denied";
        return;
      }
      show("Turning on alerts...");
      const token = await getPushToken(swUrl);
      if (!token) throw new Error("No push token");
      await registerToken(token);
      show("Match alerts are on for this device.", { on: true });
      toast("Match alerts enabled.", "success");
    } catch (err) {
      const message =
        err instanceof ApiError ? err.message : "We could not turn on alerts in this browser. Please try again later.";
      show(message);
      toast(message, "error");
    } finally {
      enableBtn.disabled = Notification.permission === "denied";
    }
  });

  disableBtn?.addEventListener("click", async () => {
    disableBtn.disabled = true;
    const token = storedToken();
    try {
      if (token) {
        try {
          const messaging = await loadMessaging();
          await messaging.deleteToken();
        } catch {
          /* the server-side delete below is what matters */
        }
        try {
          await api(`${devicesUrl}/${encodeURIComponent(token)}`, { method: "DELETE" });
        } catch (err) {
          if (!(err instanceof ApiError && err.status === 404)) throw err;
        }
      }
      storeToken(null);
      show("Alerts are off for this device.");
      toast("Match alerts turned off for this device.", "info");
    } catch (err) {
      toast(err instanceof ApiError ? err.message : "Could not turn off alerts. Please try again.", "error");
    } finally {
      disableBtn.disabled = false;
    }
  });
}

// --- Notification list: mark read ------------------------------------------------------------

function markItemRead(item) {
  item.classList.remove("is-unread");
  item.querySelector("[data-unread-sr]")?.remove();
  item.querySelector("[data-mark-read]")?.remove();
}

function initNotificationList(list) {
  list.querySelectorAll("[data-mark-read]").forEach((button) => {
    button.hidden = false;
    button.addEventListener("click", async () => {
      const id = button.dataset.markRead;
      button.disabled = true;
      try {
        const data = await api(`/api/notifications/${encodeURIComponent(id)}/read`, { method: "POST" });
        const item = button.closest("[data-notification-id]");
        if (item) markItemRead(item);
        setUnreadCount(data ? data.unread : 0);
      } catch (err) {
        button.disabled = false;
        toast(err instanceof ApiError ? err.message : "Could not update the notification.", "error");
      }
    });
  });

  const markAllForm = list.querySelector("[data-mark-all-form]");
  markAllForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = markAllForm.querySelector("button");
    if (button) button.disabled = true;
    try {
      const data = await api("/api/notifications/read-all", { method: "POST" });
      list.querySelectorAll("[data-notification-id].is-unread").forEach(markItemRead);
      setUnreadCount(data ? data.unread : 0);
      toast("All notifications marked as read.", "success");
    } catch {
      markAllForm.submit(); // fall back to the normal form post
    } finally {
      if (button) button.disabled = false;
    }
  });
}

// --- Verify phone: resend countdown ------------------------------------------------------------

function initResendCountdown(button) {
  let seconds = parseInt(button.dataset.resendIn, 10) || 0;
  if (seconds <= 0) return;
  const counter = document.querySelector("[data-resend-seconds]");
  const wait = document.querySelector("[data-resend-wait]");
  const timer = window.setInterval(() => {
    seconds -= 1;
    if (counter) counter.textContent = String(Math.max(0, seconds));
    if (seconds <= 0) {
      window.clearInterval(timer);
      button.disabled = false;
      button.removeAttribute("aria-describedby");
      if (wait) wait.hidden = true;
    }
  }, 1000);
}

// --- Boot --------------------------------------------------------------------------------------

if (pushSupported()) {
  document.querySelectorAll("[data-enable-alerts]").forEach(initAlertsCard);
}
document.querySelectorAll("[data-notification-list]").forEach(initNotificationList);
document.querySelectorAll("[data-resend-button]").forEach(initResendCountdown);
