(() => {
  const viewer = document.querySelector('[data-slide-viewer]');
  const images = document.querySelectorAll('[data-slide-image], .slide-strip img, .conspectus img');
  images.forEach((image) => image.addEventListener('contextmenu', (event) => event.preventDefault()));
  if (!viewer) return;
  const current = Number(viewer.dataset.current);
  const total = Number(viewer.dataset.total);
  const base = viewer.dataset.base;
  function go(number) {
    if (number >= 1 && number <= total && number !== current) {
      window.location.assign(`${base}/${number}`);
    }
  }
  document.addEventListener('keydown', (event) => {
    if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
    if (event.key === 'ArrowLeft') go(current - 1);
    if (event.key === 'ArrowRight') go(current + 1);
  });
  let touchStart = null;
  viewer.addEventListener('touchstart', (event) => {
    touchStart = event.changedTouches[0].clientX;
  }, { passive: true });
  viewer.addEventListener('touchend', (event) => {
    if (touchStart === null) return;
    const delta = event.changedTouches[0].clientX - touchStart;
    if (Math.abs(delta) > 50) go(current + (delta < 0 ? 1 : -1));
    touchStart = null;
  }, { passive: true });
})();
