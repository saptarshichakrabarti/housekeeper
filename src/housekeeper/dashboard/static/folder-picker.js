// The "Choose folder…" button and path field are re-rendered by HTMX on every /fragments/control
// swap, so this listens on document (event delegation) rather than binding to one button instance.
document.addEventListener('click', async (event) => {
  const button = event.target.closest('#control-pick-folder');
  if (!button) return;
  event.preventDefault();
  const input = document.getElementById('control-path');
  const api = window.pywebview && window.pywebview.api;
  if (!api || !api.pick_folder) {
    // Plain browser: no native dialog available. The text field is the fallback.
    if (input) input.focus();
    return;
  }
  const path = await api.pick_folder();
  if (path && input) input.value = path;
});
