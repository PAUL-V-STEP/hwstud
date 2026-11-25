(() => {
  const root = document.documentElement;
  const themeButton = document.querySelector('[data-theme-toggle]');
  const themeLabel = document.querySelector('[data-theme-label]');
  let storedTheme = null;
  try {
    storedTheme = localStorage.getItem('hwstud-theme');
  } catch (_) {
    storedTheme = null;
  }

  function preferredTheme() {
    if (storedTheme === 'light' || storedTheme === 'dark') return storedTheme;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function applyTheme(theme) {
    root.dataset.theme = theme;
    if (themeButton) {
      const dark = theme === 'dark';
      themeButton.setAttribute('aria-pressed', String(dark));
      themeButton.setAttribute('aria-label', dark ? 'Включить тему Тетрадь' : 'Включить тему Синька');
      if (themeLabel) themeLabel.textContent = dark ? 'Тетрадь' : 'Синька';
    }
  }

  applyTheme(preferredTheme());
  if (themeButton) {
    themeButton.addEventListener('click', () => {
      const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
      storedTheme = next;
      try {
        localStorage.setItem('hwstud-theme', next);
      } catch (_) {
        // Тема работает и без доступа к хранилищу.
      }
      applyTheme(next);
    });
  }

  const menuButton = document.querySelector('[data-menu-toggle]');
  const menu = document.querySelector('#mobile-menu');
  if (menuButton && menu) {
    menuButton.addEventListener('click', () => {
      const expanded = menuButton.getAttribute('aria-expanded') === 'true';
      menuButton.setAttribute('aria-expanded', String(!expanded));
      menu.hidden = expanded;
    });
  }

  const formScrollKey = 'hwstud-form-scroll';
  let savedFormScroll = null;
  try {
    const saved = sessionStorage.getItem(formScrollKey);
    sessionStorage.removeItem(formScrollKey);
    if (saved) {
      const parsed = JSON.parse(saved);
      if (parsed.path === window.location.pathname && Number.isFinite(parsed.y) && parsed.y >= 0) {
        savedFormScroll = parsed.y;
      }
    }
  } catch (_) {
    // Форма отправляется и без доступа к sessionStorage.
    savedFormScroll = null;
  }

  function restoreFormScroll() {
    if (savedFormScroll === null) return;
    const previousBehavior = root.style.scrollBehavior;
    root.style.scrollBehavior = 'auto';
    window.scrollTo(0, savedFormScroll);
    window.requestAnimationFrame(() => {
      window.scrollTo(0, savedFormScroll);
      root.style.scrollBehavior = previousBehavior;
    });
  }

  restoreFormScroll();
  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.matches('[data-upload-form]') || form.dataset.preserveScroll === 'false') return;
    try {
      sessionStorage.setItem(formScrollKey, JSON.stringify({
        path: window.location.pathname,
        y: window.scrollY,
      }));
    } catch (_) {
      // Отсутствие хранилища не мешает отправке формы.
    }
  });

  const processLinks = Array.from(document.querySelectorAll('.process-step'));
  processLinks.forEach((link, index) => {
    link.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const delta = event.key === 'ArrowRight' ? 1 : -1;
      const next = (index + delta + processLinks.length) % processLinks.length;
      processLinks[next].focus();
    });
  });
  document.querySelectorAll('[data-copy]').forEach((button) => {
    button.addEventListener('click', async () => {
      const target = document.querySelector(button.dataset.copy);
      if (!target) return;
      const text = 'value' in target ? target.value : target.textContent;
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = 'Скопировано';
      } catch (_) {
        target.focus();
        if (typeof target.select === 'function') target.select();
      }
    });
  });
  const messageBody = document.querySelector('[data-message-body]');
  const messageCounter = document.querySelector('[data-message-counter]');
  if (messageBody && messageCounter) {
    const updateCounter = () => { messageCounter.textContent = `${messageBody.value.length} / 2000`; };
    messageBody.addEventListener('input', updateCounter);
    updateCounter();
  }
  document.querySelectorAll('[data-variant-select]').forEach((select) => {
    select.addEventListener('change', () => {
      const option = select.selectedOptions[0];
      const index = select.dataset.target;
      const expansion = document.querySelector(`[data-expansion-input="${index}"]`);
      const english = document.querySelector(`[data-english-input="${index}"]`);
      if (expansion) expansion.value = option.dataset.expansion || '';
      if (english) english.value = option.dataset.english || '';
    });
  });
  const backToTop = document.querySelector('[data-back-to-top]');
  const footer = document.querySelector('.site-footer');
  if (backToTop) {
    let scrollScheduled = false;
    const updateBackToTop = () => {
      const footerRect = footer ? footer.getBoundingClientRect() : null;
      const footerVisible = footerRect && footerRect.top < window.innerHeight && footerRect.bottom > 0;
      backToTop.classList.toggle('is-visible', window.scrollY > 320 && !footerVisible);
      scrollScheduled = false;
    };
    const scheduleBackToTopUpdate = () => {
      if (scrollScheduled) return;
      scrollScheduled = true;
      window.requestAnimationFrame(updateBackToTop);
    };
    window.addEventListener('scroll', scheduleBackToTopUpdate, { passive: true });
    window.addEventListener('resize', scheduleBackToTopUpdate);
    updateBackToTop();
  }

  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.confirm) return;
    if (form.dataset.confirmed === '1') return;
    event.preventDefault();
    if (!window.confirm(form.dataset.confirm)) return;
    form.dataset.confirmed = '1';
    const confirmation = form.querySelector('input[name="confirmed"]');
    if (confirmation) confirmation.value = '1';
    form.requestSubmit();
  });
})();
