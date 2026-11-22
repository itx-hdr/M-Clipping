/* Saved timeline drafts. Source playback is labelled separately from draft edits. */
(() => {
  let project = null;
  let busy = false;
  let renderTimer = null;
  let rendered = null;
  let firstCut = null;
  let planningTimer = null;
  let viewEpoch = 0;
  const drafts = new Map();
  const pendingRequests = new Set();
  class StaleView extends Error {}
  const chosenCuts = new Map();
  const pacingBriefs = new Map();
  const selectedText = new Set();
  const dialog = document.createElement('dialog');
  dialog.className = 'editor-draft';
  dialog.style.cssText = 'box-sizing:border-box;width:min(1100px,96vw);max-width:96vw;max-height:92vh;padding:24px;overflow:auto;overflow-wrap:anywhere';
  const editorStyle = document.createElement('style');
  editorStyle.textContent = '.editor-draft .row{flex-wrap:wrap;min-width:0}.editor-draft input{box-sizing:border-box;max-width:100%;min-width:0}';
  document.head.append(editorStyle);
  dialog.setAttribute('aria-label', 'Editor draft');
  document.body.append(dialog);
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  async function request(path, body, method = 'POST') {
    const epoch = viewEpoch;
    const controller = new AbortController();
    pendingRequests.add(controller);
    const timeout = setTimeout(() => controller.abort(), 180000);
    const options = body === undefined ? {} : {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
    try {
      const response = await fetch('/api/editor' + path, {...options, signal: controller.signal});
      let result;
      try { result = await response.json(); }
      catch { throw new Error('The server returned an unreadable response. Reload the saved draft to check whether it finished. Your unsaved input is kept here.'); }
      if (epoch !== viewEpoch) throw new StaleView();
      if (!response.ok) throw new Error(typeof result?.detail === 'string' ? result.detail : 'The request failed. Reload the saved draft and try again.');
      return result;
    } catch (error) {
      if (epoch !== viewEpoch) throw new StaleView();
      if (controller.signal.aborted || error instanceof TypeError) {
        throw new Error('The connection stopped before a reply arrived. The server may still finish. Reload the saved draft before retrying; your unsaved input is kept here.');
      }
      throw error;
    } finally {
      clearTimeout(timeout); pendingRequests.delete(controller);
    }
  }
  const status = element('p', '', 'help');
  status.setAttribute('role', 'status');
  function draftStatus() {
    return Array.from(drafts.get(project?.id)?.keys() || []).some(key => key !== 'text-instruction') ?
      'Unsaved input is kept in this editor. Save each changed field before rendering.' : '';
  }
  function field(node, key) {
    const projectId = project.id;
    const saved = node.value;
    if (!drafts.has(projectId)) drafts.set(projectId, new Map());
    const values = drafts.get(projectId);
    if (values.get(key) === saved) values.delete(key);
    if (values.has(key)) node.value = values.get(key);
    node.addEventListener('input', () => {
      node.value === saved ? values.delete(key) : values.set(key, node.value);
      status.textContent = draftStatus();
    });
    node.dataset.draftKey = key;
    return node;
  }
  function stopPolling() {
    clearTimeout(renderTimer); clearTimeout(planningTimer); viewEpoch++;
    for (const controller of pendingRequests) controller.abort();
    dialog.querySelectorAll('video').forEach(video => video.pause());
  }
  async function action(fn) {
    if (busy) return;
    busy = true;
    dialog.setAttribute('aria-busy', 'true');
    const controls = Array.from(dialog.querySelectorAll('button, input')).map(node => [node, node.disabled]);
    for (const [node] of controls) if (!node.hasAttribute('data-close-editor')) node.disabled = true;
    status.textContent = 'Working…';
    try { await fn(); status.textContent = draftStatus() || 'Ready'; }
    catch (error) { if (!(error instanceof StaleView)) status.textContent = error.message; }
    finally {
      for (const [node, disabled] of controls) if (node.isConnected) node.disabled = disabled;
      busy = false; dialog.removeAttribute('aria-busy');
    }
  }
  function button(text, fn) {
    const node = element('button', text, 'btn sm');
    node.type = 'button';
    // A write already received by the server can still finish after the editor closes.
    if (text === 'Close') { node.dataset.closeEditor = ''; node.onclick = () => dialog.close(); }
    else node.onclick = () => action(fn);
    return node;
  }
  async function commit(body) {
    const result = await request('/projects/' + project.id + '/commands', {
      base_revision: project.revision, op_id: crypto.randomUUID(), ...body
    });
    Object.assign(project, result);
    for (const op of body.ops || []) if (op.type === 'item.split') drafts.get(project.id)?.delete(op.item_id + ':split');
    draw();
  }
  async function renderSequence() {
    const projectId = project.id;
    const revision = project.revision;
    const output = await request('/projects/' + projectId + '/exports', {revision});
    rendered = {...output, projectId};
    draw();
    pollRender(projectId, output.id);
  }
  function pollRender(projectId, outputId) {
    clearTimeout(renderTimer);
    const epoch = viewEpoch;
    renderTimer = setTimeout(async () => {
      if (!dialog.open || epoch !== viewEpoch || project?.id !== projectId) return;
      try {
        const output = await request('/projects/' + projectId + '/exports/' + outputId);
        if (!dialog.open || epoch !== viewEpoch || project?.id !== projectId || rendered?.id !== outputId) return;
        rendered = {...output, projectId};
        const state = dialog.querySelector('[data-render-state]');
        if (state) state.textContent = 'Revision ' + rendered.revision + ' · ' + rendered.status + ' · ' + Math.round(rendered.progress) + '%';
        if (['queued', 'running'].includes(rendered.status)) pollRender(projectId, outputId);
        else {
          if (rendered.status === 'done') {
            const video = dialog.querySelector('[data-render-video]');
            const label = dialog.querySelector('[data-preview-label]');
            const download = dialog.querySelector('[data-render-download]');
            const url = '/api/editor/projects/' + projectId + '/exports/' + outputId + '/media';
            if (video) video.src = url;
            if (label) label.textContent = 'Edited sequence · revision ' + rendered.revision +
              (rendered.revision !== project.revision ? ' · Render again to include your latest changes' : '');
            if (download) { download.href = url + '?download=1'; download.hidden = false; }
          }
          if (rendered.error) status.textContent = rendered.error;
        }
      } catch (error) { if (!(error instanceof StaleView) && epoch === viewEpoch) status.textContent = error.message; }
    }, 1500);
  }
  function draw() {
    const scroll = dialog.scrollTop;
    dialog.querySelectorAll('video').forEach(video => video.pause());
    const itemIds = new Set(project.document.items.map(item => item.id));
    for (const key of drafts.get(project.id)?.keys() || []) {
      if (key.includes(':') && !key.startsWith('new:') && !itemIds.has(key.split(':')[0])) drafts.get(project.id).delete(key);
    }
    for (const id of selectedText) if (!itemIds.has(id)) selectedText.delete(id);
    dialog.replaceChildren();
    const head = element('div', undefined, 'row');
    head.append(element('h2', project.name), button('Close', async () => dialog.close()));
    dialog.append(head, element('p', 'Timeline draft · revision ' + project.revision, 'help'));
    const video = element('video');
    video.dataset.renderVideo = '';
    video.controls = true;
    video.preload = 'metadata';
    const ready = rendered?.projectId === project.id && rendered.status === 'done';
    const sourceUrl = '/api/editor/projects/' + project.id + '/source';
    video.src = ready ? '/api/editor/projects/' + project.id + '/exports/' + rendered.id + '/media' : sourceUrl;
    video.style.cssText = 'width:100%;max-height:35vh;background:#17110e';
    video.addEventListener('error', () => { status.textContent = 'This source format cannot play in your browser. The saved draft is still available.'; });
    const previewLabel = element('p', ready ? 'Edited sequence · revision ' + rendered.revision : 'Original source preview', 'help');
    previewLabel.dataset.previewLabel = '';
    dialog.append(previewLabel, video);
    if (ready && rendered.revision !== project.revision) dialog.append(element('p', 'This preview shows an earlier revision. Render the saved changes to update it.', 'help'));
    const renderState = element('p', '', 'help');
    renderState.dataset.renderState = '';
    if (rendered?.projectId === project.id) renderState.textContent = 'Revision ' + rendered.revision + ' · ' + rendered.status + ' · ' + Math.round(rendered.progress) + '%';
    dialog.append(renderState);
    const outputTools = element('div', undefined, 'row');
    outputTools.append(button('Render saved sequence', renderSequence));
    const download = element('a', 'Download MP4', 'btn sm');
    download.dataset.renderDownload = ''; download.hidden = !ready;
    if (ready) download.href = video.src + '?download=1';
    outputTools.append(download);
    outputTools.append(button('Previous renders', async () => {
      const outputs = await request('/projects/' + project.id + '/exports');
      const panel = element('div');
      for (const output of outputs) panel.append(button('Revision ' + output.revision + ' · ' + output.status, async () => {
        rendered = {...output, projectId: project.id}; draw();
        if (['queued', 'running'].includes(output.status)) pollRender(project.id, output.id);
      }));
      dialog.append(panel);
    }));
    dialog.append(outputTools);
    drawFirstCut();
    const aiTools = element('div', undefined, 'row');
    const instruction = element('input'); instruction.placeholder = 'Describe caption or title improvements';
    instruction.maxLength = 1000; instruction.setAttribute('aria-label', 'AI text editing request');
    field(instruction, 'text-instruction');
    aiTools.append(instruction, button('Suggest text changes', async () => {
      status.textContent = 'Reviewing the selected text…';
      const body = {revision: project.revision, instruction: instruction.value || 'Correct obvious spelling and improve title clarity.'};
      if (selectedText.size) body.item_ids = Array.from(selectedText);
      const suggestions = await request('/projects/' + project.id + '/suggestions', body);
      showSuggestions(suggestions);
    }), button('Saved suggestions', async () => showSuggestions(await request('/projects/' + project.id + '/suggestions'))));
    dialog.append(aiTools, element('p', 'AI suggestions review unlocked text. Select captions to limit the request. Saved corrections are protected until you allow further AI changes.', 'help'));
    const tools = element('div', undefined, 'row');
    tools.append(button('Reload saved draft', async () => {
      project = await request('/projects/' + project.id); draw(); await loadActivity();
    }), button('Discard unsaved input', async () => {
      drafts.delete(project.id); draw();
    }), button('History', async () => {
      const history = await request('/projects/' + project.id + '/history');
      const panel = element('div');
      for (const entry of history) {
        const row = element('div', undefined, 'row');
        row.append(button('Restore revision ' + entry.revision + ' · ' + entry.label,
          () => commit({restore_revision: entry.revision})), button('Choose one item', async () => {
          const saved = await request('/projects/' + project.id + '/history/' + entry.revision);
          const choices = element('div');
          for (const item of saved.document.items) choices.append(button('Restore ' + item.kind + ' at ' +
            (item.at_frame / 30).toFixed(2) + 's' + (item.text ? ' · ' + item.text.slice(0, 40) : ''),
            () => commit({restore_revision: entry.revision, restore_item_id: item.id})));
          row.after(choices);
        }));
        panel.append(row);
      }
      dialog.append(panel);
    }), button('Saved projects', list));
    dialog.append(tools);
    for (const track of project.document.tracks) {
      const heading = element('div', undefined, 'row');
      heading.append(element('h3', track.kind), button(track.locked ? 'Unlock track' : 'Lock track',
        () => commit({ops: [{type: 'track.lock', track_id: track.id, locked: !track.locked}]})));
      if (['title', 'caption'].includes(track.kind)) heading.append(button('Add ' + track.kind, async () => {
        const form = element('div', undefined, 'row');
        const text = element('input'); text.placeholder = 'Enter text'; text.setAttribute('aria-label', 'New ' + track.kind);
        const start = element('input'); start.type = 'number'; start.min = '0'; start.step = '1'; start.value = '0';
        start.setAttribute('aria-label', 'New text start frame'); start.style.width = '90px';
        const length = element('input'); length.type = 'number'; length.min = '1'; length.step = '1'; length.value = '60';
        length.setAttribute('aria-label', 'New text duration in frames'); length.style.width = '90px';
        field(text, 'new:' + track.id + ':text'); field(start, 'new:' + track.id + ':start'); field(length, 'new:' + track.id + ':length');
        form.append(text, element('span', 'Start / duration in frames'), start, length,
          button('Add text', async () => {
            await commit({ops: [{type: 'text.add', track_id: track.id, new_id: crypto.randomUUID(),
              text: text.value, at_frame: frame(start), frames: frame(length, 1)}]});
            for (const key of ['text', 'start', 'length']) drafts.get(project.id)?.delete('new:' + track.id + ':' + key);
          }));
        heading.nextElementSibling?.matches('[data-add-text]') && heading.nextElementSibling.remove();
        form.dataset.addText = '';
        heading.after(form); text.focus();
      }));
      dialog.append(heading);
      if (track.locked) heading.querySelectorAll('button').forEach(control => {
        if (control.textContent.startsWith('Add ')) control.disabled = true;
      });
      for (const item of project.document.items.filter(i => i.track_id === track.id).sort((a,b) => a.at_frame - b.at_frame)) {
        const row = element('div', undefined, 'row');
        row.style.cssText = 'padding:10px 0;border-bottom:1px solid var(--line);flex-wrap:wrap';
        const time = element('input');
        time.type = 'number'; time.min = '0'; time.step = '1'; time.value = item.at_frame;
        time.style.width = '90px'; time.disabled = track.locked;
        time.setAttribute('aria-label', 'Start frame for ' + item.kind);
        field(time, item.id + ':at');
        row.append(element('span', (item.frames / 30).toFixed(2) + ' seconds'), time,
          button('Move to frame', () => commit({ops: [{type: 'item.move', item_id: item.id, at_frame: frame(time)}]})));
        if (item.text !== undefined) {
          const selection = element('label', undefined, 'ck');
          const checkbox = element('input'); checkbox.type = 'checkbox'; checkbox.checked = selectedText.has(item.id);
          checkbox.onchange = () => checkbox.checked ? selectedText.add(item.id) : selectedText.delete(item.id);
          selection.append(checkbox, element('span', 'Include in AI request')); row.append(selection);
          const text = element('input'); text.value = item.text; text.maxLength = 2000; text.disabled = track.locked;
          text.setAttribute('aria-label', item.kind + ' text');
          field(text, item.id + ':text');
          row.append(text, button('Save text', () => commit({ops: [{type: 'item.text', item_id: item.id, text: text.value}]})));
        } else {
          row.append(button('View source section', async () => {
            if (video.getAttribute('src') !== sourceUrl) video.src = sourceUrl;
            video.currentTime = item.source_in / 30; previewLabel.textContent = 'Original source preview';
          }));
          const split = element('input'); split.type = 'number'; split.min = '1'; split.step = '1';
          split.value = Math.floor(item.frames / 2); split.style.width = '90px';
          split.setAttribute('aria-label', 'Split offset in frames');
          field(split, item.id + ':split');
          row.append(split, button('Split section', () => commit({ops: [{type: 'item.split', item_id: item.id,
            offset_frames: frame(split, 1), new_id: crypto.randomUUID()}]})));
          const trimStart = element('input'); trimStart.type = 'number'; trimStart.min = '0'; trimStart.step = '1';
          trimStart.value = item.source_in; trimStart.style.width = '90px'; trimStart.setAttribute('aria-label', 'Source start frame');
          const trimLength = element('input'); trimLength.type = 'number'; trimLength.min = '1'; trimLength.step = '1';
          trimLength.value = item.frames; trimLength.style.width = '90px'; trimLength.setAttribute('aria-label', 'Section length in frames');
          field(trimStart, item.id + ':source'); field(trimLength, item.id + ':length');
          row.append(element('span', 'Source start / length'), trimStart, trimLength,
            button('Trim section', () => commit({ops: [{type: 'item.trim', item_id: item.id,
              source_in: frame(trimStart), frames: frame(trimLength, 1)}]})));
        }
        const protectedItem = project.document.protected.includes(item.id);
        row.append(button(protectedItem ? 'Allow AI changes' : 'Protect from AI changes', () => commit({ops: [
          {type: 'item.protect', item_id: item.id, protected: !protectedItem}]})),
          button('Remove', () => commit({ops: [{type: 'item.remove', item_id: item.id}]})));
        dialog.append(row);
        const affected = [item, ...project.document.items.filter(cue => cue.parent_id === item.id)];
        const locked = affected.some(target => project.document.tracks.some(t => t.id === target.track_id && t.locked));
        if (locked) row.querySelectorAll('button, input').forEach(control => {
          if (control.textContent !== 'View source section') control.disabled = true;
        });
      }
    }
    status.setAttribute('role', 'status');
    dialog.append(status);
    dialog.scrollTop = scroll;
  }
  function frame(input, minimum = 0) {
    const value = Number(input.value);
    if (!input.value.trim() || !Number.isSafeInteger(value) || value < minimum || value > 1000000000) {
      throw new Error('Enter a whole frame number of ' + minimum + ' or more.');
    }
    return value;
  }
  function drawFirstCut() {
    let panel = dialog.querySelector('[data-first-cut]');
    if (!panel) { panel = element('section'); panel.dataset.firstCut = ''; dialog.append(panel); }
    panel.replaceChildren(element('h3', 'First-cut pacing'));
    panel.append(element('p', 'Review repeated dialogue and verbal filler across the saved sequence. Footage without transcript evidence stays in place. Protected content is retained; its timing may shift when earlier footage is cut. Locked tracks keep their timing.', 'help'));
    const brief = element('input'); brief.maxLength = 1000;
    brief.placeholder = 'Keep the full story; remove clear repetitions';
    brief.value = pacingBriefs.get(project.id) || '';
    brief.oninput = () => pacingBriefs.set(project.id, brief.value);
    brief.setAttribute('aria-label', 'First-cut pacing brief');
    const tools = element('div', undefined, 'row');
    const start = button('Plan pacing cut', async () => {
      const projectId = project.id;
      const body = {revision: project.revision, instruction: brief.value || brief.placeholder,
        op_id: crypto.randomUUID()};
      const result = await request('/projects/' + projectId + '/first-cuts', body);
      if (!dialog.open || project?.id !== projectId) return;
      firstCut = {...result, projectId}; drawFirstCut();
      pollFirstCut(projectId, result.id);
    });
    start.disabled = firstCut?.projectId === project.id && ['queued', 'running'].includes(firstCut.status);
    brief.disabled = start.disabled;
    tools.append(brief, start, button('Saved pacing passes', async () => {
      const projectId = project.id;
      const passes = await request('/projects/' + projectId + '/first-cuts');
      if (!dialog.open || project?.id !== projectId) return;
      const history = element('div');
      if (!passes.length) history.append(element('p', 'No pacing passes yet.'));
      for (const pass of passes) history.append(button('Revision ' + pass.revision + ' · ' + pass.status, async () => {
        clearTimeout(planningTimer); firstCut = {...pass, projectId}; drawFirstCut();
        if (['queued', 'running'].includes(pass.status)) pollFirstCut(projectId, pass.id);
      }));
      panel.append(history);
    }));
    panel.append(tools, element('p', 'Uses your configured AI provider. Up to 24 five-minute review windows; sequences up to two hours. Review and apply the chosen cuts, then render the saved sequence.', 'help'));
    if (firstCut?.projectId !== project.id) return;
    const pass = firstCut;
    panel.append(element('p', 'Revision ' + pass.revision + ' · ' + pass.status + ' · ' + pass.progress + '%', 'help'));
    if (pass.error) panel.append(element('p', pass.error, 'help'));
    const current = pass.revision === project.revision;
    if (!current && pass.status !== 'applied') panel.append(element('p', 'The draft has changed. Start a new pacing pass before applying cuts.', 'help'));
    if (pass.status === 'applied') panel.append(element('p', 'Restore one cut to bring back its source picture, sound and linked captions. Later text edits and removed titles stay as you saved them. Restored content is protected from future AI cuts.', 'help'));
    const controls = element('div', undefined, 'row');
    for (const [decision, label, visible] of [
      ['cancel', 'Cancel pass', ['queued', 'running'].includes(pass.status)],
      ['retry', 'Retry unfinished windows', current && ['partial', 'failed'].includes(pass.status)],
      ['dismiss', 'Dismiss proposal', ['ready', 'partial', 'failed'].includes(pass.status)]
    ]) if (visible) controls.append(button(label, async () => {
      const result = await request('/projects/' + pass.projectId + '/first-cuts/' + pass.id + '/decision', {decision});
      if (!dialog.open || project?.id !== pass.projectId || firstCut?.id !== pass.id) return;
      firstCut = {...result, projectId: pass.projectId}; drawFirstCut();
      if (result.status === 'queued') pollFirstCut(pass.projectId, pass.id);
    }));
    panel.append(controls);
    if (['queued', 'running'].includes(pass.status)) panel.append(element('p', 'You can keep editing while this runs. Cancelling stops later windows; a provider request already in progress may finish.', 'help'));
    const cuts = pass.windows.flatMap(window => window.cuts);
    const canApply = current && ['ready', 'partial'].includes(pass.status);
    if (canApply && !chosenCuts.has(pass.id)) chosenCuts.set(pass.id, new Set(cuts.map(cut => cut.id)));
    const selection = chosenCuts.get(pass.id) || new Set();
    const summary = element('p', '', 'help');
    const apply = button('Apply chosen cuts', async () => {
      await commit({first_cut_id: pass.id, cut_ids: Array.from(selection)});
      firstCut = {...await request('/projects/' + project.id + '/first-cuts/' + pass.id), projectId: project.id};
      drawFirstCut();
    });
    function updateSelection() {
      const frames = cuts.filter(cut => selection.has(cut.id)).reduce((total, cut) => total + cut.end_frame - cut.start_frame, 0);
      summary.textContent = selection.size + ' selected · ' + (frames / 30).toFixed(2) + ' seconds removed. Apply creates one revision; History restores the previous sequence.';
      apply.disabled = !selection.size;
    }
    if (canApply && cuts.length) { updateSelection(); panel.append(summary, apply); }
    if (['ready', 'partial'].includes(pass.status) && !cuts.length) panel.append(element('p', 'No supported cuts were proposed. The saved sequence is unchanged.'));
    const coverage = element('details');
    coverage.append(element('summary', 'Review coverage and reasons'));
    for (const window of pass.windows) {
      const section = element('div');
      section.append(element('p', (window.start_frame / 30).toFixed(1) + '–' + (window.end_frame / 30).toFixed(1) + 's · ' + window.status),
        element('p', window.summary || 'Waiting for review.', 'help'));
      if (window.error) section.append(element('p', window.error, 'help'));
      coverage.append(section);
    }
    panel.append(coverage);
    for (const cut of cuts) {
      const card = element('div'); card.style.cssText = 'padding:12px 0;border-bottom:1px solid var(--line)';
      if (pass.status === 'applied') {
        const restored = (project.document.restored_cuts || []).includes(pass.id + ':' + cut.id);
        card.append(element('p', restored ? 'Restored and protected' : cut.applied ? 'Applied' : 'Kept in the sequence', 'help'));
        if (cut.applied && !restored) card.append(button('Restore this cut', () => commit({
          restore_cut: {pass_id: pass.id, cut_id: cut.id}
        })));
      }
      if (canApply) {
        const label = element('label', undefined, 'ck');
        const check = element('input'); check.type = 'checkbox'; check.checked = selection.has(cut.id);
        check.onchange = () => { check.checked ? selection.add(cut.id) : selection.delete(cut.id); updateSelection(); };
        label.append(check, element('span', 'Include this cut')); card.append(label);
      }
      card.append(element('p', 'Remove ' + (cut.start_frame / 30).toFixed(2) + '–' + (cut.end_frame / 30).toFixed(2) + 's · ' + cut.text),
        element('p', cut.reason, 'help'), button('Review source moment', async () => {
          const video = dialog.querySelector('[data-render-video]');
          const sourceUrl = '/api/editor/projects/' + project.id + '/source';
          if (video.getAttribute('src') !== sourceUrl) video.src = sourceUrl;
          video.currentTime = Math.max(0, cut.source_in / 30 - 1);
          dialog.querySelector('[data-preview-label]').textContent = 'Original source · proposed cut starts at ' + (cut.source_in / 30).toFixed(2) + 's';
        }));
      panel.append(card);
    }
  }
  function pollFirstCut(projectId, passId) {
    clearTimeout(planningTimer);
    const epoch = viewEpoch;
    planningTimer = setTimeout(async () => {
      if (!dialog.open || epoch !== viewEpoch || project?.id !== projectId || firstCut?.id !== passId) return;
      try {
        const result = await request('/projects/' + projectId + '/first-cuts/' + passId);
        if (!dialog.open || epoch !== viewEpoch || project?.id !== projectId || firstCut?.id !== passId) return;
        firstCut = {...result, projectId}; drawFirstCut();
        if (['queued', 'running'].includes(result.status)) pollFirstCut(projectId, passId);
      } catch (error) { if (!(error instanceof StaleView) && epoch === viewEpoch) status.textContent = error.message; }
    }, 1500);
  }
  async function loadFirstCut() {
    const projectId = project.id;
    const passes = await request('/projects/' + projectId + '/first-cuts');
    if (!dialog.open || project?.id !== projectId) return;
    firstCut = passes.length ? {...passes[0], projectId} : null;
    drawFirstCut();
    if (firstCut && ['queued', 'running'].includes(firstCut.status)) pollFirstCut(projectId, firstCut.id);
  }
  async function loadActivity() {
    const projectId = project.id;
    const epoch = viewEpoch;
    const outputs = await request('/projects/' + projectId + '/exports');
    if (!dialog.open || epoch !== viewEpoch || project?.id !== projectId) return;
    rendered = outputs.length ? {...outputs[0], projectId} : null;
    draw();
    if (rendered && ['queued', 'running'].includes(rendered.status)) pollRender(projectId, rendered.id);
    await loadFirstCut();
  }
  function showSuggestions(suggestions) {
    const old = dialog.querySelector('[data-suggestions]');
    if (old) old.remove();
    const panel = element('section'); panel.dataset.suggestions = '';
    panel.append(element('h3', 'AI text recommendations'));
    if (!suggestions.length) panel.append(element('p', 'No text changes were suggested.'));
    for (const suggestion of suggestions) {
      const card = element('div'); card.style.cssText = 'padding:12px 0;border-bottom:1px solid var(--line)';
      card.append(element('p', 'Current text · ' + suggestion.before_text), element('p', 'Suggested text · ' + suggestion.text),
        element('p', suggestion.reason, 'help'));
      if (suggestion.status === 'pending' && suggestion.revision === project.revision) {
        for (const decision of ['apply', 'dismiss']) card.append(button(decision === 'apply' ? 'Apply suggestion' : 'Dismiss', async () => {
          await request('/projects/' + project.id + '/suggestions/' + suggestion.id + '/decision', {decision});
          project = await request('/projects/' + project.id); draw();
          showSuggestions(await request('/projects/' + project.id + '/suggestions'));
        }));
      } else card.append(element('p', suggestion.status === 'pending' ? 'Based on an earlier revision. Request fresh suggestions.' : suggestion.status, 'help'));
      panel.append(card);
    }
    dialog.append(panel);
  }
  async function list() {
    stopPolling();
    const projects = await request('/projects');
    dialog.replaceChildren(element('h2', 'Saved editor drafts'), button('Close', async () => dialog.close()));
    for (const entry of projects) dialog.append(button(entry.name + ' · revision ' + entry.revision, async () => {
      stopPolling(); rendered = null;
      project = await request('/projects/' + entry.id); selectedText.clear(); firstCut = null; draw(); await loadActivity();
    }));
    if (!projects.length) dialog.append(element('p', 'Open a finished clip to create an editor draft.'));
    dialog.append(status);
  }
  document.addEventListener('click', event => {
    if (event.target.closest('[data-editor-list]')) {
      action(async () => {
        if (!dialog.open) dialog.showModal();
        dialog.replaceChildren(element('p', 'Loading saved projects…'), button('Close', async () => dialog.close()), status);
        await list();
      });
      return;
    }
    const trigger = event.target.closest('[data-editor-clip], [data-editor-whole]');
    if (!trigger) return;
    action(async () => {
      stopPolling(); rendered = null; firstCut = null;
      dialog.replaceChildren(element('p', 'Retaining the source and creating your draft…'), button('Close', async () => dialog.close()), status);
      if (!dialog.open) dialog.showModal();
      trigger.dataset.editorRequest ||= crypto.randomUUID();
      project = await request('/projects', {job_id: trigger.dataset.editorJob, clip_id: trigger.dataset.editorClip,
        op_id: trigger.dataset.editorRequest});
      selectedText.clear();
      draw();
      await loadActivity();
    });
  });
  dialog.addEventListener('close', stopPolling);
  window.addEventListener('beforeunload', event => {
    if (Array.from(drafts.values()).some(values => Array.from(values.keys()).some(key => key !== 'text-instruction'))) {
      event.preventDefault(); event.returnValue = '';
    }
  });
})();
