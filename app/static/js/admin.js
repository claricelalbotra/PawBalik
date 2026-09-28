/**
 * admin.js - small enhancements for the admin pages.
 * OWNER: rescue_admin team.
 *
 * - <form data-confirm="..."> asks before destructive actions (delete, deactivate).
 * - <select data-auto-submit> re-runs the filter form when changed.
 * - Moderation forms need a reason: focus the reason box instead of submitting empty.
 * Everything still works without JavaScript (the server validates every field).
 */

document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm || "Are you sure?")) event.preventDefault();
  });
});

document.querySelectorAll("select[data-auto-submit]").forEach((select) => {
  select.addEventListener("change", () => {
    const form = select.form;
    if (!form) return;
    if (form.requestSubmit) form.requestSubmit();
    else form.submit();
  });
});

document.querySelectorAll("form.moderation-form").forEach((form) => {
  form.addEventListener("submit", (event) => {
    const reason = form.querySelector('input[name="reason"][required]');
    if (reason && !reason.value.trim()) {
      event.preventDefault();
      reason.setAttribute("aria-invalid", "true");
      reason.focus();
    }
  });
});

export {};
