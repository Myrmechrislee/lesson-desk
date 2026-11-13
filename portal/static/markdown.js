// Textareas stay usable without JavaScript; every preview uses the saved-text renderer.
(() => {
  const token = document.querySelector('meta[name="csrf-token"]')?.content;
  for (const field of document.querySelectorAll('textarea[data-markdown-editor]')) {
    const editor = document.createElement('div');
    editor.className = 'markdown-editor';
    const toolbar = document.createElement('div');
    toolbar.className = 'markdown-toolbar';
    toolbar.setAttribute('role', 'toolbar');
    toolbar.setAttribute('aria-label', 'Markdown formatting');
    const insert = (before, after = '', sample = 'text') => {
      const start = field.selectionStart, end = field.selectionEnd;
      const selected = field.value.slice(start, end) || sample;
      const replacement = before + selected + after;
      if (field.maxLength > 0 && field.value.length - (end - start) + replacement.length > field.maxLength) return;
      field.setRangeText(replacement, start, end, 'select');
      field.focus();
      field.dispatchEvent(new Event('input', { bubbles: true }));
    };
    const tools = [['Bold', '**', '**'], ['Italic', '*', '*'], ['Heading', '## ', ''],
      ['List', '- ', ''], ['Quote', '> ', ''], ['Code', '`', '`'], ['Link', '[', '](https://example.com)']];
    for (const [label, before, after] of tools) {
      const button = document.createElement('button');
      button.type = 'button'; button.textContent = label; button.className = 'button secondary compact';
      button.addEventListener('click', () => insert(before, after));
      toolbar.append(button);
    }
    const toggle = document.createElement('button');
    toggle.type = 'button'; toggle.textContent = 'Preview'; toggle.className = 'button secondary compact';
    toggle.setAttribute('aria-pressed', 'false');
    toolbar.append(toggle);
    const preview = document.createElement('div');
    preview.className = 'markdown-body markdown-preview'; preview.hidden = true;
    preview.setAttribute('aria-live', 'polite');
    field.before(editor); editor.append(toolbar, field, preview);
    let controller;
    toggle.addEventListener('click', async () => {
      if (!preview.hidden) {
        controller?.abort(); preview.hidden = true; toggle.textContent = 'Preview'; toggle.setAttribute('aria-pressed', 'false'); return;
      }
      preview.hidden = false; preview.textContent = 'Rendering preview…';
      toggle.textContent = 'Close preview'; toggle.setAttribute('aria-pressed', 'true');
      controller = new AbortController();
      try {
        const response = await fetch('/markdown/preview', { method: 'POST',
          headers: { 'X-CSRFToken': token, Accept: 'application/json' },
          body: new URLSearchParams({ text: field.value }), signal: controller.signal });
        if (!response.ok || response.redirected) throw new Error('Preview unavailable');
        const result = await response.json();
        preview.innerHTML = result.html || '<p>No text yet.</p>';
      } catch (error) {
        if (error.name !== 'AbortError') preview.textContent = 'Preview could not load. Your text can still be saved.';
      }
    });
    field.addEventListener('input', () => { controller?.abort(); preview.hidden = true; toggle.textContent = 'Preview'; toggle.setAttribute('aria-pressed', 'false'); });
  }
})();
