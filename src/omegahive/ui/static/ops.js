/* An operation form waits for the whole operation (a sandboxed launch takes minutes), so a
   press says it registered: the button is disabled and the form's data-pending line shows,
   inside the form, or just after it in a launch row (whose auto-width form would otherwise
   grow around the line and squeeze the order's title).
   A page restored by Back gets its buttons back, so the same form can be resubmitted. */
(() => {
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form.dataset.pending) return;
    if (form.dataset.submitted) { event.preventDefault(); return; }
    form.dataset.submitted = "1";
    const button = form.querySelector('button[type="submit"]');
    if (button) { button.dataset.label = button.textContent; button.disabled = true; button.textContent = "Working…"; }
    const line = document.createElement("p");
    line.className = "op-pending";
    line.setAttribute("role", "status");
    line.textContent = form.dataset.pending;
    if (form.closest(".launch-row")) form.after(line); else form.append(line);
  });
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    for (const form of document.querySelectorAll("form[data-submitted]")) {
      delete form.dataset.submitted;
      const button = form.querySelector('button[type="submit"]');
      if (button) { button.disabled = false; button.textContent = button.dataset.label || button.textContent; }
      form.querySelectorAll(".op-pending").forEach((line) => line.remove());
      if (form.nextElementSibling?.classList.contains("op-pending")) form.nextElementSibling.remove();
    }
  });
})();
