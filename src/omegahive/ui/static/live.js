/* Receives server-rendered fragments only; it never interprets or folds log data in the browser. */
(() => {
  const streamUrl = document.body.dataset.streamUrl;
  if (!streamUrl || !window.EventSource) return;

  const source = new EventSource(streamUrl);
  source.addEventListener("fragments", (message) => {
    const template = document.createElement("template");
    template.innerHTML = message.data;
    for (const replacement of template.content.children) {
      const existing = document.getElementById(replacement.id);
      if (existing) existing.replaceWith(replacement);
    }
  });
})();
