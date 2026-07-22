// The "Choose folder…" button and the folder browser are re-rendered by HTMX, so this uses event
// delegation rather than binding to one element instance. Two modes:
//   * Desktop app (pywebview): a real OS folder dialog via window.pywebview.api.pick_folder.
//   * Plain browser: no native dialog exists, so open an in-page modal (#folder-browser) whose
//     contents come from the read-only /fragments/folders endpoint; picking writes the chosen path
//     into #control-path.
// Capture phase (the `true` below) so we can preempt HTMX's own click handler when using the
// native dialog; in browser mode we let the click through so HTMX loads the folder list.
document.addEventListener(
  'click',
  async (event) => {
    const modal = () => document.getElementById('folder-browser');

    const openButton = event.target.closest('#control-pick-folder');
    if (openButton) {
      const api = window.pywebview && window.pywebview.api;
      if (api && api.pick_folder) {
        event.preventDefault();
        event.stopPropagation(); // native dialog wins; do not also HTMX-browse
        const path = await api.pick_folder();
        const input = document.getElementById('control-path');
        if (path && input) input.value = path;
        return;
      }
      const box = modal();
      if (box) box.hidden = false; // reveal; HTMX (hx-get on the button) fills the body
      return;
    }

    const useButton = event.target.closest('.folder-use');
    if (useButton) {
      const input = document.getElementById('control-path');
      if (input && useButton.dataset.path) input.value = useButton.dataset.path;
      const box = modal();
      if (box) box.hidden = true;
      return;
    }

    if (event.target.closest('.folder-close')) {
      const box = modal();
      if (box) box.hidden = true;
      return;
    }

    // Click on the backdrop itself (not its panel) closes the modal.
    const box = modal();
    if (box && !box.hidden && event.target === box) box.hidden = true;
  },
  true,
);
