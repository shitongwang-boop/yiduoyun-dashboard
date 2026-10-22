/* Data refresh uses the same snapshot for every chart and requirement list. */
(() => {
  const $ = id => document.getElementById(id);
  const modal = $('dataRefreshMask');
  const button = $('refreshAllData');
  let status = null;
  let monitoring = false;
  let submitting = false;
  let appliedVersion = '';
  let previousFocus = null;
  let previousOverflow = '';
  let nextTimer;

  async function request(path, options = {}) {
    const response = await fetch(`api/${path}`, { cache: 'no-store', signal: AbortSignal.timeout(15000), ...options });
    if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('数据刷新服务暂不可用，请稍后重试');
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.message || '数据刷新服务连接失败，请稍后重试');
    return result;
  }

  function renderStatus(connectionError = '') {
    const running = status?.status === 'running';
    const failed = status?.status === 'error';
    button.disabled = running || submitting || Boolean(connectionError);
    button.classList.toggle('is-loading', running || submitting);
    button.querySelector('span').textContent = running || submitting ? '正在刷新…' : '一键刷新';
    $('refreshStatus').className = `tag ${connectionError || failed ? 'tag-danger' : running ? 'tag-info' : 'tag-success'}`;
    $('refreshStatus').textContent = connectionError ? '连接失败' : running ? '刷新中' : failed ? '刷新失败' : status ? '数据正常' : '连接中';
    $('refreshMessage').textContent = connectionError || status?.message || '正在连接数据刷新服务…';
    $('refreshUpdatedAt').textContent = status?.updatedAt || REQUIREMENT_DATA.meta.snapshotAt;
    $('refreshNextAt').textContent = status?.nextRefreshAt || '连接后显示';
    $('refreshSchedule').textContent = status?.schedule || '每小时整点自动刷新（北京时间）';
    $('refreshRecordCount').textContent = status?.sourceRecordCount == null ? '—' : `${status.sourceRecordCount} 条`;
    $('refreshRequirementCount').textContent = `${status?.requirementCount ?? Object.values(REQUIREMENT_DATA.topics).reduce((n, t) => n + t.total, 0)} 项`;
    $('refreshTrigger').textContent = status?.trigger === 'hourly' ? '自动刷新' : status?.trigger === 'manual' ? '手动刷新' : '初始快照';
    $('refreshCompletedAt').textContent = status?.completedAt || '—';
    $('btnRefresh').classList.toggle('is-loading', running);
    $('btnRefresh').setAttribute('aria-label', running ? '数据刷新，正在更新' : '数据刷新');
  }

  async function loadSnapshot() {
    const result = await request('dashboard');
    if (!result.data?.meta?.snapshotAt || !result.data.topics) throw new Error('最新数据格式异常，请稍后重试');
    REQUIREMENT_DATA.meta = result.data.meta;
    REQUIREMENT_DATA.topics = result.data.topics;
    renderAll();
    appliedVersion = `${status?.runId || ''}:${status?.updatedAt || ''}`;
  }

  async function monitor() {
    if (monitoring) return;
    clearTimeout(nextTimer);
    monitoring = true;
    try {
      const previousRun = status?.status === 'running';
      status = await request('refresh/status');
      const version = `${status.runId || ''}:${status.updatedAt || ''}`;
      if (status.status !== 'running' && version !== appliedVersion) {
        await loadSnapshot();
        if (previousRun) toast(status.status === 'success' ? '数据刷新完成，已更新看板' : status.message, status.status === 'success' ? 'success' : 'warn');
      }
      renderStatus();
    } catch (error) {
      renderStatus(error.name === 'TimeoutError' ? '连接超时，请稍后重试' : error.message);
    } finally {
      monitoring = false;
      nextTimer = setTimeout(monitor, status?.status === 'running' ? 2000 : 30000);
    }
  }

  function close() {
    modal.classList.remove('show');
    document.body.style.overflow = previousOverflow;
    previousFocus?.focus();
  }

  $('btnRefresh').addEventListener('click', () => {
    previousFocus = document.activeElement;
    previousOverflow = document.body.style.overflow;
    modal.classList.add('show');
    document.body.style.overflow = 'hidden';
    $('closeDataRefresh').focus();
    monitor();
  });
  $('closeDataRefresh').addEventListener('click', close);
  modal.addEventListener('click', event => { if (event.target === modal) close(); });
  modal.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.stopPropagation(); close(); }
    if (event.key === 'Tab') {
      const controls = Array.from(modal.querySelectorAll('button:not(:disabled), a[href]'));
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  button.addEventListener('click', async () => {
    if (submitting || status?.status === 'running') return;
    submitting = true;
    renderStatus();
    try {
      status = await request('refresh', { method: 'POST' });
      renderStatus();
    } catch (error) {
      toast(error.message, 'warn');
    } finally {
      submitting = false;
      renderStatus();
      monitor();
    }
  });
  $('retryRefreshConnection').addEventListener('click', monitor);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) monitor(); });
  renderStatus();
  monitor();
})();
