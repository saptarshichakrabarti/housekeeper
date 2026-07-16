document.addEventListener('htmx:configRequest', (event) => {
  const token = document.body.dataset.csrfToken;
  if (token) event.detail.headers['X-CSRF-Token'] = token;
});
