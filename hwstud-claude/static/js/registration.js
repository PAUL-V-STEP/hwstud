(() => {
  const menuLabels = document.querySelectorAll('[data-registration-countdown]');
  const studentRows = document.querySelector('#new-students');
  if (!menuLabels.length && !studentRows) return;

  function clock(seconds) {
    const minutes = Math.floor(seconds / 60);
    return `${String(minutes).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  }

  function hidden(form, name, value) {
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = name;
    input.value = value;
    form.appendChild(input);
  }

  function cell(row, text) {
    const cell = document.createElement('td');
    cell.textContent = text;
    row.appendChild(cell);
  }

  function updateStudents(items) {
    if (!studentRows) return;
    studentRows.replaceChildren();
    if (!items.length) {
      const row = document.createElement('tr');
      const empty = document.createElement('td');
      empty.colSpan = 4;
      empty.textContent = 'Новых ников нет.';
      row.appendChild(empty);
      studentRows.appendChild(row);
      return;
    }
    items.forEach((item) => {
      const row = document.createElement('tr');
      cell(row, new Date(item.created_at).toLocaleString('ru-RU'));
      cell(row, item.nick);
      cell(row, item.group_name);
      const action = document.createElement('td');
      const form = document.createElement('form');
      form.method = 'post';
      form.action = '/admin/registration/delete';
      form.dataset.confirm = `Удалить ник ${item.nick}?`;
      hidden(form, 'csrf', studentRows.dataset.csrf);
      hidden(form, 'student_id', item.id);
      hidden(form, 'confirmed', '0');
      const button = document.createElement('button');
      button.type = 'submit';
      button.className = 'danger-link';
      button.textContent = 'Удалить';
      form.appendChild(button);
      action.appendChild(form);
      row.appendChild(action);
      studentRows.appendChild(row);
    });
  }

  async function refresh() {
    try {
      const response = await fetch('/admin/registration/status', {cache: 'no-store'});
      if (!response.ok) return;
      const data = await response.json();
      const seconds = data.windows.reduce((maximum, item) => Math.max(maximum, item.seconds_left), 0);
      menuLabels.forEach((label) => {
        label.hidden = seconds <= 0;
        label.textContent = seconds > 0 ? ` · ${clock(seconds)}` : '';
      });
      document.querySelectorAll('[data-registration-group]').forEach((group) => {
        const groupId = Number(group.dataset.registrationGroup);
        const window = data.windows.find((item) => item.group_id === groupId);
        const label = group.querySelector('[data-group-countdown]');
        if (label) label.textContent = window ? `Осталось: ${clock(window.seconds_left)}` : 'Закрыто';
      });
      updateStudents(data.new_students);
    } catch (_) {
      // Текущее серверное содержимое остаётся доступным при временной ошибке сети.
    }
  }

  refresh();
  window.setInterval(refresh, 5000);
})();
