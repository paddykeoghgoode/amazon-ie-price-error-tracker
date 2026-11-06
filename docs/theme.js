(function () {
  const KEY = 'resaleRadarTheme.v1';

  function apply(theme) {
    const root = document.documentElement;
    root.dataset.theme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', theme === 'light' ? '#f5f3ee' : '#111416');
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;
    const light = theme === 'light';
    btn.setAttribute('aria-pressed', light ? 'true' : 'false');
    btn.textContent = light ? 'Dark' : 'Light';
    btn.setAttribute('aria-label', light ? 'Switch to dark mode' : 'Switch to light mode');
  }

  const stored = localStorage.getItem(KEY);
  const theme = stored === 'light' || stored === 'dark' ? stored : 'dark';
  apply(theme);

  function initToggle() {
    const btn = document.getElementById('theme-toggle');
    if (!btn || btn.dataset.bound) return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', () => {
      const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
      try { localStorage.setItem(KEY, next); } catch (_) { /* private mode */ }
      apply(next);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initToggle);
  } else {
    initToggle();
  }
})();
