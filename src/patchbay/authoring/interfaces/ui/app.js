'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const state = {token: '', role: '', tasks: [], selected: null, detail: null, feedback: null,
                 leases: new Map(), offset: 0, nextOffset: null, busy: false, generation: 0, file: null};
  const key = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const json = value => JSON.stringify(value, null, 2);
  function notice(text, error = false) { $('notice').textContent = text; $('notice').classList.toggle('error', error); }
  async function api(path, {method = 'GET', data, headers = {}, body} = {}) {
    const response = await fetch(path, {method, headers: {Authorization: `Bearer ${state.token}`,
      ...(data !== undefined ? {'Content-Type': 'application/json'} : {}), ...headers}, body: data !== undefined ? JSON.stringify(data) : body});
    const type = response.headers.get('content-type') || '';
    const result = type.includes('json') ? await response.json() : await response.text();
    if (!response.ok) { const error = result.error || result; throw new Error(`${error.code || response.status}: ${error.message || String(result)}${error.details ? '\n' + json(error.details) : ''}`); }
    return result;
  }
  async function download(path, filename) {
    const response = await fetch(path, {headers: {Authorization: `Bearer ${state.token}`}});
    if (!response.ok) throw new Error(`Artifact download failed (${response.status})`);
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = filename;
    document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
  async function act(action) {
    if (state.busy) return;
    state.busy = true; renderControls(); notice('Processing the requested action…');
    try { const result = await action(); if (result !== undefined) $('last-action').textContent = json(result); }
    catch (error) { notice(error.message, true); }
    finally { state.busy = false; renderControls(); }
  }
  function renderControls() {
    const writable = ['operator', 'author'].includes(state.role);
    for (const id of ['download-context', 'run-checks', 'download-feedback', 'import-package']) $(id).disabled = state.busy || !state.selected || !writable;
    $('create-panel').hidden = !writable;
    $('import-zone').hidden = !writable;
    $('publish').disabled = state.busy || state.role !== 'operator' || !state.detail?.policy?.publication?.enabled || state.feedback?.classification !== 'READY_FOR_REVIEW';
    $('previous').disabled = state.busy || !state.offset; $('next').disabled = state.busy || state.nextOffset === null;
    const operation = state.feedback?.operation;
    $('cancel').hidden = !writable || !operation || !['queued', 'running'].includes(operation.state);
    $('cancel').disabled = state.busy;
  }
  async function lease(taskId) {
    const prior = state.leases.get(taskId);
    const value = await api('/api/claim', {method: 'POST', data: {task_id: taskId, label: 'operator-browser', token: prior?.token || null, takeover: false}});
    state.leases.set(taskId, value); return {token: value.token, epoch: value.epoch};
  }
  function renderTasks() {
    $('task-list').replaceChildren();
    for (const task of state.tasks) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'task-button';
      button.classList.toggle('selected', task.id === state.selected);
      const title = document.createElement('strong'); title.textContent = task.goal.split('\n')[0].slice(0, 140);
      const detail = document.createElement('small'); detail.textContent = `Revision ${task.current_revision} · ${task.status.replaceAll('_', ' ')}`;
      button.append(title, detail); button.addEventListener('click', () => act(async () => { await select(task.id); notice('Task loaded.'); }));
      $('task-list').append(button);
    }
    if (!state.tasks.length) $('task-list').textContent = 'No tasks on this page.';
  }
  async function refreshList() {
    const result = await api(`/api/tasks?offset=${state.offset}&limit=25`);
    state.tasks = result.tasks; state.nextOffset = result.next_offset; renderTasks(); renderControls();
  }
  function renderChecks(feedback) {
    $('verification').textContent = `${feedback.classification.replaceAll('_', ' ')}. ${feedback.next_action}`;
    $('checks').replaceChildren();
    for (const check of feedback.check_results || []) {
      const section = document.createElement('div'); section.className = 'check';
      const heading = document.createElement('strong'); heading.textContent = `${check.stage} / ${check.id} — ${check.verification || check.state}`;
      const line = document.createElement('p'); line.textContent = `Process: ${check.state}; exit: ${check.exit_code ?? 'not available'}; cleanup: ${check.cleanup?.state || 'unavailable'}`;
      section.append(heading, line);
      if (check.verification !== 'passed') {
        const evidence = document.createElement('pre');
        evidence.textContent = check.failures?.length ? json(check.failures) : check.stderr?.tail || check.stdout?.tail || check.reason || 'No complete execution evidence.';
        section.append(evidence);
      }
      $('checks').append(section);
    }
    if (feedback.next_offset !== null && feedback.next_offset !== undefined) {
      const more = document.createElement('p'); more.className = 'muted'; more.textContent = `Showing the first page of ${feedback.total_check_results} check results. Download feedback for complete evidence.`; $('checks').append(more);
    }
  }
  async function select(taskId) {
    const generation = ++state.generation;
    state.selected = taskId; state.file = null; $('package').value = ''; $('diff-panel').open = false;
    const [task, feedback, publication] = await Promise.all([api(`/api/tasks/${taskId}`), api(`/api/tasks/${taskId}/feedback`), api(`/api/tasks/${taskId}/publications`)]);
    if (generation !== state.generation) return;
    state.detail = task; state.feedback = feedback;
    $('empty').hidden = true; $('task-detail').hidden = false;
    $('task-title').textContent = task.goal; $('task-id').textContent = task.id;
    $('state').textContent = task.status.replaceAll('_', ' '); $('revision').textContent = String(task.current_revision);
    $('commit').textContent = task.current_commit; $('policy').textContent = json(task.policy);
    $('publications').textContent = publication.publications.length ? json(publication.publications) : 'No publication recorded.';
    renderChecks(feedback); renderTasks(); renderControls();
  }
  $('connect-form').addEventListener('submit', event => { event.preventDefault(); act(async () => {
    state.token = $('token').value.trim(); const capabilities = await api('/api/capabilities'); state.role = capabilities.role;
    $('runtime-status').textContent = `${capabilities.role} · ${capabilities.scheduler_enabled ? 'check scheduler enabled' : 'scheduler off; explicit checks only'} · MCP writes ${capabilities.mcp_write_qualified ? 'qualified' : 'not qualified'}`;
    const repos = await api('/api/repositories'); $('repository').replaceChildren();
    for (const repo of repos.repositories) { const option = document.createElement('option'); option.value = repo.id; option.textContent = repo.name; $('repository').append(option); }
    $('token').value = ''; $('connection').hidden = true; $('workspace').hidden = false; await refreshList(); notice('Connected.');
  }); });
  $('disconnect').addEventListener('click', () => { state.token = ''; state.role = ''; state.leases.clear(); state.selected = null; state.generation++; $('workspace').hidden = true; $('connection').hidden = false; notice('Disconnected. Existing authorized checks are not cancelled.'); });
  $('refresh').addEventListener('click', () => act(async () => { await refreshList(); if (state.selected) await select(state.selected); notice('State refreshed.'); }));
  $('previous').addEventListener('click', () => act(async () => { state.offset = Math.max(0, state.offset - 25); await refreshList(); notice('Previous task page loaded.'); }));
  $('next').addEventListener('click', () => act(async () => { state.offset = state.nextOffset; await refreshList(); notice('Next task page loaded.'); }));
  $('task-form').addEventListener('submit', event => { event.preventDefault(); act(async () => {
    const task = await api('/api/tasks', {method: 'POST', data: {repository: $('repository').value, goal: $('goal').value, key: key()}});
    $('goal').value = ''; await refreshList(); await select(task.id); notice('Task created at its pinned source revision.'); return task;
  }); });
  $('download-context').addEventListener('click', () => act(async () => {
    const result = await api('/api/context', {method: 'POST', data: {task_id: state.selected, revision: state.detail.current_revision}});
    await download(result.download, `context-${state.selected}-r${result.revision}.zip`); notice('Pinned source context exported. Give the archive and its TASK.md to Pro.'); return result;
  }));
  $('run-checks').addEventListener('click', () => act(async () => {
    const id = state.selected; const authority = await lease(id);
    const result = await api('/api/checks', {method: 'POST', data: {task_id: id, revision: state.detail.current_revision, key: key(), ...authority}});
    await select(id); notice('Checks admitted. The displayed operation is authoritative; admission is not a passing result.'); return result;
  }));
  function choose(file) { if (!file || !file.name.toLowerCase().endsWith('.zip')) throw new Error('Select a completed implementation ZIP, not a partial download.'); state.file = file; notice(`Selected ${file.name}. Import explicitly to validate it for the current task.`); }
  $('package').addEventListener('change', () => { try { choose($('package').files[0]); } catch (error) { notice(error.message, true); } });
  const zone = $('import-zone');
  zone.addEventListener('dragover', event => { event.preventDefault(); zone.classList.add('drag'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag'));
  zone.addEventListener('drop', event => { event.preventDefault(); zone.classList.remove('drag'); try { choose(event.dataTransfer.files[0]); } catch (error) { notice(error.message, true); } });
  $('import-package').addEventListener('click', () => act(async () => {
    if (!state.file) throw new Error('Select an implementation ZIP first.');
    const id = state.selected; const authority = await lease(id);
    const result = await api(`/api/tasks/${id}/packages`, {method: 'POST', body: state.file,
      headers: {'Content-Type': 'application/zip', 'X-Author-Lease': authority.token, 'X-Author-Epoch': String(authority.epoch), 'Idempotency-Key': key()}});
    await refreshList(); await select(id); notice(result.duplicate ? 'The same implementation was already recorded; no duplicate candidate was created.' : 'Implementation accepted as an isolated candidate. Run approved checks or inspect the task’s automatic check operation.'); return result;
  }));
  $('download-feedback').addEventListener('click', () => act(async () => {
    const result = await api(`/api/tasks/${state.selected}/feedback`, {method: 'POST'});
    await download(result.download, `feedback-${state.selected}-r${result.revision}.zip`);
    try { await navigator.clipboard.writeText(result.continuation); notice('Feedback exported and the continuation prompt copied.'); }
    catch { notice(`Feedback exported. Continuation prompt:\n${result.continuation}`); }
    return result;
  }));
  $('cancel').addEventListener('click', () => act(async () => {
    const operation = state.feedback?.operation; if (!operation) return;
    const authority = await lease(state.selected);
    const result = await api('/api/cancel', {method: 'POST', data: {operation_id: operation.id, ...authority}});
    await select(state.selected); notice('Cancellation requested for this owned check. Cleanup proof is recorded separately.'); return result;
  }));
  $('diff-panel').addEventListener('toggle', async () => {
    if (!$('diff-panel').open || !state.selected) return;
    const id = state.selected; const revision = state.detail.current_revision; $('diff').textContent = 'Loading…';
    try { const result = await api(`/api/tasks/${id}/diff?revision=${revision}`); if (id === state.selected) $('diff').textContent = result.diff + (result.truncated ? '\n[Bounded diff is partial. Request individual paths through the API/CLI.]' : ''); }
    catch (error) { $('diff').textContent = error.message; }
  });
  $('publish').addEventListener('click', () => act(async () => {
    const id = state.selected; const revision = state.detail.current_revision;
    const preview = await api(`/api/tasks/${id}/publication-preview?revision=${revision}`);
    if (!window.confirm(`Publish this reviewed commit to ${preview.branch}?\n${preview.commit}\n\nNo merge or default-branch update is authorized.`)) { notice('Publication not authorized.'); return; }
    const authority = await lease(id);
    const approval = await api('/api/approvals', {method: 'POST', data: {task_id: id, revision, actor: 'operator-browser', key: key(), ...authority}});
    const result = await api('/api/publications', {method: 'POST', data: {approval_id: approval.approval_id, key: key(), ...authority}});
    await select(id); notice('Exact candidate publication recorded. Inspect the separate PR and CI outcomes; nothing was merged.'); return result;
  }));
  // Poll only the visible task. Reads never launch checks or scan historical workspaces.
  setInterval(async () => {
    if (!state.token || !state.selected || state.busy || document.hidden) return;
    const id = state.selected;
    try {
      const feedback = await api(`/api/tasks/${id}/feedback`);
      if (id !== state.selected) return;
      state.feedback = feedback; renderChecks(feedback); renderControls();
    } catch (error) { notice(`Status refresh failed: ${error.message}`, true); }
  }, 5000);
})();
