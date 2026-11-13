(() => {
  'use strict';
  const csrf = () => document.querySelector('[name="csrf_token"]')?.value || '';
  const create = (name, attrs = {}, value) => {
    const node = document.createElement(name);
    Object.entries(attrs).forEach(([key, val]) => node.setAttribute(key, val));
    if (value !== undefined) node.textContent = value;
    return node;
  };
  const readError = async response => {
    const contentType = response.headers.get('Content-Type') || '';
    if (contentType.includes('application/json')) return (await response.json()).error || 'The request could not finish.';
    return response.status === 400 ? 'This page expired. Reload it and try again.' : 'The request could not finish. Try again.';
  };
  document.querySelectorAll('[data-course-plan-editor]').forEach(form => {
    const list = form.querySelector('[data-goal-editor-list]');
    const hidden = form.querySelector('[data-goals-json]');
    const add = (goal = {}) => {
      if (list.children.length >= 100) return;
      const id = goal.id || Array.from(crypto.getRandomValues(new Uint8Array(12)), n => n.toString(16).padStart(2, '0')).join('');
      const row = create('div', { class:'goal-editor-row', 'data-goal-editor-id':id });
      const titleLabel = create('label', {}, 'Goal');
      const title = create('input', { type:'text', maxlength:'300', required:'', 'data-goal-title':'' });
      title.value = goal.title || '';
      titleLabel.append(title);
      const dayLabel = create('label', {}, 'Date');
      const day = create('input', { type:'date', min:'1900-01-01', max:'2199-12-31', required:'', 'data-goal-day':'' });
      day.value = goal.due_at || '';
      dayLabel.append(day);
      const actions = create('div', { class:'goal-editor-actions' });
      const doneLabel = create('label');
      const done = create('input', { type:'checkbox', 'data-goal-completed':'' });
      done.checked = Boolean(goal.completed);
      doneLabel.append(done, document.createTextNode('Achieved'));
      const remove = create('button', { class:'text-link', type:'button' }, 'Remove goal');
      remove.addEventListener('click', () => row.remove());
      actions.append(doneLabel, remove);
      row.append(titleLabel, dayLabel, actions); list.append(row);
    };
    try { JSON.parse(hidden.value).forEach(add); } catch { /* Server also validates every goal. */ }
    form.querySelector('[data-goal-add]').addEventListener('click', () => add());
    form.addEventListener('submit', event => {
      try {
        hidden.value = JSON.stringify(Array.from(list.children, row => ({
          id:row.dataset.goalEditorId, title:row.querySelector('[data-goal-title]').value,
          due_at:row.querySelector('[data-goal-day]').value,
          completed:row.querySelector('[data-goal-completed]').checked,
        })));
      } catch (error) {
        event.preventDefault();
        const target = form.querySelector('[data-plan-error]'); target.textContent = error.message; target.hidden = false;
      }
    });
  });
  const deleteDialog = document.querySelector('[data-plan-delete-dialog]');
  if (deleteDialog) {
    document.querySelector('[data-plan-delete-open]').addEventListener('click', () => deleteDialog.showModal());
    deleteDialog.querySelector('[data-plan-delete-cancel]').addEventListener('click', () => deleteDialog.close());
  }
  document.querySelectorAll('[data-course-timeline]').forEach(list => {
    const current = list.querySelector('[data-current-goal]');
    if (current) list.scrollTop = Math.max(0, current.offsetTop - list.offsetTop - 35);
  });
  document.querySelectorAll('[data-goal-toggle]').forEach(form => {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const button = form.querySelector('button'); button.disabled = true;
      let errorMessage = form.querySelector('[data-goal-error]');
      if (!errorMessage) { errorMessage = create('small', { 'data-goal-error':'', role:'status' }); form.append(errorMessage); }
      errorMessage.textContent = '';
      try {
        const response = await fetch(form.action, { method:'POST', body:new FormData(form), headers:{ Accept:'application/json' } });
        if (!response.ok) throw new Error(await readError(response));
        const result = await response.json();
        result.timeline.forEach(goal => {
          document.querySelectorAll(`[data-goal-id="${goal.id}"]`).forEach(row => {
            row.className = `timeline-goal timeline-${goal.state}`;
            row.removeAttribute('data-current-goal'); row.removeAttribute('aria-current');
            if (goal.state === 'current') { row.dataset.currentGoal = ''; row.setAttribute('aria-current', 'step'); }
            const check = row.querySelector('.goal-check'); check.textContent = goal.completed ? '✓' : '○'; check.setAttribute('aria-checked', String(goal.completed)); check.setAttribute('aria-label', `Mark ${row.querySelector('strong').textContent} ${goal.completed ? 'not achieved' : 'achieved'}`);
            const field = row.querySelector('[name="completed"]'); if (field) field.value = goal.completed ? 'no' : 'yes';
            const badge = row.querySelector('.pill'); if (badge) badge.remove();
            if (goal.state === 'current') row.querySelector('strong').after(create('span', { class:'pill' }, 'Current agenda'));
          });
          const editable = document.querySelector(`[data-goal-editor-id="${goal.id}"] [data-goal-completed]`);
          if (editable) editable.checked = goal.completed;
        });
        document.querySelectorAll('input[name="revision"]').forEach(field => {
          if (Number(field.value) === result.revision - 1) field.value = String(result.revision);
        });
      } catch (error) { errorMessage.textContent = error.message; }
      finally { button.disabled = false; }
    });
  });
  const dialog = document.querySelector('[data-schema-dialog]');
  if (dialog) {
    const code = dialog.querySelector('[data-schema-code]');
    const source = code.textContent;
    const tokens = /("(?:\\.|[^"\\])*"\s*:)|("(?:\\.|[^"\\])*")|\b(true|false|null)\b|\b(\d+)\b/g;
    let at = 0;
    code.textContent = '';
    for (const match of source.matchAll(tokens)) {
      code.append(document.createTextNode(source.slice(at, match.index)));
      code.append(create('span', { class:match[1] ? 'schema-key' : match[2] ? 'schema-string' : match[3] ? 'schema-boolean' : 'schema-number' }, match[0]));
      at = match.index + match[0].length;
    }
    code.append(document.createTextNode(source.slice(at)));
    document.querySelector('[data-schema-open]')?.addEventListener('click', () => dialog.showModal());
    dialog.querySelector('[data-schema-close]').addEventListener('click', () => dialog.close());
    dialog.querySelector('[data-schema-copy]').addEventListener('click', async () => {
      const status = dialog.querySelector('[data-copy-status]');
      try { await navigator.clipboard.writeText(source); status.textContent = ' Copied.'; }
      catch { const range = document.createRange(); range.selectNodeContents(code); const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range); status.textContent = ' Select and copy the highlighted schema.'; }
    });
  }
  document.querySelectorAll('[data-ai-generate]').forEach(button => {
    const kind = button.dataset.aiKind;
    const scope = kind === 'feedback' ? button.closest('.feedback-ai') : button.closest('form');
    const form = button.closest('form');
    const progress = scope.querySelector('[data-ai-progress]');
    const error = scope.querySelector('[data-ai-error]');
    const draft = scope.querySelector('[data-ai-draft]');
    let generated = null;
    button.addEventListener('click', async () => {
      error.hidden = true; draft.hidden = true; progress.hidden = false; button.disabled = true;
      try {
        const response = await fetch(button.dataset.aiGenerate, { method:'POST', headers:{ 'Content-Type':'application/json', 'X-CSRFToken':csrf() }, body:JSON.stringify({ status:form.querySelector('[name="status"]')?.value }) });
        if (!response.ok) throw new Error(await readError(response));
        generated = await response.json();
        scope.querySelector('[data-ai-draft-text]').textContent = kind === 'plan' ? generated.lesson_plan : `For the parent\n\n${generated.parent_feedback}\n\nFor the student\n\n${generated.student_feedback}`;
        draft.hidden = false;
      } catch (failure) { error.textContent = failure.message; error.hidden = false; }
      finally { progress.hidden = true; button.disabled = false; }
    });
    scope.querySelector('[data-ai-apply]').addEventListener('click', () => {
      if (!generated) return;
      const keys = kind === 'plan' ? ['lesson_plan'] : ['parent_feedback', 'student_feedback'];
      keys.forEach(key => {
        const field = form.querySelector(`[name="${key}"]`);
        if (field) { field.value = generated[key]; field.dispatchEvent(new Event('input', { bubbles:true })); }
      });
      draft.hidden = true;
      form.querySelector(`[name="${keys[0]}"]`)?.focus();
    });
  });
})();
