(() => {
  document.querySelectorAll('[data-file-input]').forEach((input) => {
    input.addEventListener('change', () => {
      const form = input.closest('form');
      const label = form.querySelector('[data-file-name]');
      if (input.files.length) label.textContent = `${input.files[0].name} · ${input.files[0].size} байт`;
    });
  });
  document.querySelectorAll('[data-upload-form]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!window.XMLHttpRequest || !window.FormData) return;
      event.preventDefault();
      const progress = form.querySelector('[data-upload-progress]');
      const error = form.querySelector('[data-upload-error]');
      const button = form.querySelector('[data-upload-submit]');
      const request = new XMLHttpRequest();
      progress.hidden = false;
      progress.value = 0;
      error.hidden = true;
      button.disabled = true;
      request.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) progress.value = Math.round((e.loaded / e.total) * 100);
      });
      request.addEventListener('load', () => {
        window.location.assign('/assignments');
      });
      request.addEventListener('error', () => {
        error.textContent = 'Не удалось отправить файл. Повторите попытку.';
        error.hidden = false;
        button.disabled = false;
      });
      request.open('POST', form.action);
      request.send(new FormData(form));
    });
  });
})();
