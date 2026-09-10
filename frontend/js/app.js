/* ============================================================
   SIGNAL — Performance Prediction Engine
   Frontend pipeline orchestration + rendering.
   ============================================================ */

(() => {
  'use strict';

  /* ---------------------------------------------------------- */
  /* State                                                        */
  /* ---------------------------------------------------------- */

  const state = {
    projectName: null,
    githubUrl: null,
    branch: null,
    path: null,
    detectionResult: null,
    locationResult: null,
    validation: null,
    imageName: 'performance-image',
    containerPort: null,
    containerId: null,
    hostPort: null,
    host: null,
    routesResult: null,
    locustResult: null,
    fullResult: null,
    charts: {},
  };

  const PIPELINE_STAGES = ['clone', 'detect', 'entry', 'validate', 'build', 'run', 'health', 'routes', 'generate', 'loadtest'];
  const LOAD_LEVEL_USERS = [20, 50, 100, 200, 300, 500];

  /* ---------------------------------------------------------- */
  /* Small utilities                                              */
  /* ---------------------------------------------------------- */

  const qs = (sel, root = document) => root.querySelector(sel);
  const qsa = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function fmt(value, decimals = 2, fallback = '—') {
    if (value === null || value === undefined || value === '' || Number.isNaN(value)) return fallback;
    const num = Number(value);
    if (!Number.isFinite(num)) return fallback;
    return num.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  }

  function fmtInt(value, fallback = '—') {
    if (value === null || value === undefined || Number.isNaN(value)) return fallback;
    const num = Number(value);
    if (!Number.isFinite(num)) return fallback;
    return Math.round(num).toLocaleString();
  }

  function fmtPercent(value, decimals = 1, fallback = '—') {
    if (value === null || value === undefined || Number.isNaN(value)) return fallback;
    const num = Number(value);
    if (!Number.isFinite(num)) return fallback;
    return `${num.toFixed(decimals)}%`;
  }

  function fmtBool(value, fallback = '—') {
    if (value === null || value === undefined) return fallback;
    return value ? 'Yes' : 'No';
  }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function setBadge(id, text, variant) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = `badge badge--${variant}`;
  }

  function animateNumber(id, target, decimals = 0, suffix = '') {
    const el = document.getElementById(id);
    if (!el) return;
    const targetNum = Number(target);
    if (!Number.isFinite(targetNum)) { el.textContent = '—'; return; }
    const start = 0;
    const duration = 700;
    const startTime = performance.now();
    function tick(now) {
      const progress = Math.min(1, (now - startTime) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      const value = start + (targetNum - start) * eased;
      el.textContent = decimals > 0 ? value.toFixed(decimals) + suffix : Math.round(value).toLocaleString() + suffix;
      if (progress < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  async function api(path, { method = 'GET', body = null } = {}) {
    const opts = { method, headers: {} };
    if (body !== null) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, opts);
    } catch (networkErr) {
      throw new ApiError(`Network error calling ${path}: ${networkErr.message}`, 0, null);
    }
    let data = null;
    try { data = await response.json(); } catch (_) { /* no body */ }
    if (!response.ok) {
      const message = (data && (data.message || data.error)) || `Request to ${path} failed (${response.status}).`;
      throw new ApiError(message, response.status, data);
    }
    return data;
  }

  class ApiError extends Error {
    constructor(message, status, data) {
      super(message);
      this.status = status;
      this.data = data;
    }
  }

  /* ---------------------------------------------------------- */
  /* Toasts                                                        */
  /* ---------------------------------------------------------- */

  function toast(type, title, message, duration = 4200) {
    const container = qs('#toastContainer');
    if (!container) return;
    const el = document.createElement('div');
    el.className = `toast toast--${type}`;
    el.innerHTML = `
      <span class="toast__title">${escapeHtml(title)}</span>
      ${message ? `<span class="toast__message">${escapeHtml(message)}</span>` : ''}
      <span class="toast__bar" style="animation-duration:${duration}ms"></span>
    `;
    container.appendChild(el);
    const remove = () => {
      el.classList.add('is-leaving');
      setTimeout(() => el.remove(), 180);
    };
    const timer = setTimeout(remove, duration);
    el.addEventListener('click', () => { clearTimeout(timer); remove(); });
  }

  function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* ---------------------------------------------------------- */
  /* Run status + pipeline track                                  */
  /* ---------------------------------------------------------- */

  function setRunStatus(stateName, text) {
    const el = qs('#runStatus');
    if (!el) return;
    el.dataset.state = stateName;
    setText('runStatusText', text);
  }

  function setStage(stage, status) {
    const node = qs(`.ptrack__node[data-stage="${stage}"]`);
    if (node) node.dataset.status = status;
  }

  function resetPipeline() {
    PIPELINE_STAGES.forEach((s) => setStage(s, 'pending'));
  }

  function logLine(message, kind = '') {
    const log = qs('#overviewLog');
    if (!log) return;
    log.hidden = false;
    const line = document.createElement('div');
    line.className = `run-log__line ${kind ? `run-log__line--${kind}` : ''}`;
    const time = new Date().toLocaleTimeString(undefined, { hour12: false });
    line.textContent = `[${time}] ${message}`;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
  }

  /* ---------------------------------------------------------- */
  /* Sidebar navigation                                            */
  /* ---------------------------------------------------------- */

  const SECTION_META = {
    overview: ['Overview', "Deploy, load-test, and predict — from one small experiment."],
    repository: ['Repository', 'Workspace state for the current and previously analyzed repositories.'],
    deployment: ['Deployment', 'Container build, start, and readiness verification.'],
    routes: ['Routes', 'Endpoints discovered on the running container.'],
    'load-testing': ['Load Testing', 'Executes the generated workload across increasing concurrency levels.'],
    runtime: ['Runtime Metrics', 'Container-level resource pressure observed during the load test.'],
    usl: ['Scalability (USL)', 'Fits contention and coherency to the measured throughput curve.'],
    'littles-law': ["Little's Law", 'L = λ × W'],
    queueing: ['Queueing Theory', 'M/M/1 utilization and saturation.'],
    bottleneck: ['Bottleneck Analysis', 'Which resource is limiting the system.'],
    capacity: ['Capacity Planning', 'Safe operating capacity, combined from every model.'],
    scalability: ['Future Load Prediction', 'Projected system health beyond tested load.'],
    recommendations: ['Recommendations', 'What the evidence implies you should do.'],
    playground: ['Model Playground', 'Test each mathematical model independently.'],
    report: ['Report', 'Export this run.'],
  };

  function initNav() {
    qsa('.rail__link').forEach((link) => {
      link.addEventListener('click', (e) => {
        e.preventDefault();
        const target = link.dataset.nav;
        showSection(target);
        closeMobileNav();
      });
    });

    window.addEventListener('hashchange', () => {
      const id = location.hash.replace('#', '') || 'overview';
      showSection(id, false);
    });

    const initial = location.hash.replace('#', '') || 'overview';
    showSection(initial, false);
  }

  function showSection(id, pushHash = true) {
    if (!SECTION_META[id]) return;
    qsa('.view').forEach((v) => v.classList.toggle('is-active', v.dataset.view === id));
    qsa('.rail__link').forEach((l) => l.classList.toggle('is-active', l.dataset.nav === id));
    const [title, sub] = SECTION_META[id];
    setText('topbarHeading', title);
    setText('topbarSubheading', sub);
    if (pushHash) history.replaceState(null, '', `#${id}`);
    qs('.content')?.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function closeMobileNav() {
    qs('#shell')?.classList.remove('is-nav-open');
    qs('#menuToggle')?.setAttribute('aria-expanded', 'false');
  }

  function initMobileNav() {
    const shell = qs('#shell');
    const toggle = qs('#menuToggle');
    if (!toggle || !shell) return;
    toggle.addEventListener('click', () => {
      const open = shell.classList.toggle('is-nav-open');
      toggle.setAttribute('aria-expanded', String(open));
    });
    shell.addEventListener('click', (e) => {
      if (shell.classList.contains('is-nav-open') && !qs('.rail').contains(e.target) && e.target !== toggle && !toggle.contains(e.target)) {
        closeMobileNav();
      }
    });
  }

  /* ---------------------------------------------------------- */
  /* Theme                                                         */
  /* ---------------------------------------------------------- */

  function initTheme() {
    const saved = localStorage.getItem('signal-theme');
    const initial = saved || 'dark';
    document.documentElement.setAttribute('data-theme', initial);
    setText('themeToggleLabel', initial === 'dark' ? 'Dark' : 'Light');

    qs('#themeToggle')?.addEventListener('click', () => {
      const current = document.documentElement.getAttribute('data-theme');
      const next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      localStorage.setItem('signal-theme', next);
      setText('themeToggleLabel', next === 'dark' ? 'Dark' : 'Light');
      Object.values(state.charts).forEach((chart) => chart && chart.update());
    });
  }

  /* ---------------------------------------------------------- */
  /* Chart helpers                                                 */
  /* ---------------------------------------------------------- */

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function baseChartOptions(overrides = {}) {
    const ink = cssVar('--ink-muted');
    const line = cssVar('--line');
    return Object.assign({
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 600, easing: 'easeOutCubic' },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: cssVar('--bg-panel-raised'),
          titleColor: cssVar('--ink'),
          bodyColor: cssVar('--ink-muted'),
          borderColor: cssVar('--line-strong'),
          borderWidth: 1,
          padding: 10,
          titleFont: { family: 'JetBrains Mono', size: 11 },
          bodyFont: { family: 'JetBrains Mono', size: 11 },
        },
      },
      scales: {
        x: { grid: { color: line, drawTicks: false }, ticks: { color: ink, font: { family: 'JetBrains Mono', size: 10.5 } } },
        y: { grid: { color: line, drawTicks: false }, ticks: { color: ink, font: { family: 'JetBrains Mono', size: 10.5 } }, beginAtZero: true },
      },
    }, overrides);
  }

  function makeOrUpdateChart(key, canvasId, config) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || typeof Chart === 'undefined') return null;
    if (state.charts[key]) {
      state.charts[key].data = config.data;
      state.charts[key].options = config.options;
      state.charts[key].update();
      return state.charts[key];
    }
    state.charts[key] = new Chart(canvas.getContext('2d'), config);
    return state.charts[key];
  }

  function measuredDataset(label, data) {
    return {
      label, data,
      borderColor: cssVar('--amber'),
      backgroundColor: 'transparent',
      pointBackgroundColor: cssVar('--amber'),
      pointRadius: 4,
      pointHoverRadius: 6,
      borderWidth: 2.5,
      tension: 0.3,
      spanGaps: false,
    };
  }

  function predictedDataset(label, data) {
    return {
      label, data,
      borderColor: cssVar('--cyan'),
      backgroundColor: 'transparent',
      pointBackgroundColor: cssVar('--cyan'),
      pointStyle: 'circle',
      pointRadius: 4,
      pointHoverRadius: 6,
      pointBorderWidth: 2,
      borderWidth: 2.5,
      borderDash: [6, 4],
      tension: 0.3,
      spanGaps: false,
    };
  }

  /* ============================================================
     PIPELINE ORCHESTRATION
     ============================================================ */

  function initStartForm() {
    qs('#repoForm')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const url = qs('#githubUrl').value.trim();
      const branch = qs('#branchInput').value.trim();
      if (!url) return;
      await runPipeline(url, branch || null);
    });
  }

  async function runPipeline(githubUrl, branch) {
    const btn = qs('#analyzeBtn');
    btn.classList.add('is-loading');
    btn.disabled = true;
    resetPipeline();
    resetSectionsForNewRun();
    setRunStatus('running', 'Running');
    logLine(`Starting analysis for ${githubUrl}`);

    try {
      // 1. Clone
      setStage('clone', 'active');
      showSection('repository', false);
      let cloneResult;
      try {
        cloneResult = await api('/api/github/clone', { method: 'POST', body: { github_url: githubUrl, branch } });
      } catch (err) {
        if (err.status === 409) {
          const choice = await promptExistingRepoModal();
          if (choice === 'continue') {
            state.projectName = err.data.project_name;
            state.path = err.data.path;
            state.githubUrl = githubUrl;
            state.branch = branch;
            logLine(`Continuing with existing clone of ${state.projectName}.`, 'success');
          } else if (choice === 'delete') {
            logLine(`Deleting existing clone of ${err.data.project_name}…`);
            await api('/api/github/delete', { method: 'POST', body: { project_name: err.data.project_name } });
            cloneResult = await api('/api/github/clone', { method: 'POST', body: { github_url: githubUrl, branch } });
            state.projectName = cloneResult.project_name;
            state.path = cloneResult.path;
            state.githubUrl = githubUrl;
            state.branch = cloneResult.branch;
          } else {
            throw new ApiError('Cancelled by user.', 0, null);
          }
        } else {
          throw err;
        }
      }
      if (cloneResult && cloneResult.success) {
        state.projectName = cloneResult.project_name;
        state.path = cloneResult.path;
        state.githubUrl = githubUrl;
        state.branch = cloneResult.branch;
      }
      renderRepoDetails();
      setStage('clone', 'done');
      logLine(`Cloned as project "${state.projectName}".`, 'success');

      // 2. Detect framework
      setStage('detect', 'active');
      const fwRes = await api('/api/framework/detect', { method: 'POST', body: { project_name: state.projectName } });
      state.detectionResult = fwRes.framework;
      renderFramework(fwRes.framework);
      setStage('detect', 'done');
      logLine(`Detected framework: ${fwRes.framework.detected_framework || 'unknown'} (confidence: ${fwRes.framework.confidence || 'n/a'}).`);

      // 3. Entry point
      setStage('entry', 'active');
      const epRes = await api('/api/entry-point', { method: 'POST', body: { project_name: state.projectName, detection_result: state.detectionResult } });
      state.locationResult = epRes.entry_point;
      setText('fw-entry-point', epRes.entry_point.entry_point_file || epRes.entry_point.run_command_hint || '—');
      setStage('entry', 'done');
      logLine(`Entry point resolved: ${epRes.entry_point.entry_point_file || epRes.entry_point.run_command_hint || 'unresolved'}.`);

      // 4. Validate
      setStage('validate', 'active');
      const valRes = await api('/api/validate', { method: 'POST', body: { project_name: state.projectName, detection_result: state.detectionResult, location_result: state.locationResult } });
      state.validation = valRes;
      renderValidation(valRes);
      if (!valRes.valid) {
        setStage('validate', 'error');
        throw new ApiError('Repository failed validation — see the Repository section for details.', 0, valRes);
      }
      setStage('validate', 'done');
      logLine(`Validation passed with ${valRes.warnings.length} warning(s).`, 'success');

      // 5. Docker build
      setStage('build', 'active');
      showSection('deployment', false);
      const buildRes = await api('/api/docker/build', { method: 'POST', body: { project_name: state.projectName, image_name: state.imageName, detection_result: state.detectionResult, location_result: state.locationResult } });
      if (!buildRes.success) { setStage('build', 'error'); renderBuild(buildRes, false); throw new ApiError(buildRes.message || 'Docker build failed.', 0, buildRes); }
      state.containerPort = buildRes.container_port;
      renderBuild(buildRes, true);
      setStage('build', 'done');
      logLine(`Docker image built: ${buildRes.image_name} (container port ${buildRes.container_port}).`, 'success');

      // 6. Docker run
      setStage('run', 'active');
      const runRes = await api('/api/docker/run', { method: 'POST', body: { image_name: state.imageName, container_name: 'performance-container', container_port: state.containerPort } });
      if (!runRes.success) { setStage('run', 'error'); renderRun(runRes, false); throw new ApiError(runRes.message || 'Container failed to start.', 0, runRes); }
      state.containerId = runRes.container_id;
      state.hostPort = runRes.port;
      state.host = runRes.host;
      renderRun(runRes, true);
      setStage('run', 'done');
      logLine(`Container started: ${runRes.container_id.slice(0, 12)} on port ${runRes.port}.`, 'success');

      // 7. Health check
      setStage('health', 'active');
      await runHealthCheck();

      // 8. Route discovery
      setStage('routes', 'active');
      showSection('routes', false);
      const routesRes = await api('/api/routes', { method: 'POST', body: { project_name: state.projectName, detection_result: state.detectionResult, location_result: state.locationResult } });
      state.routesResult = routesRes.routes;
      renderRoutes(routesRes.routes);
      setStage('routes', 'done');
      logLine(`Discovered ${routesRes.count} route(s).`, 'success');

      // 9. Locust generate
      setStage('generate', 'active');
      const locustRes = await api('/api/locust/generate', { method: 'POST', body: { project_name: state.projectName, route_discovery_result: state.routesResult } });
      state.locustResult = locustRes.locust;
      renderLocust(locustRes.locust);
      setStage('generate', 'done');
      logLine(`Locust configuration generated (${locustRes.locust.task_count} task(s)).`, 'success');

      // 10. Load testing (the big one)
      setStage('loadtest', 'active');
      showSection('load-testing', false);
      initLoadLevelChips();
      const loadRes = await api('/api/load-testing/run', { method: 'POST', body: { project_name: state.projectName, container_id: state.containerId, port: state.hostPort } });
      state.fullResult = loadRes;
      finishLoadLevelChips();
      renderFullResult(loadRes);
      setStage('loadtest', 'done');
      logLine('Load test complete — full mathematical analysis ready.', 'success');

      setRunStatus('success', 'Complete');
      toast('success', 'Run complete', 'Full pipeline finished — explore Prediction sections for results.');
      qs('#exportReportBtn').disabled = false;

    } catch (err) {
      console.error(err);
      setRunStatus('error', 'Failed');
      logLine(err.message || 'Pipeline failed.', 'error');
      toast('error', 'Pipeline stopped', err.message || 'Something went wrong.');
    } finally {
      btn.classList.remove('is-loading');
      btn.disabled = false;
    }
  }

  function resetSectionsForNewRun() {
    qs('#exportReportBtn').disabled = true;
    setBadge('repoStateBadge', 'Running', 'accent');
    ['crashPanel', 'healthResult', 'healthWait'].forEach((id) => { const el = document.getElementById(id); if (el) el.hidden = true; });
  }

  async function runHealthCheck() {
    const waitEl = qs('#healthWait');
    const resultEl = qs('#healthResult');
    const crashEl = qs('#crashPanel');
    waitEl.hidden = false;
    resultEl.hidden = true;
    crashEl.hidden = true;
    setBadge('healthBadge', 'Waiting', 'accent');

    let elapsed = 0;
    const ticker = setInterval(() => {
      elapsed += 0.1;
      setText('healthWaitElapsed', `${elapsed.toFixed(1)}s`);
    }, 100);

    try {
      const res = await api('/api/health-check', { method: 'POST', body: { project_name: state.projectName, container_id: state.containerId, host: 'localhost', port: state.hostPort } });
      clearInterval(ticker);
      waitEl.hidden = true;
      const hc = res.health_check || {};
      setText('healthWaitAttempts', hc.attempts ?? '0');

      if (hc.container_crashed) {
        crashEl.hidden = false;
        setText('crashReason', hc.reason || 'Container exited unexpectedly.');
        setText('crashLogs', hc.container_logs || '(no logs captured)');
        setBadge('healthBadge', 'Crashed', 'danger');
        setStage('health', 'error');
        throw new ApiError(hc.reason || 'Container crashed before becoming ready.', 0, res);
      }

      if (!res.healthy) {
        setBadge('healthBadge', 'Timed out', 'danger');
        setStage('health', 'error');
        resultEl.hidden = false;
        setText('health-status-code', hc.status_code ?? '—');
        setText('health-elapsed', hc.elapsed_seconds != null ? `${hc.elapsed_seconds}s` : '—');
        setText('health-attempts', hc.attempts ?? '—');
        throw new ApiError(hc.reason || 'Container did not become ready in time.', 0, res);
      }

      resultEl.hidden = false;
      setText('health-status-code', hc.status_code ?? '—');
      setText('health-elapsed', hc.elapsed_seconds != null ? `${hc.elapsed_seconds}s` : '—');
      setText('health-attempts', hc.attempts ?? '—');
      setBadge('healthBadge', 'Ready', 'success');
      setStage('health', 'done');
      logLine(`Container ready after ${hc.elapsed_seconds}s (${hc.attempts} attempt(s)).`, 'success');
    } catch (err) {
      clearInterval(ticker);
      waitEl.hidden = true;
      throw err;
    }
  }

  qs('#retryHealthBtn')?.addEventListener('click', async () => {
    if (!state.containerPort) return;
    try {
      setStage('run', 'active');
      const runRes = await api('/api/docker/run', { method: 'POST', body: { image_name: state.imageName, container_name: 'performance-container', container_port: state.containerPort } });
      if (!runRes.success) throw new ApiError(runRes.message, 0, runRes);
      state.containerId = runRes.container_id;
      state.hostPort = runRes.port;
      state.host = runRes.host;
      renderRun(runRes, true);
      setStage('run', 'done');
      setStage('health', 'active');
      await runHealthCheck();
      toast('success', 'Container restarted', 'Health check passed — you can continue the pipeline manually from Routes onward.');
    } catch (err) {
      toast('error', 'Retry failed', err.message);
    }
  });

  /* ---------------------------------------------------------- */
  /* Existing repo modal                                          */
  /* ---------------------------------------------------------- */

  function promptExistingRepoModal() {
    return new Promise((resolve) => {
      const modal = qs('#existingRepoModal');
      modal.hidden = false;
      const cleanup = (choice) => {
        modal.hidden = true;
        continueBtn.removeEventListener('click', onContinue);
        deleteBtn.removeEventListener('click', onDelete);
        resolve(choice);
      };
      const continueBtn = qs('#continueExistingBtn');
      const deleteBtn = qs('#deleteAndCloneBtn');
      const onContinue = () => cleanup('continue');
      const onDelete = () => cleanup('delete');
      continueBtn.addEventListener('click', onContinue);
      deleteBtn.addEventListener('click', onDelete);
    });
  }

  /* ============================================================
     RENDER FUNCTIONS — Repository / Deployment / Routes
     ============================================================ */

  /* ============================================================
    REPOSITORY ANALYSIS — RENDER FUNCTIONS
    ============================================================ */
  
  /**
  * Render current repository information.
  */
  function renderRepoDetails() {
    setText(
      'repo-project-name',
      state.projectName || '—'
    );
  
    setText(
      'repo-url',
      state.githubUrl || '—'
    );
  
    setText(
      'repo-branch',
      state.branch || 'default'
    );
  
    setText(
      'repo-path',
      state.path || '—'
    );
  
    setBadge(
      'repoStateBadge',
      'Cloned',
      'success'
    );
  
    setText(
      'repoProjectIcon',
      getRepositoryInitials(
        state.projectName || 'Repository'
      )
    );
  
    updateRepoPipelineStep(
      'repoStepClone',
      'repoStepCloneText',
      'success',
      'Repository cloned'
    );
  }
  
  
  /**
  * Render framework detection result.
  *
  * Expected result from framework_detector.py:
  *
  * {
  *   detected_framework,
  *   confidence,
  *   support_tier,
  *   ambiguous,
  *   candidates,
  *   ...
  * }
  */
  function renderFramework(fw) {
  
    if (!fw) {
      return;
    }
  
    const framework =
      fw.detected_framework || null;
  
    const confidence =
      fw.confidence || 'Low';
  
    const supportTier =
      fw.support_tier || '—';
  
    setText(
      'fw-detected',
      framework
        ? formatFrameworkName(framework)
        : 'Not detected'
    );
  
    setText(
      'fw-tier',
      supportTier
    );
  
    setText(
      'fw-ambiguous',
      fmtBool(fw.ambiguous)
    );
  
    setText(
      'fw-entry-point',
      fw.entry_point || '—'
    );
  
    setText(
      'fw-confidence-value',
      confidence
    );
  
    setBadge(
      'frameworkConfidenceBadge',
      confidence,
      getConfidenceBadgeType(confidence)
    );
  
    setText(
      'frameworkLogo',
      getFrameworkInitials(framework)
    );
  
    renderFrameworkSignals(fw);
  
    updateRepoPipelineStep(
      'repoStepFramework',
      'repoStepFrameworkText',
      confidence === 'High'
        ? 'success'
        : confidence === 'Medium'
          ? 'warning'
          : 'danger',
      framework
        ? `${formatFrameworkName(framework)} detected`
        : 'Framework not detected'
    );
  }
  
  
  /**
  * Render framework evidence/signals.
  *
  * Reads the candidate/signal structure produced
  * by framework_detector.py.
  */
  function renderFrameworkSignals(fw) {
  
    const container =
      qs('#frameworkSignals');
  
    if (!container) {
      return;
    }
  
    const detected =
      fw.detected_framework;
  
    const candidates =
      Array.isArray(fw.candidates)
        ? fw.candidates
        : [];
  
    const candidate =
      candidates.find(
        (item) =>
          item.framework === detected
      );
    
    const signals =
      candidate &&
      Array.isArray(candidate.signals)
        ? candidate.signals
        : [];
    
    if (!signals.length) {
    
      container.innerHTML = `
        <div class="signal-empty">
          No detailed detection signals available.
        </div>
      `;
    
      return;
    }
  
    container.innerHTML =
      signals
        .map((signal) => {
        
          const name =
            signal.name ||
            'Detection signal';
        
          const description =
            signal.description ||
            signal.source ||
            'Signal detected in repository.';
        
          return `
            <div class="signal-item">
        
              <div class="signal-item__icon">
                ✓
              </div>
        
              <div class="signal-item__content">
        
                <strong>
                  ${escapeHtml(
                    formatSignalName(name)
                  )}
                </strong>
                
                <span>
                  ${escapeHtml(
                    description
                  )}
                </span>
                
              </div>
                
            </div>
          `;
                
        })
        .join('');
  }
  
  
  /**
  * Render entry-point locator result.
  *
  * Expected output from entry_point_locator.py:
  *
  * entry_point_file
  * entry_point_directory
  * entry_point_module
  * app_variable
  * run_command_hint
  * resolution_method
  * confidence
  * ambiguous
  * alternative_candidates
  * notes
  */
  function renderEntryPoint(location) {
  
    if (!location) {
      return;
    }
  
    setText(
      'entry-file',
      location.entry_point_file || '—'
    );
  
    setText(
      'entry-directory',
      location.entry_point_directory || '—'
    );
  
    setText(
      'entry-module',
      location.entry_point_module || '—'
    );
  
    setText(
      'entry-app-variable',
      location.app_variable || '—'
    );
  
    setText(
      'entry-run-command',
      location.run_command_hint || '—'
    );
  
    const confidence =
      location.confidence || 'None';
  
    setBadge(
      'entryConfidenceBadge',
      confidence,
      getConfidenceBadgeType(confidence)
    );
  
    updateRepoPipelineStep(
      'repoStepEntry',
      'repoStepEntryText',
      confidence === 'High'
        ? 'success'
        : confidence === 'Medium'
          ? 'warning'
          : confidence === 'None'
            ? 'danger'
            : 'warning',
      location.entry_point_file
        ? location.entry_point_file
        : location.run_command_hint
          ? 'Run command resolved'
          : 'Not resolved'
    );
  }
  
  
  /**
  * Render final RepositoryValidator result.
  *
  * Expected:
  *
  * {
  *   valid: true/false,
  *   errors: [],
  *   warnings: []
  * }
  */
  function renderValidation(val) {
  
    if (!val) {
      return;
    }
  
    const errors =
      Array.isArray(val.errors)
        ? val.errors
        : [];
  
    const warnings =
      Array.isArray(val.warnings)
        ? val.warnings
        : [];
  
    const valid =
      Boolean(val.valid);
  
    setBadge(
      'validationBadge',
      valid ? 'Passed' : 'Failed',
      valid ? 'success' : 'danger'
    );
  
    setText(
      'validationErrorCount',
      String(errors.length)
    );
  
    setText(
      'validationWarningCount',
      String(warnings.length)
    );
  
    setText(
      'validationErrorCountLabel',
      String(errors.length)
    );
  
    setText(
      'validationWarningCountLabel',
      String(warnings.length)
    );
  
    setText(
      'validationPipelineStatus',
      valid
        ? 'Ready'
        : 'Blocked'
    );
  
  
    const errEl =
      qs('#validationErrors');
  
    const warnEl =
      qs('#validationWarnings');
  
  
    if (errEl) {
    
      errEl.innerHTML =
        errors.length
          ? errors
              .map(
                (error) => `
                  <li class="validation-issue">
                    <span class="validation-issue__icon">!</span>
                    <span>${escapeHtml(error)}</span>
                  </li>
                `
              )
              .join('')
          : `
            <li class="issue-list__empty issue-list__empty--success">
              ✓ No blocking errors.
            </li>
          `;
    }
  
  
    if (warnEl) {
    
      warnEl.innerHTML =
        warnings.length
          ? warnings
              .map(
                (warning) => `
                  <li class="validation-issue">
                    <span class="validation-issue__icon">!</span>
                    <span>${escapeHtml(warning)}</span>
                  </li>
                `
              )
              .join('')
          : `
            <li class="issue-list__empty">
              No warnings.
            </li>
          `;
    }
  
  
    updateRepoPipelineStep(
      'repoStepValidation',
      'repoStepValidationText',
      valid
        ? 'success'
        : 'danger',
      valid
        ? 'Validation passed'
        : 'Validation failed'
    );
  
  
    updateRepositoryReadiness(valid, errors, warnings);
  }
  
  
  /* ============================================================
    REPOSITORY UI HELPERS
    ============================================================ */
  
  
  /**
  * Updates one step in the repository analysis pipeline.
  */
  function updateRepoPipelineStep(
    stepId,
    textId,
    status,
    text
  ) {
  
    const step =
      qs(`#${stepId}`);
  
    const textElement =
      qs(`#${textId}`);
  
    if (!step) {
      return;
    }
  
    step.classList.remove(
      'repo-step--success',
      'repo-step--warning',
      'repo-step--danger',
      'repo-step--pending'
    );
  
    step.classList.add(
      `repo-step--${status}`
    );
  
    if (textElement) {
      textElement.textContent =
        text || 'Waiting';
    }
  }
  
  
  /**
  * Updates the large repository readiness area.
  */
  function updateRepositoryReadiness(
    valid,
    errors,
    warnings
  ) {
  
    const title =
      qs('#repoReadinessTitle');
  
    const text =
      qs('#repoReadinessText');
  
    const score =
      qs('#repoReadinessScore strong');
  
    if (valid) {
    
      if (title) {
        title.textContent =
          'Repository is ready for deployment';
      }
    
      if (text) {
        text.textContent =
          warnings.length
            ? `${warnings.length} advisory warning${warnings.length > 1 ? 's' : ''} detected.`
            : 'All repository validation checks passed.';
      }
    
      if (score) {
        score.textContent = 'READY';
      }
    
    } else {
    
      if (title) {
        title.textContent =
          'Repository requires attention';
      }
    
      if (text) {
        text.textContent =
          `${errors.length} blocking issue${errors.length !== 1 ? 's' : ''} must be resolved before continuing.`;
      }
    
      if (score) {
        score.textContent = 'BLOCKED';
      }
    }
  }
  
  
  /**
  * Returns the correct badge type for confidence.
  */
  function getConfidenceBadgeType(confidence) {
  
    switch (String(confidence).toLowerCase()) {
    
      case 'high':
        return 'success';
    
      case 'medium':
        return 'warning';
    
      case 'low':
        return 'danger';
    
      default:
        return 'neutral';
    }
  }
  
  
  /**
  * Converts framework identifiers into readable names.
  */
  function formatFrameworkName(framework) {
  
    const names = {
      flask: 'Flask',
      fastapi: 'FastAPI',
      django: 'Django',
      express: 'Express.js',
      'express.js': 'Express.js',
      springboot: 'Spring Boot',
      'spring-boot': 'Spring Boot'
    };
  
    return names[String(framework).toLowerCase()]
      || framework
      || 'Unknown';
  }
  
  
  /**
  * Creates a short framework icon.
  */
  function getFrameworkInitials(framework) {
  
    const key =
      String(framework || '')
        .toLowerCase();
  
    const initials = {
      flask: 'F',
      fastapi: 'FA',
      django: 'D',
      express: 'EX',
      'express.js': 'EX',
      springboot: 'SB',
      'spring-boot': 'SB'
    };
  
    return initials[key] || '?';
  }
  
  
  /**
  * Creates repository initials.
  */
  function getRepositoryInitials(name) {
  
    if (!name) {
      return 'GH';
    }
  
    const parts =
      String(name)
        .trim()
        .split(/[\s_-]+/)
        .filter(Boolean);
  
    if (parts.length === 1) {
      return parts[0]
        .substring(0, 2)
        .toUpperCase();
    }
  
    return (
      parts[0][0] +
      parts[1][0]
    ).toUpperCase();
  }
  
  
  /**
  * Makes signal names readable.
  */
  function formatSignalName(name) {
  
    return String(name || '')
      .replace(/_/g, ' ')
      .replace(/\b\w/g, (char) =>
        char.toUpperCase()
      );
  }

  function renderBuild(res, success) {
    setBadge('buildBadge', success ? 'Built' : 'Failed', success ? 'success' : 'danger');
    setText('build-image-name', res.image_name || '—');
    setText('build-container-port', res.container_port ?? '—');
  }

  function renderRun(res, success) {
    setBadge('runBadge', success ? 'Running' : 'Failed', success ? 'success' : 'danger');
    setText('run-container-id', res.container_id ? res.container_id.slice(0, 20) : '—');
    setText('run-host', res.host || '—');
    setText('run-port', res.port ?? '—');
  }

  function renderRoutes(routesData) {
    const routes = routesData.routes || [];
    setText('routesCount', `${routes.length} route(s) discovered across ${routesData.files_scanned ?? 0} file(s) scanned.`);

    const methods = Array.from(new Set(routes.map((r) => (r.method || 'GET').toUpperCase()))).sort();
    const filterEl = qs('#methodFilter');
    filterEl.innerHTML = '<button type="button" class="method-filter__btn is-active" data-method="all">All</button>' +
      methods.map((m) => `<button type="button" class="method-filter__btn" data-method="${m}">${m}</button>`).join('');
    qsa('.method-filter__btn', filterEl).forEach((btn) => {
      btn.addEventListener('click', () => {
        qsa('.method-filter__btn', filterEl).forEach((b) => b.classList.remove('is-active'));
        btn.classList.add('is-active');
        renderRoutesTable(routes, btn.dataset.method);
      });
    });

    renderRoutesTable(routes, 'all');
  }

  function renderRoutesTable(routes, filterMethod) {
    const body = qs('#routesTableBody');
    const filtered = filterMethod === 'all' ? routes : routes.filter((r) => (r.method || '').toUpperCase() === filterMethod);
    if (!filtered.length) {
      body.innerHTML = '<tr><td colspan="3" class="data-table__empty">No routes match this filter.</td></tr>';
      return;
    }
    body.innerHTML = filtered.map((r) => {
      const method = (r.method || 'GET').toUpperCase();
      return `<tr>
        <td><span class="method-pill method-pill--${method.toLowerCase()}">${method}</span></td>
        <td class="mono">${escapeHtml(r.path || '—')}</td>
        <td>${escapeHtml(r.handler || '')}</td>
      </tr>`;
    }).join('');
  }

  function renderLocust(locust) {
    setBadge('locustBadge', 'Generated', 'success');
    qs('#locustSummary').textContent =
`locustfile: ${locust.locustfile_path || '—'}
framework: ${locust.framework || '—'}
routes used: ${locust.route_count ?? '—'}
tasks generated: ${locust.task_count ?? '—'}
skipped (unsupported method): ${locust.skipped_unsupported_method_count ?? 0}
fallback route used: ${fmtBool(locust.used_fallback_route)}
truncated: ${fmtBool(locust.truncated)}`;
  }

  /* ---------------------------------------------------------- */
  /* Load testing progress chips                                  */
  /* ---------------------------------------------------------- */

  function initLoadLevelChips() {
    const container = qs('#loadProgressLevels');
    container.innerHTML = LOAD_LEVEL_USERS.map((u) => `
      <div class="load-level-chip" data-users="${u}" data-state="pending">
        <span class="load-level-chip__users">${u}</span>
        <span class="load-level-chip__state">pending</span>
      </div>`).join('');
    setText('loadProgressText', 'Testing at increasing concurrency levels…');
    setText('loadProgressCount', `0 / ${LOAD_LEVEL_USERS.length} levels`);

    // Backend responds once at the end today; simulate a believable
    // sequential progression through the chips while we wait so the
    // user sees what's happening rather than a static screen.
    let i = 0;
    state._chipTimer = setInterval(() => {
      if (i > 0) {
        const prevChip = qs(`.load-level-chip[data-users="${LOAD_LEVEL_USERS[i - 1]}"]`);
        if (prevChip) { prevChip.dataset.state = 'done'; qs('.load-level-chip__state', prevChip).textContent = 'done'; }
      }
      if (i < LOAD_LEVEL_USERS.length) {
        const chip = qs(`.load-level-chip[data-users="${LOAD_LEVEL_USERS[i]}"]`);
        if (chip) { chip.dataset.state = 'active'; qs('.load-level-chip__state', chip).textContent = 'testing'; }
        setText('loadProgressCount', `${i} / ${LOAD_LEVEL_USERS.length} levels`);
        i += 1;
      } else {
        clearInterval(state._chipTimer);
      }
    }, 2600);
  }

  function finishLoadLevelChips() {
    if (state._chipTimer) clearInterval(state._chipTimer);
    qsa('.load-level-chip').forEach((chip) => {
      chip.dataset.state = 'done';
      qs('.load-level-chip__state', chip).textContent = 'done';
    });
    setText('loadProgressText', 'Load test complete.');
    setText('loadProgressCount', `${LOAD_LEVEL_USERS.length} / ${LOAD_LEVEL_USERS.length} levels`);
  }

  /* ============================================================
     RENDER FUNCTIONS — full pipeline result
     ============================================================ */

  function renderFullResult(result) {
    const levels = result.levels || [];
    renderLoadTestTableAndChart(levels);
    renderRuntimeMetrics(levels);
    renderUSL(result.prediction && result.prediction.usl, levels);
    renderLittlesLaw(result.little_law, levels);
    renderQueueing(result.queueing);
    renderBottleneck(result.bottleneck);
    renderCapacity(result.capacity);
    renderScalability(result.prediction && result.prediction.scalability, levels);
    renderRecommendations(result.recommendations);
    renderOverviewStats(result);
  }

  function renderOverviewStats(result) {
    const cap = result.capacity && result.capacity.results;
    const usl = result.prediction && result.prediction.usl;
    const bottleneck = result.bottleneck;

    if (cap) {
      animateNumber('stat-safe-users', cap.safe_users ?? 0, 0);
      animateNumber('stat-breaking-point', cap.breaking_point ?? 0, 0);
    }
    if (usl && usl.usl_metrics) {
      animateNumber('stat-peak-throughput', usl.usl_metrics.peak_throughput ?? 0, 1);
    }
    if (bottleneck && bottleneck.bottleneck) {
      setText('stat-bottleneck', bottleneck.bottleneck.resource || '—');
    } else if (bottleneck && bottleneck.bottleneck_analysis === 'unavailable') {
      setText('stat-bottleneck', 'Unavailable');
    }
  }

  /* ---- Load testing table + chart -------------------------- */

  function renderLoadTestTableAndChart(levels) {
    const body = qs('#loadTableBody');
    if (!levels.length) {
      body.innerHTML = '<tr><td colspan="6" class="data-table__empty">No load test data.</td></tr>';
      return;
    }
    body.innerHTML = levels.map((l) => `
      <tr>
        <td class="mono">${fmtInt(l.users)}</td>
        <td class="mono">${fmt(l.mean_throughput, 2)}</td>
        <td class="mono">${fmt(l.mean_average_response_time, 1)}</td>
        <td class="mono">${fmt(l.mean_cpu_usage, 1)}</td>
        <td class="mono">${fmt(l.mean_memory_usage, 2)}</td>
        <td class="mono">${fmtPercent(l.mean_error_rate, 2)}</td>
      </tr>`).join('');

    const labels = levels.map((l) => String(fmtInt(l.users)));
    makeOrUpdateChart('loadTest', 'loadTestChart', {
      type: 'line',
      data: {
        labels,
        datasets: [
          Object.assign(measuredDataset('Throughput (req/s)', levels.map((l) => l.mean_throughput)), { yAxisID: 'y' }),
          Object.assign(predictedDataset('Avg response time (ms)', levels.map((l) => l.mean_average_response_time)), { borderDash: [], borderColor: cssVar('--ink-muted'), pointBackgroundColor: cssVar('--ink-muted'), yAxisID: 'y1' }),
        ],
      },
      options: baseChartOptions({
        plugins: { legend: { display: true, labels: { color: cssVar('--ink-muted'), font: { family: 'Inter', size: 11.5 } } }, tooltip: baseChartOptions().plugins.tooltip },
        scales: {
          x: baseChartOptions().scales.x,
          y: Object.assign({}, baseChartOptions().scales.y, { position: 'left', title: { display: true, text: 'req/s', color: cssVar('--ink-muted') } }),
          y1: Object.assign({}, baseChartOptions().scales.y, { position: 'right', grid: { drawOnChartArea: false }, title: { display: true, text: 'ms', color: cssVar('--ink-muted') } }),
        },
      }),
    });
  }

  /* ---- Runtime metrics --------------------------------------- */

  function renderRuntimeMetrics(levels) {
    const labels = levels.map((l) => String(fmtInt(l.users)));

    makeOrUpdateChart('cpuMem', 'cpuMemChart', {
      type: 'line',
      data: { labels, datasets: [
        measuredDataset('CPU %', levels.map((l) => l.mean_cpu_usage)),
        Object.assign(predictedDataset('Memory %', levels.map((l) => l.mean_memory_usage)), { borderDash: [], borderColor: cssVar('--cyan') }),
      ]},
      options: baseChartOptions({ plugins: { legend: { display: true, labels: { color: cssVar('--ink-muted'), font: { size: 11 } } } } }),
    });

    makeOrUpdateChart('disk', 'diskChart', {
      type: 'line',
      data: { labels, datasets: [
        measuredDataset('Read MB/s', levels.map((l) => l.mean_disk_read_mb_s)),
        Object.assign(predictedDataset('Write MB/s', levels.map((l) => l.mean_disk_write_mb_s)), { borderDash: [], borderColor: cssVar('--cyan') }),
      ]},
      options: baseChartOptions({ plugins: { legend: { display: true, labels: { color: cssVar('--ink-muted'), font: { size: 11 } } } } }),
    });

    makeOrUpdateChart('network', 'networkChart', {
      type: 'line',
      data: { labels, datasets: [
        measuredDataset('RX MB/s', levels.map((l) => l.mean_network_rx_mb_s)),
        Object.assign(predictedDataset('TX MB/s', levels.map((l) => l.mean_network_tx_mb_s)), { borderDash: [], borderColor: cssVar('--cyan') }),
      ]},
      options: baseChartOptions({ plugins: { legend: { display: true, labels: { color: cssVar('--ink-muted'), font: { size: 11 } } } } }),
    });

    makeOrUpdateChart('error', 'errorChart', {
      type: 'bar',
      data: { labels, datasets: [{
        label: 'Error rate %', data: levels.map((l) => l.mean_error_rate),
        backgroundColor: cssVar('--danger'), borderRadius: 4, maxBarThickness: 34,
      }]},
      options: baseChartOptions(),
    });
  }

  /* ---- USL ---------------------------------------------------- */
  
  function renderUSL(usl, levels) {
  
    /*
    * USL frontend renderer
    *
    * Backend payload:
    *   parameters
    *   usl_metrics
    *   predictions
    *   predictions_with_confidence
    *   efficiency
    *   scalability_classification
    *   fit_quality
    *   fit_quality_status
    *   parameter_interpretation
    *   observed_vs_predicted
    *   parameter_uncertainty
    *   prediction_reliability
    *   observed_range
    */
  
    const safeLevels = Array.isArray(levels) ? levels : [];
  
    /* ---------------------------------------------------------- */
    /* Empty / failed state                                       */
    /* ---------------------------------------------------------- */
  
    if (!usl || usl.success === false || !usl.parameters) {
    
      setText('usl-sigma', '—');
      setText('usl-kappa', '—');
      setText('usl-baseline', '—');
      setText('usl-peak', '—');
      setText('usl-optimal', '—');
      setText('usl-saturation', '—');
    
      setText('usl-r2', '—');
      setText('usl-rmse', '—');
      setText('usl-loo-r2', '—');
    
      setText('usl-observations', '—');
      setText('usl-tested-range', '—');
      setText('usl-peak-range', '—');
      setText('usl-extrapolated-count', '—');
    
      setText('uslSigmaInterpretation', '—');
      setText('uslKappaInterpretation', '—');
    
      setText('usl-sigma-stderr', '—');
      setText('usl-kappa-stderr', '—');
      setText('usl-kappa-relative-uncertainty', '—');
    
      setText('usl-fit-summary', 'No fit assessment available.');
      setText('uslClassificationDescription', 'No classification available.');
    
      setBadge('uslFitStatus', 'Fit unavailable', 'neutral');
      setBadge('uslFitStatusSecondary', '—', 'neutral');
      setBadge('uslReliabilityBadge', 'Reliability unavailable', 'neutral');
      setBadge('uslClassification', '—', 'neutral');
      setBadge('uslResidualBadge', '—', 'neutral');
    
      const emptyObserved = qs('#uslObservedTableBody');
      if (emptyObserved) {
        emptyObserved.innerHTML = `
          <tr>
            <td colspan="5" class="data-table__empty">
              USL model results are unavailable.
            </td>
          </tr>
        `;
      }
    
      const emptyPredictions = qs('#uslPredictionTableBody');
      if (emptyPredictions) {
        emptyPredictions.innerHTML = `
          <tr>
            <td colspan="5" class="data-table__empty">
              USL predictions are unavailable.
            </td>
          </tr>
        `;
      }
    
      const warningPanel = qs('#uslExtrapolationPanel');
      if (warningPanel) warningPanel.hidden = true;
    
      return;
    }
  
  
    /* ---------------------------------------------------------- */
    /* Core parameters                                            */
    /* ---------------------------------------------------------- */
  
    const parameters = usl.parameters || {};
    const metrics = usl.usl_metrics || {};
  
    const sigma = Number(parameters.sigma);
    const kappa = Number(parameters.kappa);
    const baseline = Number(parameters.baseline_throughput);
  
    setText(
      'usl-sigma',
      fmt(sigma, 6)
    );
  
    setText(
      'usl-kappa',
      fmt(kappa, 8)
    );
  
    setText(
      'usl-baseline',
      fmt(baseline, 2)
    );
  
    setText(
      'usl-peak',
      fmt(metrics.peak_throughput, 2)
    );
  
    setText(
      'usl-optimal',
      fmtInt(metrics.optimal_users)
    );
  
    setText(
      'usl-saturation',
      fmtInt(metrics.saturation_point)
    );
  
  
    /* ---------------------------------------------------------- */
    /* Observed range                                             */
    /* ---------------------------------------------------------- */
  
    const observedRange = usl.observed_range || {};
  
    const minUsers = Number(observedRange.min_users);
    const maxUsers = Number(observedRange.max_users);
  
    if (
      Number.isFinite(minUsers)
      && Number.isFinite(maxUsers)
    ) {
    
      setText(
        'usl-tested-range',
        `${fmtInt(minUsers)} – ${fmtInt(maxUsers)}`
      );
    
    } else if (safeLevels.length) {
    
      const levelUsers = safeLevels
        .map((level) => Number(level.users))
        .filter((value) => Number.isFinite(value));
    
      if (levelUsers.length) {
      
        setText(
          'usl-tested-range',
          `${fmtInt(Math.min(...levelUsers))} – ${fmtInt(Math.max(...levelUsers))}`
        );
      
      }
    
    }
  
    setText(
      'usl-observations',
      fmtInt(usl.observations)
    );
  
  
    /* ---------------------------------------------------------- */
    /* Extrapolation information                                  */
    /* ---------------------------------------------------------- */
  
    const extrapolatedPoints = Array.isArray(usl.extrapolated_points)
      ? usl.extrapolated_points
      : [];
  
    setText(
      'usl-extrapolated-count',
      fmtInt(extrapolatedPoints.length)
    );
  
  
    /* ---------------------------------------------------------- */
    /* Peak reliability                                           */
    /* ---------------------------------------------------------- */
  
    const reliability = usl.prediction_reliability || {};
  
    const peakInRange = reliability.peak_observed_in_tested_range;
  
    if (peakInRange === true) {
    
      setText(
        'usl-peak-range',
        'Yes'
      );
    
    } else if (peakInRange === false) {
    
      setText(
        'usl-peak-range',
        'No'
      );
    
    } else {
    
      setText(
        'usl-peak-range',
        'Not finite'
      );
    
    }
  
  
    /* ---------------------------------------------------------- */
    /* Fit quality                                                */
    /* ---------------------------------------------------------- */
  
    const fit = usl.fit_quality || {};
    const r2 = Number(fit.r2);
    const rmse = Number(fit.rmse);
  
    setText(
      'usl-r2',
      Number.isFinite(r2)
        ? r2.toFixed(3)
        : '—'
    );
  
    setText(
      'usl-rmse',
      Number.isFinite(rmse)
        ? fmt(rmse, 2)
        : '—'
    );
  
  
    /* ---------------------------------------------------------- */
    /* LOO cross-validation                                       */
    /* ---------------------------------------------------------- */
  
    const loo = reliability.loo_cv || {};
  
    if (
      loo.available
      && Number.isFinite(Number(loo.loo_r2))
    ) {
    
      setText(
        'usl-loo-r2',
        Number(loo.loo_r2).toFixed(3)
      );
    
    } else {
    
      setText(
        'usl-loo-r2',
        'N/A'
      );
    
    }
  
  
    /* ---------------------------------------------------------- */
    /* Fit status                                                  */
    /* ---------------------------------------------------------- */
  
    const fitStatus = usl.fit_quality_status || 'Unknown';
  
    let fitVariant = 'neutral';
  
    switch (fitStatus.toLowerCase()) {
    
      case 'excellent':
        fitVariant = 'success';
        break;
    
      case 'good':
        fitVariant = 'success';
        break;
    
      case 'moderate':
        fitVariant = 'warning';
        break;
    
      case 'poor':
        fitVariant = 'danger';
        break;
    
      default:
        fitVariant = 'neutral';
        break;
    
    }
  
    setBadge(
      'uslFitStatus',
      `Fit: ${fitStatus}`,
      fitVariant
    );
  
    setBadge(
      'uslFitStatusSecondary',
      fitStatus,
      fitVariant
    );
  
  
    /* ---------------------------------------------------------- */
    /* Fit summary                                                */
    /* ---------------------------------------------------------- */
  
    let fitSummary = 'Fit quality could not be determined.';
  
    if (fitStatus === 'Excellent') {
    
      fitSummary =
        'The USL curve explains the measured throughput very closely.';
    
    } else if (fitStatus === 'Good') {
    
      fitSummary =
        'The USL curve provides a strong explanation of the measured throughput.';
    
    } else if (fitStatus === 'Moderate') {
    
      fitSummary =
        'The USL curve captures the general scaling trend, but uncertainty remains.';
    
    } else if (fitStatus === 'Poor') {
    
      fitSummary =
        'The measured throughput does not closely follow the fitted USL curve.';
    
    }
  
    setText(
      'usl-fit-summary',
      fitSummary
    );
  
  
    /* ---------------------------------------------------------- */
    /* Prediction reliability                                    */
    /* ---------------------------------------------------------- */
  
    const reliabilityStatus =
      reliability.status || 'unknown';
  
    const usableForExtrapolation =
      reliability.usable_for_extrapolation === true;
  
    let reliabilityVariant = 'warning';
  
    if (usableForExtrapolation) {
      reliabilityVariant = 'success';
    }
  
    if (reliabilityStatus === 'low') {
      reliabilityVariant = 'warning';
    }
  
    setBadge(
      'uslReliabilityBadge',
      `Extrapolation: ${reliabilityStatus}`,
      reliabilityVariant
    );
  
  
    /* ---------------------------------------------------------- */
    /* Scalability classification                                 */
    /* ---------------------------------------------------------- */
  
    const classification =
      usl.scalability_classification || {};
  
    const classificationName =
      classification.classification || 'Unknown';
  
    const classificationDescription =
      classification.description ||
      'No scalability interpretation is available.';
  
    let classificationVariant = 'neutral';
  
    const classificationLower =
      classificationName.toLowerCase();
  
    if (classificationLower.includes('linear')) {
    
      classificationVariant = 'success';
    
    } else if (classificationLower.includes('sublinear')) {
    
      classificationVariant = 'warning';
    
    } else if (classificationLower.includes('retrograde')) {
    
      classificationVariant = 'danger';
    
    }
  
    setBadge(
      'uslClassification',
      classificationName,
      classificationVariant
    );
  
    setText(
      'uslClassificationDescription',
      classificationDescription
    );
  
  
    /* ---------------------------------------------------------- */
    /* Parameter interpretation                                   */
    /* ---------------------------------------------------------- */
  
    const interpretation =
      usl.parameter_interpretation || {};
  
    setText(
      'uslSigmaInterpretation',
      interpretation.sigma ||
        'Contention interpretation unavailable.'
    );
  
    setText(
      'uslKappaInterpretation',
      interpretation.kappa ||
        'Coherency interpretation unavailable.'
    );
  
  
    /* ---------------------------------------------------------- */
    /* Parameter uncertainty                                      */
    /* ---------------------------------------------------------- */
  
    const uncertainty =
      usl.parameter_uncertainty || {};
  
    setText(
      'usl-sigma-stderr',
      fmt(uncertainty.sigma_stderr, 8)
    );
  
    setText(
      'usl-kappa-stderr',
      fmt(uncertainty.kappa_stderr, 8)
    );
  
    const kappaRelativeUncertainty =
      reliability.kappa_relative_uncertainty;
  
    if (
      kappaRelativeUncertainty !== null
      && kappaRelativeUncertainty !== undefined
      && Number.isFinite(Number(kappaRelativeUncertainty))
    ) {
    
      setText(
        'usl-kappa-relative-uncertainty',
        fmtPercent(
          Number(kappaRelativeUncertainty) * 100,
          1
        )
      );
    
    } else {
    
      setText(
        'usl-kappa-relative-uncertainty',
        'N/A'
      );
    
    }
  
  
    /* ---------------------------------------------------------- */
    /* Uncertainty note                                           */
    /* ---------------------------------------------------------- */
  
    let uncertaintyNote =
      'Parameter uncertainty assessment is not available.';
  
    if (
      kappaRelativeUncertainty !== null
      && Number.isFinite(Number(kappaRelativeUncertainty))
    ) {
    
      const relative =
        Number(kappaRelativeUncertainty);
    
      if (relative > 0.5) {
      
        uncertaintyNote =
          'κ has high relative uncertainty. The coherency parameter and the derived optimal-user/saturation estimates should therefore be treated as directional rather than precise.';
      
      } else {
      
        uncertaintyNote =
          'The current measurements provide a measurable estimate of the USL coherency parameter.';
      
      }
    
    }
  
    setText(
      'uslUncertaintyNote',
      uncertaintyNote
    );
  
  
    /* ---------------------------------------------------------- */
    /* Observed vs predicted table                                */
    /* ---------------------------------------------------------- */
  
    const observedVsPredicted =
      usl.observed_vs_predicted || {};
  
    const observedEntries =
      Object.entries(observedVsPredicted)
        .map(([key, value]) => {
        
          const users = Number(key);
          const row = value || {};
        
          const observed =
            Number(row.observed);
        
          const predicted =
            Number(row.predicted);
        
          const residual =
            Number(row.residual);
        
          let deviation = null;
        
          if (
            Number.isFinite(observed)
            && observed !== 0
            && Number.isFinite(residual)
          ) {
          
            deviation =
              Math.abs(residual / observed) * 100;
          
          }
        
          return {
            users,
            observed,
            predicted,
            residual,
            deviation,
          };
        
        })
        .filter(
          (row) => Number.isFinite(row.users)
        )
        .sort(
          (a, b) => a.users - b.users
        );
      
      
    const observedBody =
      qs('#uslObservedTableBody');
      
    if (observedBody) {
    
      if (!observedEntries.length) {
      
        observedBody.innerHTML = `
          <tr>
            <td colspan="5" class="data-table__empty">
              No observed-vs-predicted data available.
            </td>
          </tr>
        `;
      
      } else {
      
        observedBody.innerHTML =
          observedEntries
            .map((row) => {
            
              const residualVariant =
                Number.isFinite(row.residual)
                  ? row.residual >= 0
                    ? 'success'
                    : 'warning'
                  : 'neutral';
            
              const residualText =
                Number.isFinite(row.residual)
                  ? `${row.residual >= 0 ? '+' : ''}${fmt(row.residual, 2)}`
                  : '—';
            
              return `
                <tr>
            
                  <td class="mono">
                    ${fmtInt(row.users)}
                  </td>
            
                  <td class="mono">
                    ${fmt(row.observed, 2)}
                  </td>
            
                  <td class="mono">
                    ${fmt(row.predicted, 2)}
                  </td>
            
                  <td class="mono">
                    <span class="badge badge--${residualVariant}">
                      ${residualText}
                    </span>
                  </td>
            
                  <td class="mono">
                    ${
                      Number.isFinite(row.deviation)
                        ? fmtPercent(row.deviation, 1)
                        : '—'
                    }
                  </td>
            
                </tr>
              `;
            
            })
            .join('');
          
      }
    
    }
  
  
    /* ---------------------------------------------------------- */
    /* Overall residual summary                                   */
    /* ---------------------------------------------------------- */
  
    if (observedEntries.length) {
    
      const absoluteResiduals =
        observedEntries
          .map((row) => Math.abs(row.residual))
          .filter(Number.isFinite);
    
      const meanAbsoluteResidual =
        absoluteResiduals.length
          ? absoluteResiduals.reduce(
              (sum, value) => sum + value,
              0
            ) / absoluteResiduals.length
          : null;
          
      setBadge(
        'uslResidualBadge',
        meanAbsoluteResidual !== null
          ? `Mean |residual| ${fmt(meanAbsoluteResidual, 2)} req/s`
          : 'Residuals available',
        'neutral'
      );
    
    } else {
    
      setBadge(
        'uslResidualBadge',
        'No residual data',
        'neutral'
      );
    
    }
  
  
    /* ---------------------------------------------------------- */
    /* Future prediction table                                   */
    /* ---------------------------------------------------------- */
  
    const predictions =
      usl.predictions || {};
  
    const efficiency =
      usl.efficiency || {};
  
    const confidence =
      usl.predictions_with_confidence || {};
  
  
    /*
    * IMPORTANT:
    *
    * Do NOT convert the JSON object keys to numbers and then use
    * predictions[number].
    *
    * Flask serializes Python float dictionary keys such as:
    *
    *     1000.0
    *
    * into JSON object keys such as:
    *
    *     "1000.0"
    *
    * Therefore Object.entries() is used throughout.
    */
  
    const predictionEntries =
      Object.entries(predictions)
        .map(([key, value]) => {
        
          const users =
            Number(key);
        
          const throughput =
            Number(value);
        
          const efficiencyValue =
            efficiency[key] !== undefined
              ? Number(efficiency[key])
              : null;
        
          const confidenceValue =
            confidence[key] || null;
        
          return {
            key,
            users,
            throughput,
            efficiency: efficiencyValue,
            confidence: confidenceValue,
          };
        
        })
        .filter(
          (row) => Number.isFinite(row.users)
        )
        .sort(
          (a, b) => a.users - b.users
        );
      
      
    const predictionBody =
      qs('#uslPredictionTableBody');
      
    if (predictionBody) {
    
      if (!predictionEntries.length) {
      
        predictionBody.innerHTML = `
          <tr>
            <td colspan="5" class="data-table__empty">
              No predictions available.
            </td>
          </tr>
        `;
      
      } else {
      
        predictionBody.innerHTML =
          predictionEntries
            .map((row) => {
            
              const ci =
                row.confidence;
            
              let confidenceText = '—';
            
              if (
                ci
                && Number.isFinite(Number(ci.lower))
                && Number.isFinite(Number(ci.upper))
              ) {
              
                confidenceText =
                  `${fmt(ci.lower, 2)} – ${fmt(ci.upper, 2)}`;
              
              }
            
              const isExtrapolated =
                extrapolatedPoints.some(
                  (point) =>
                    Number(point) === row.users
                );
              
              return `
                <tr>
              
                  <td class="mono">
                    ${fmtInt(row.users)}
                  </td>
              
                  <td class="mono">
                    <strong>
                      ${fmt(row.throughput, 2)}
                    </strong>
                    <span class="table-unit">
                      req/s
                    </span>
                  </td>
              
                  <td class="mono">
                    ${
                      row.efficiency !== null
                        && Number.isFinite(row.efficiency)
                        ? fmtPercent(
                            row.efficiency * 100,
                            1
                          )
                        : '—'
                    }
                  </td>
                        
                  <td class="mono">
                    ${confidenceText}
                  </td>
                        
                  <td>
                    <span class="badge badge--${
                      isExtrapolated
                        ? 'warning'
                        : 'neutral'
                    }">
                      ${
                        isExtrapolated
                          ? 'Extrapolated'
                          : 'Tested'
                      }
                    </span>
                  </td>
                        
                </tr>
              `;
                        
            })
            .join('');
          
      }
    
    }
  
  
    /* ---------------------------------------------------------- */
    /* Extrapolation warning                                     */
    /* ---------------------------------------------------------- */
  
    const warningPanel =
      qs('#uslExtrapolationPanel');
  
    if (warningPanel) {
    
      if (
        extrapolatedPoints.length
        && usl.extrapolation_warning
      ) {
      
        warningPanel.hidden = false;
      
        setText(
          'uslExtrapolationWarning',
          usl.extrapolation_warning
        );
      
      } else {
      
        warningPanel.hidden = true;
      
      }
    
    }
  
  
    /* ---------------------------------------------------------- */
    /* Summary cards                                              */
    /* ---------------------------------------------------------- */
  
    setText(
      'uslSummaryClassification',
      classificationName
    );
  
    setText(
      'uslSummaryOptimal',
      Number.isFinite(Number(metrics.optimal_users))
        ? `${fmtInt(metrics.optimal_users)} users`
        : 'Not finite'
    );
  
    setText(
      'uslSummaryPeak',
      Number.isFinite(Number(metrics.peak_throughput))
        ? `${fmt(metrics.peak_throughput, 2)} req/s`
        : '—'
    );
  
    setText(
      'uslSummaryReliability',
      reliabilityStatus
        ? reliabilityStatus.charAt(0).toUpperCase()
          + reliabilityStatus.slice(1)
        : 'Unknown'
    );
  
  
    /* ---------------------------------------------------------- */
    /* Chart data                                                 */
    /* ---------------------------------------------------------- */
  
    /*
    * IMPORTANT:
    *
    * The old implementation used `rows` here even after the table
    * was changed to `predictionEntries`.
    *
    * This implementation NEVER references `rows`.
    */
  
    const measuredRows =
      safeLevels
        .map((level) => ({
          users: Number(level.users),
          throughput: Number(level.mean_throughput),
        }))
        .filter(
          (row) =>
            Number.isFinite(row.users)
            && Number.isFinite(row.throughput)
        )
        .sort(
          (a, b) => a.users - b.users
        );
      
      
    const fittedRows =
      observedEntries
        .map((row) => ({
          users: row.users,
          throughput: row.predicted,
        }))
        .filter(
          (row) =>
            Number.isFinite(row.users)
            && Number.isFinite(row.throughput)
        )
        .sort(
          (a, b) => a.users - b.users
        );
      
      
    const futureRows =
      predictionEntries
        .map((row) => ({
          users: row.users,
          throughput: row.throughput,
          confidence: row.confidence,
        }))
        .filter(
          (row) =>
            Number.isFinite(row.users)
            && Number.isFinite(row.throughput)
        )
        .sort(
          (a, b) => a.users - b.users
        );
      
      
    /*
    * Create one ordered user axis.
    *
    * Example:
    *
    * 20, 50, 100, 200, 300, 500, 1000, 2000, 5000
    */
      
    const chartUsers =
      Array.from(
        new Set(
          measuredRows
            .map((row) => row.users)
            .concat(
              futureRows.map((row) => row.users)
            )
        )
      ).sort(
        (a, b) => a - b
      );
    
    
    const chartLabels =
      chartUsers.map(
        (users) => fmtInt(users)
      );
    
    
    /* ---------------------------------------------------------- */
    /* Measured chart data                                        */
    /* ---------------------------------------------------------- */
    
    const measuredChartData =
      chartUsers.map((users) => {
      
        const row =
          measuredRows.find(
            (item) => item.users === users
          );
        
        return row
          ? row.throughput
          : null;
        
      });
    
    
    /* ---------------------------------------------------------- */
    /* Fitted + future USL curve                                 */
    /* ---------------------------------------------------------- */
    
    const fittedChartData =
      chartUsers.map((users) => {
      
        const fitted =
          fittedRows.find(
            (item) => item.users === users
          );
        
        if (fitted) {
          return fitted.throughput;
        }
      
        const future =
          futureRows.find(
            (item) => item.users === users
          );
        
        return future
          ? future.throughput
          : null;
        
      });
    
    
    /* ---------------------------------------------------------- */
    /* Confidence interval                                       */
    /* ---------------------------------------------------------- */
    
    const lowerConfidenceData =
      chartUsers.map((users) => {
      
        const future =
          futureRows.find(
            (item) => item.users === users
          );
        
        if (
          future
          && future.confidence
          && Number.isFinite(Number(future.confidence.lower))
        ) {
        
          return Number(
            future.confidence.lower
          );
        
        }
      
        return null;
      
      });
    
    
    const upperConfidenceData =
      chartUsers.map((users) => {
      
        const future =
          futureRows.find(
            (item) => item.users === users
          );
        
        if (
          future
          && future.confidence
          && Number.isFinite(Number(future.confidence.upper))
        ) {
        
          return Number(
            future.confidence.upper
          );
        
        }
      
        return null;
      
      });
    
    
    /* ---------------------------------------------------------- */
    /* Create/update USL chart                                   */
    /* ---------------------------------------------------------- */
    
    makeOrUpdateChart(
      'usl',
      'uslChart',
      {
        type: 'line',
      
        data: {
        
          labels: chartLabels,
        
          datasets: [
          
            /*
            * Measured throughput
            */
            Object.assign(
              measuredDataset(
                'Measured throughput',
                measuredChartData
              ),
              {
                spanGaps: false,
                pointRadius: 4,
                pointHoverRadius: 6,
              }
            ),
          
            /*
            * Fitted USL curve + extrapolation
            */
            Object.assign(
              predictedDataset(
                'USL fitted / predicted',
                fittedChartData
              ),
              {
                spanGaps: true,
                pointRadius: 3,
                pointHoverRadius: 6,
                tension: 0.25,
              }
            ),
          
            /*
            * Lower confidence boundary
            */
            Object.assign(
              predictedDataset(
                '95% CI lower',
                lowerConfidenceData
              ),
              {
                borderDash: [4, 5],
                pointRadius: 0,
                tension: 0.25,
              }
            ),
          
            /*
            * Upper confidence boundary.
            *
            * `fill: '-1'` fills the area down to the previous
            * confidence-boundary dataset.
            */
            Object.assign(
              predictedDataset(
                '95% CI upper',
                upperConfidenceData
              ),
              {
                borderDash: [4, 5],
                pointRadius: 0,
                tension: 0.25,
                fill: '-1',
                backgroundColor: 'rgba(255,255,255,0.04)',
              }
            ),
          
          ],
        
        },
      
        options: baseChartOptions({
        
          plugins: {
          
            legend: {
              display: true,
              labels: {
                color: cssVar('--ink-muted'),
                font: {
                  family: 'Inter',
                  size: 11.5,
                },
              },
            },
          
            tooltip:
              baseChartOptions().plugins.tooltip,
          
          },
        
          scales: {
          
            x: Object.assign(
              {},
              baseChartOptions().scales.x,
              {
                title: {
                  display: true,
                  text: 'Concurrent users',
                  color: cssVar('--ink-muted'),
                },
              }
            ),
          
            y: Object.assign(
              {},
              baseChartOptions().scales.y,
              {
                title: {
                  display: true,
                  text: 'Throughput (req/s)',
                  color: cssVar('--ink-muted'),
                },
              }
            ),
          
          },
        
        }),
      
      }
    );
  
  }

  /* ---- Little's Law -------------------------------------------- */

  function renderLittlesLaw(littleLaw, levels) {

    /*
    * Little's Law frontend renderer
    *
    * Mathematical relationship:
    *
    *     L = λ × W
    *
    * where:
    *
    *     L = average requests in the system
    *     λ = arrival rate / throughput
    *     W = average response time in seconds
    *
    * Backend result contains:
    *
    *     levels
    *     summary
    *     signals
    *
    * The renderer intentionally uses the user count supplied inside
    * each backend row when available instead of assuming that
    * `levels[i]` always matches `littleLaw.levels[i]`.
    */


    /* ---------------------------------------------------------- */
    /* Safe input                                                 */
    /* ---------------------------------------------------------- */

    const rows =
      littleLaw && Array.isArray(littleLaw.levels)
        ? littleLaw.levels
        : [];


    const safeLevels =
      Array.isArray(levels)
        ? levels
        : [];


    /* ---------------------------------------------------------- */
    /* Empty state                                                */
    /* ---------------------------------------------------------- */

    if (!rows.length) {

      setBadge(
        'littleLawOverallStatus',
        'No data',
        'neutral'
      );

      setBadge(
        'littleLawTableStatus',
        'Unavailable',
        'neutral'
      );

      setText('littleLawPeakArrival', '—');
      setText('littleLawPeakResponse', '—');
      setText('littleLawPeakOccupancy', '—');
      setText('littleLawHighestUsers', '—');

      setText('littleLawLambda', '—');
      setText('littleLawW', '—');
      setText('littleLawL', '—');
      setText('littleLawUsers', '—');

      setText('littleLawMaxL', '—');

      setText(
        'littleLawOccupancyDescription',
        'No occupancy information is available.'
      );

      setText(
        'littleLawResponseDescription',
        'No response-time information is available.'
      );

      setText(
        'littleLawArrivalDescription',
        'No arrival-rate information is available.'
      );

      setText(
        'littleLawLInterpretation',
        'No Little’s Law result is available.'
      );

      setText(
        'littleLawLambdaInterpretation',
        'No arrival-rate result is available.'
      );

      setText(
        'littleLawWInterpretation',
        'No response-time result is available.'
      );

      const body =
        qs('#littlesLawTableBody');

      if (body) {

        body.innerHTML = `
          <tr>
            <td colspan="6" class="data-table__empty">
              No Little's Law data available.
            </td>
          </tr>
        `;

      }

      return;
    }


    /* ---------------------------------------------------------- */
    /* Normalize backend rows                                     */
    /* ---------------------------------------------------------- */

    const normalizedRows =
      rows
        .map((row, index) => {

          const r = row || {};

          /*
          * Prefer a user/load field returned directly by the backend.
          *
          * Fall back to the corresponding Locust level only when
          * the backend does not provide one.
          */

          let users =
            Number(
              r.users ??
              r.load_users ??
              r.concurrent_users
            );

          if (!Number.isFinite(users)) {

            users =
              Number(
                safeLevels[index]?.users
              );

          }


          /*
          * Backend output may expose:
          *
          * arrival_rate
          * response_time
          * requests_in_system
          *
          * Keep the frontend tolerant of alternate naming.
          */

          const arrivalRate =
            Number(
              r.arrival_rate ??
              r.lambda ??
              r.throughput
            );


          const responseTime =
            Number(
              r.response_time ??
              r.average_response_time
            );


          const requestsInSystem =
            Number(
              r.requests_in_system ??
              r.concurrent_requests ??
              r.L
            );


          const signals =
            r.signals || {};


          return {

            original: r,

            index,

            users,

            arrivalRate,

            responseTime,

            requestsInSystem,

            classification:
              r.load_classification ||
              r.classification ||
              '—',

            highOccupancy:
              signals.high_occupancy,

            criticalOccupancy:
              signals.critical_occupancy,

            occupancyTrend:
              signals.occupancy_trend,

            responseTimeTrend:
              signals.response_time_trend,

            arrivalRateTrend:
              signals.arrival_rate_trend,

          };

        })
        .filter(
          (row) =>
            Number.isFinite(row.users)
            || Number.isFinite(row.arrivalRate)
            || Number.isFinite(row.requestsInSystem)
        );


    if (!normalizedRows.length) {
      return;
    }


    /* ---------------------------------------------------------- */
    /* Sort by load                                               */
    /* ---------------------------------------------------------- */

    normalizedRows.sort(
      (a, b) => {

        if (
          Number.isFinite(a.users)
          && Number.isFinite(b.users)
        ) {

          return a.users - b.users;

        }

        return a.index - b.index;

      }
    );


    /* ---------------------------------------------------------- */
    /* Calculate summary values                                  */
    /* ---------------------------------------------------------- */

    const validArrivalRates =
      normalizedRows
        .map((row) => row.arrivalRate)
        .filter(Number.isFinite);


    const validResponseTimes =
      normalizedRows
        .map((row) => row.responseTime)
        .filter(Number.isFinite);


    const validLValues =
      normalizedRows
        .map((row) => row.requestsInSystem)
        .filter(Number.isFinite);


    const peakArrival =
      validArrivalRates.length
        ? Math.max(...validArrivalRates)
        : null;


    const peakResponse =
      validResponseTimes.length
        ? Math.max(...validResponseTimes)
        : null;


    const peakL =
      validLValues.length
        ? Math.max(...validLValues)
        : null;


    const highestUsers =
      normalizedRows
        .map((row) => row.users)
        .filter(Number.isFinite)
        .reduce(
          (max, value) => Math.max(max, value),
          -Infinity
        );


    /* ---------------------------------------------------------- */
    /* Top metric cards                                          */
    /* ---------------------------------------------------------- */

    setText(
      'littleLawPeakArrival',
      Number.isFinite(peakArrival)
        ? fmt(peakArrival, 2)
        : '—'
    );


    setText(
      'littleLawPeakResponse',
      Number.isFinite(peakResponse)
        ? fmt(peakResponse, 4)
        : '—'
    );


    setText(
      'littleLawPeakOccupancy',
      Number.isFinite(peakL)
        ? fmt(peakL, 2)
        : '—'
    );


    setText(
      'littleLawHighestUsers',
      Number.isFinite(highestUsers)
        ? fmtInt(highestUsers)
        : '—'
    );


    setText(
      'littleLawLambda',
      Number.isFinite(peakArrival)
        ? fmt(peakArrival, 2)
        : '—'
    );


    setText(
      'littleLawW',
      Number.isFinite(peakResponse)
        ? fmt(peakResponse, 4)
        : '—'
    );


    setText(
      'littleLawL',
      Number.isFinite(peakL)
        ? fmt(peakL, 2)
        : '—'
    );


    setText(
      'littleLawUsers',
      Number.isFinite(highestUsers)
        ? fmtInt(highestUsers)
        : '—'
    );


    setText(
      'littleLawMaxL',
      Number.isFinite(peakL)
        ? fmt(peakL, 2)
        : '—'
    );


    /* ---------------------------------------------------------- */
    /* Overall status                                             */
    /* ---------------------------------------------------------- */

    let overallClassification =
      normalizedRows[normalizedRows.length - 1]
        ?.classification || 'Unknown';


    let overallVariant = 'neutral';

    const classificationLower =
      String(overallClassification).toLowerCase();


    if (
      classificationLower.includes('very light')
      || classificationLower.includes('light')
    ) {

      overallVariant = 'success';

    } else if (
      classificationLower.includes('moderate')
    ) {

      overallVariant = 'warning';

    } else if (
      classificationLower.includes('heavy')
      || classificationLower.includes('critical')
    ) {

      overallVariant = 'danger';

    }


    setBadge(
      'littleLawOverallStatus',
      overallClassification,
      overallVariant
    );


    setBadge(
      'littleLawTableStatus',
      `${normalizedRows.length} load levels`,
      'neutral'
    );


    /* ---------------------------------------------------------- */
    /* Overall trends                                            */
    /* ---------------------------------------------------------- */

    const lastRow =
      normalizedRows[normalizedRows.length - 1];


    const occupancyTrend =
      lastRow?.occupancyTrend;


    const responseTrend =
      lastRow?.responseTimeTrend;


    const arrivalTrend =
      lastRow?.arrivalRateTrend;


    /* ---------------------------------------------------------- */
    /* Occupancy trend                                            */
    /* ---------------------------------------------------------- */

    setBadge(
      'littleLawOccupancyTrend',
      occupancyTrend
        ? occupancyTrend
        : 'No trend',
      occupancyTrend === 'increasing'
        ? 'warning'
        : occupancyTrend === 'decreasing'
          ? 'success'
          : 'neutral'
    );


    let occupancyDescription =
      'No occupancy trend was reported.';


    if (occupancyTrend === 'increasing') {

      occupancyDescription =
        'System occupancy is increasing as workload rises. More requests are remaining in the system at higher concurrency levels.';

    } else if (occupancyTrend === 'decreasing') {

      occupancyDescription =
        'System occupancy is decreasing at the latest measured load level.';

    } else if (occupancyTrend) {

      occupancyDescription =
        `The latest measured occupancy trend is ${occupancyTrend}.`;

    }


    setText(
      'littleLawOccupancyDescription',
      occupancyDescription
    );


    /* ---------------------------------------------------------- */
    /* Response-time trend                                        */
    /* ---------------------------------------------------------- */

    setBadge(
      'littleLawResponseTrend',
      responseTrend
        ? responseTrend
        : 'No trend',
      responseTrend === 'increasing'
        ? 'warning'
        : responseTrend === 'decreasing'
          ? 'success'
          : 'neutral'
    );


    let responseDescription =
      'No response-time trend was reported.';


    if (responseTrend === 'increasing') {

      responseDescription =
        'Response time is increasing as workload grows, indicating growing latency under higher concurrency.';

    } else if (responseTrend === 'decreasing') {

      responseDescription =
        'Response time decreased at the latest measured load level.';

    } else if (responseTrend) {

      responseDescription =
        `The latest measured response-time trend is ${responseTrend}.`;

    }


    setText(
      'littleLawResponseDescription',
      responseDescription
    );


    /* ---------------------------------------------------------- */
    /* Arrival-rate trend                                         */
    /* ---------------------------------------------------------- */

    setBadge(
      'littleLawArrivalTrend',
      arrivalTrend
        ? arrivalTrend
        : 'No trend',
      arrivalTrend === 'increasing'
        ? 'success'
        : arrivalTrend === 'decreasing'
          ? 'warning'
          : 'neutral'
    );


    let arrivalDescription =
      'No arrival-rate trend was reported.';


    if (arrivalTrend === 'increasing') {

      arrivalDescription =
        'Throughput continues to increase as concurrency grows.';

    } else if (arrivalTrend === 'decreasing') {

      arrivalDescription =
        'Throughput decreased at the latest measured load level, which may indicate saturation or increasing contention.';

    } else if (arrivalTrend) {

      arrivalDescription =
        `The latest measured arrival-rate trend is ${arrivalTrend}.`;

    }


    setText(
      'littleLawArrivalDescription',
      arrivalDescription
    );


    /* ---------------------------------------------------------- */
    /* Table                                                     */
    /* ---------------------------------------------------------- */

    const body =
      qs('#littlesLawTableBody');


    if (body) {

      body.innerHTML =
        normalizedRows
          .map((row) => {

            const occupancyTrendLabel =
              row.occupancyTrend || '—';


            let occupancyVariant =
              'neutral';


            if (
              occupancyTrendLabel === 'increasing'
            ) {

              occupancyVariant = 'warning';

            } else if (
              occupancyTrendLabel === 'decreasing'
            ) {

              occupancyVariant = 'success';

            }


            let classificationVariant =
              'neutral';


            const cls =
              String(row.classification).toLowerCase();


            if (
              cls.includes('very light')
              || cls.includes('light')
            ) {

              classificationVariant = 'success';

            } else if (
              cls.includes('moderate')
            ) {

              classificationVariant = 'warning';

            } else if (
              cls.includes('heavy')
              || cls.includes('critical')
            ) {

              classificationVariant = 'danger';

            }


            return `
              <tr>

                <td class="mono">
                  ${
                    Number.isFinite(row.users)
                      ? fmtInt(row.users)
                      : '—'
                  }
                </td>


                <td class="mono">
                  ${
                    Number.isFinite(row.arrivalRate)
                      ? `${fmt(row.arrivalRate, 2)}`
                      : '—'
                  }
                  <span class="table-unit">
                    req/s
                  </span>
                </td>


                <td class="mono">
                  ${
                    Number.isFinite(row.responseTime)
                      ? `${fmt(row.responseTime, 4)}`
                      : '—'
                  }
                  <span class="table-unit">
                    s
                  </span>
                </td>


                <td class="mono">

                  <strong>
                    ${
                      Number.isFinite(row.requestsInSystem)
                        ? fmt(row.requestsInSystem, 2)
                        : '—'
                    }
                  </strong>

                  <span class="table-unit">
                    requests
                  </span>

                </td>


                <td>

                  <span class="badge badge--${classificationVariant}">
                    ${escapeHtml(row.classification)}
                  </span>

                </td>


                <td>

                  <span class="badge badge--${occupancyVariant}">
                    ${escapeHtml(occupancyTrendLabel)}
                  </span>

                </td>

              </tr>
            `;

          })
          .join('');

    }


    /* ---------------------------------------------------------- */
    /* Interpretation                                            */
    /* ---------------------------------------------------------- */

    const peakRow =
      normalizedRows.reduce(
        (best, row) => {

          if (!best) {
            return row;
          }

          if (
            Number.isFinite(row.requestsInSystem)
            && Number.isFinite(best.requestsInSystem)
            && row.requestsInSystem > best.requestsInSystem
          ) {

            return row;

          }

          return best;

        },
        null
      );


    if (peakRow) {

      setText(
        'littleLawLInterpretation',

        Number.isFinite(peakRow.requestsInSystem)
          ? `At the highest observed occupancy, approximately ${fmt(
              peakRow.requestsInSystem,
              2
            )} requests were simultaneously present in the system.`
          : 'Requests-in-system information is unavailable.'
      );


      setText(
        'littleLawLambdaInterpretation',

        Number.isFinite(peakRow.arrivalRate)
          ? `The corresponding arrival rate was ${fmt(
              peakRow.arrivalRate,
              2
            )} requests per second.`
          : 'Arrival-rate information is unavailable.'
      );


      setText(
        'littleLawWInterpretation',

        Number.isFinite(peakRow.responseTime)
          ? `The corresponding average response time was ${fmt(
              peakRow.responseTime,
              4
            )} seconds.`
          : 'Response-time information is unavailable.'
      );

    }


    /* ---------------------------------------------------------- */
    /* Signals                                                   */
    /* ---------------------------------------------------------- */

    const signals =
      littleLaw.signals || {};


    const highOccupancy =
      signals.high_occupancy;


    const criticalOccupancy =
      signals.critical_occupancy;


    setText(
      'littleLawHighOccupancy',
      highOccupancy === true
        ? 'Yes'
        : highOccupancy === false
          ? 'No'
          : '—'
    );


    setText(
      'littleLawCriticalOccupancy',
      criticalOccupancy === true
        ? 'Yes'
        : criticalOccupancy === false
          ? 'No'
          : '—'
    );


    /* ---------------------------------------------------------- */
    /* Chart                                                     */
    /* ---------------------------------------------------------- */

    const chartRows =
      normalizedRows.filter(
        (row) =>
          Number.isFinite(row.users)
          && Number.isFinite(row.requestsInSystem)
      );


    if (!chartRows.length) {
      return;
    }


    const labels =
      chartRows.map(
        (row) => fmtInt(row.users)
      );


    const occupancyData =
      chartRows.map(
        (row) => row.requestsInSystem
      );


    makeOrUpdateChart(
      'littlesLaw',
      'littlesLawChart',
      {

        type: 'line',

        data: {

          labels,

          datasets: [

            Object.assign(
              measuredDataset(
                'L — requests in system',
                occupancyData
              ),
              {
                pointRadius: 4,
                pointHoverRadius: 6,
                tension: 0.25,
                spanGaps: false,
              }
            ),

          ],

        },


        options:
          baseChartOptions({

            plugins: {

              legend: {
                display: true,

                labels: {
                  color: cssVar('--ink-muted'),

                  font: {
                    family: 'Inter',
                    size: 11.5,
                  },
                },

              },

              tooltip:
                baseChartOptions().plugins.tooltip,

            },


            scales: {

              x: Object.assign(
                {},
                baseChartOptions().scales.x,
                {

                  title: {
                    display: true,
                    text: 'Concurrent users',
                    color: cssVar('--ink-muted'),
                  },

                }
              ),


              y: Object.assign(
                {},
                baseChartOptions().scales.y,
                {

                  title: {
                    display: true,
                    text: 'Requests in system (L)',
                    color: cssVar('--ink-muted'),
                  },

                }
              ),

            },

          }),

      }
    );

  }

  /* ---- Queueing Theory ---------------------------------------- */

  function renderQueueing(queueing) {
    if (!queueing) return;

    const rows = Array.isArray(queueing.levels)
      ? queueing.levels
      : [];

    const predicted = Array.isArray(queueing.predicted_levels)
      ? queueing.predicted_levels
      : [];

    /* ----------------------------------------------------------
      Service / model information
    ---------------------------------------------------------- */

    setText(
      'queue-model',
      queueing.queueing_model || '—'
    );

    setText(
      'queue-service-rate',
      Number.isFinite(Number(queueing.service_rate))
        ? fmt(queueing.service_rate, 2) + ' req/s'
        : '—'
    );

    setText(
      'queue-service-time',
      Number.isFinite(Number(queueing.service_time))
        ? fmt(queueing.service_time, 4) + ' s'
        : '—'
    );

    setText(
      'queue-server-count',
      queueing.num_servers != null
        ? String(queueing.num_servers)
        : '—'
    );

    setText(
      'queue-service-source',
      queueing.service_rate_source || '—'
    );


    /* ----------------------------------------------------------
      Normalize measured rows
    ---------------------------------------------------------- */

    const normalizedRows = rows.map((r, index) => {

      const metadata = r && r.metadata
        ? r.metadata
        : {};

      const users =
        r.users ??
        r.load_users ??
        r.concurrent_users ??
        metadata.users ??
        metadata.load_users ??
        null;

      const arrivalRate =
        r.arrival_rate ??
        r.lambda ??
        r.throughput ??
        null;

      const utilization =
        r.utilization ??
        r.rho ??
        null;

      const queueLength =
        r.queue_length ??
        r.Lq ??
        null;

      const waitingTime =
        r.waiting_time ??
        r.wait_time ??
        r.Wq ??
        null;

      const systemTime =
        r.system_time ??
        r.response_time ??
        r.W ??
        null;

      const stability =
        r.stability ??
        r.queue_stability ??
        '—';

      const congestionRisk =
        r.congestion_risk ??
        '—';

      const queueStatus =
        r.queue_status ??
        '—';

      const signals =
        r.signals ||
        {};

      return {
        users,
        arrivalRate,
        utilization,
        queueLength,
        waitingTime,
        systemTime,
        stability,
        congestionRisk,
        queueStatus,
        signals
      };
    });


    /* ----------------------------------------------------------
      Main measured table
    ---------------------------------------------------------- */

    const body = qs('#queueingTableBody');

    if (!normalizedRows.length) {

      body.innerHTML = `
        <tr>
          <td colspan="8" class="data-table__empty">
            No queueing data available.
          </td>
        </tr>
      `;

    } else {

      body.innerHTML = normalizedRows.map((r) => {

        const rho = Number(r.utilization);

        const stabilityClass =
          r.stability === 'Unstable'
            ? 'status-badge status-badge--danger'
            : r.stability === 'Near Saturation'
              ? 'status-badge status-badge--warning'
              : 'status-badge';

        const congestionClass =
          r.congestionRisk === 'Critical'
            ? 'status-badge status-badge--danger'
            : r.congestionRisk === 'High'
              ? 'status-badge status-badge--warning'
              : 'status-badge';

        return `
          <tr>

            <td class="mono">
              ${fmtInt(r.users)}
            </td>

            <td class="mono">
              ${fmt(r.arrivalRate, 2)}
            </td>

            <td class="mono">
              ${Number.isFinite(rho)
                ? fmt(rho, 4)
                : '—'}
            </td>

            <td class="mono">
              ${Number.isFinite(Number(r.queueLength))
                ? fmt(r.queueLength, 2)
                : '∞'}
            </td>

            <td class="mono">
              ${Number.isFinite(Number(r.waitingTime))
                ? fmt(r.waitingTime, 4) + ' s'
                : '∞'}
            </td>

            <td class="mono">
              ${Number.isFinite(Number(r.systemTime))
                ? fmt(r.systemTime, 4) + ' s'
                : '∞'}
            </td>

            <td>
              <span class="${stabilityClass}">
                ${escapeHtml(r.stability)}
              </span>
            </td>

            <td>
              <span class="${congestionClass}">
                ${escapeHtml(r.congestionRisk)}
              </span>
            </td>

          </tr>
        `;

      }).join('');
    }


    /* ----------------------------------------------------------
      Predicted queueing table
    ---------------------------------------------------------- */

    const predictedBody =
      qs('#queueingPredictedTableBody');

    if (!predicted.length) {

      predictedBody.innerHTML = `
        <tr>
          <td colspan="4" class="data-table__empty">
            No predictions available.
          </td>
        </tr>
      `;

    } else {

      predictedBody.innerHTML = predicted.map((p) => {

        const users =
          p.predicted_users ??
          p.users ??
          p.load_users ??
          null;

        const lambda =
          p.arrival_rate ??
          p.lambda ??
          p.throughput ??
          null;

        const rho =
          p.utilization ??
          p.rho ??
          null;

        const stability =
          p.stability ??
          '—';

        const stabilityClass =
          stability === 'Unstable'
            ? 'status-badge status-badge--danger'
            : stability === 'Near Saturation'
              ? 'status-badge status-badge--warning'
              : 'status-badge';

        return `
          <tr>

            <td class="mono">
              ${fmtInt(users)}
            </td>

            <td class="mono">
              ${fmt(lambda, 2)}
            </td>

            <td class="mono">
              ${fmt(rho, 4)}
            </td>

            <td>
              <span class="${stabilityClass}">
                ${escapeHtml(stability)}
              </span>
            </td>

          </tr>
        `;

      }).join('');
    }


    /* ----------------------------------------------------------
      Summary metrics
    ---------------------------------------------------------- */

    const validUtilizations =
      normalizedRows
        .map(r => Number(r.utilization))
        .filter(Number.isFinite);

    const validQueues =
      normalizedRows
        .map(r => Number(r.queueLength))
        .filter(Number.isFinite);

    const validWaits =
      normalizedRows
        .map(r => Number(r.waitingTime))
        .filter(Number.isFinite);

    const validSystemTimes =
      normalizedRows
        .map(r => Number(r.systemTime))
        .filter(Number.isFinite);


    const peakUtilization =
      validUtilizations.length
        ? Math.max(...validUtilizations)
        : null;

    const maxQueue =
      validQueues.length
        ? Math.max(...validQueues)
        : null;

    const maxWait =
      validWaits.length
        ? Math.max(...validWaits)
        : null;

    const maxSystemTime =
      validSystemTimes.length
        ? Math.max(...validSystemTimes)
        : null;


    setText(
      'queue-peak-utilization',
      peakUtilization != null
        ? fmt(peakUtilization, 4)
        : '—'
    );

    setText(
      'queue-max-length',
      maxQueue != null
        ? fmt(maxQueue, 2) + ' requests'
        : '—'
    );

    setText(
      'queue-max-wait',
      maxWait != null
        ? fmt(maxWait, 4) + ' s'
        : '—'
    );

    setText(
      'queue-max-system-time',
      maxSystemTime != null
        ? fmt(maxSystemTime, 4) + ' s'
        : '—'
    );


    /* ----------------------------------------------------------
      Current/latest queue state
    ---------------------------------------------------------- */

    const latest =
      normalizedRows.length
        ? normalizedRows[normalizedRows.length - 1]
        : null;

    if (latest) {

      setText(
        'queue-current-status',
        latest.queueStatus !== '—'
          ? latest.queueStatus
          : latest.stability
      );

      setText(
        'queue-congestion-risk',
        latest.congestionRisk
      );

      const latestIdle =
        queueing.idle_probability ??
        latest.idle_probability ??
        null;

      setText(
        'queue-idle-probability',
        latestIdle != null
          ? fmt(latestIdle, 4)
          : '—'
      );

      const latestOfferedLoad =
        queueing.offered_load ??
        latest.offered_load ??
        null;

      setText(
        'queue-offered-load',
        latestOfferedLoad != null
          ? fmt(latestOfferedLoad, 4) + ' Erlangs'
          : '—'
      );
    }


    /* ----------------------------------------------------------
      Signals
    ---------------------------------------------------------- */

    const latestSignals =
      latest && latest.signals
        ? latest.signals
        : {};

    setText(
      'queue-signal-utilization',
      latestSignals.utilization_high
        ? 'Yes'
        : 'No'
    );

    setText(
      'queue-signal-saturation',
      latestSignals.near_saturation
        ? 'Yes'
        : 'No'
    );

    setText(
      'queue-signal-unstable',
      latestSignals.unstable_system
        ? 'Yes'
        : 'No'
    );

    setText(
      'queue-signal-wait',
      latestSignals.high_wait_time
        ? 'Yes'
        : 'No'
    );

    setText(
      'queue-signal-queue',
      latestSignals.long_queue
        ? 'Yes'
        : 'No'
    );

    setText(
      'queue-signal-congestion',
      latestSignals.congestion_detected
        ? 'Yes'
        : 'No'
    );


    /* ----------------------------------------------------------
      Utilization chart
    ---------------------------------------------------------- */

    const measuredLabels =
      normalizedRows.map((r) =>
        fmtInt(r.users)
      );

    const predictedLabels =
      predicted.map((p) =>
        fmtInt(
          p.predicted_users ??
          p.users ??
          p.load_users
        )
      );

    const allLabels =
      measuredLabels.concat(predictedLabels);


    const measuredData =
      normalizedRows
        .map(r => Number.isFinite(Number(r.utilization))
          ? Number(r.utilization)
          : null)
        .concat(
          predicted.map(() => null)
        );


    const predictedData =
      measuredLabels
        .map(() => null)
        .concat(
          predicted.map((p) => {

            const value =
              p.utilization ??
              p.rho ??
              null;

            return Number.isFinite(Number(value))
              ? Number(value)
              : null;
          })
        );


    const chartValues =
      measuredData
        .concat(predictedData)
        .filter((v) => Number.isFinite(v));


    const chartMax =
      chartValues.length
        ? Math.max(1.2, ...chartValues)
        : 1.2;


    makeOrUpdateChart(
      'utilization',
      'utilizationChart',
      {
        type: 'line',

        data: {
          labels: allLabels,

          datasets: [
            measuredDataset(
              'ρ (measured)',
              measuredData
            ),

            predictedDataset(
              'ρ (predicted)',
              predictedData
            )
          ]
        },

        options: baseChartOptions({
          scales: Object.assign(
            {},
            baseChartOptions().scales,
            {
              y: Object.assign(
                {},
                baseChartOptions().scales.y,
                {
                  min: 0,
                  max: chartMax
                }
              )
            }
          )
        })
      }
    );
  }

  /* ---- Bottleneck --------------------------------------------- */
  
  function renderBottleneck(bottleneck) {
  
    const resources = ['cpu', 'disk', 'network'];
  
    const resetResource = (resource) => {
    
      const bar = qs(`#bar-${resource}`);
      const value = qs(`#val-${resource}`);
      const demand = qs(`#demand-${resource}`);
      const approx = qs(`#approx-${resource}`);
    
      if (bar) bar.style.width = '0%';
      if (value) value.textContent = '—';
      if (demand) demand.textContent = '—';
      if (approx) approx.textContent = '—';
    };
  
  
    /* ----------------------------------------------------------
      Reset / unavailable state
    ---------------------------------------------------------- */
  
    if (!bottleneck) {
    
      setBadge(
        'bottleneckPrimaryBadge',
        'Unavailable',
        'neutral'
      );
    
      setText(
        'bottleneckPrimaryResource',
        '—'
      );
    
      setText(
        'bottleneckPrimaryDemand',
        '—'
      );
    
      setText(
        'bottleneckSeverity',
        '—'
      );
    
      setText(
        'bottleneckNotes',
        'Bottleneck analysis did not run.'
      );
    
      resources.forEach(resetResource);
    
      return;
    }
  
  
    if (bottleneck.bottleneck_analysis === 'unavailable') {
    
      setBadge(
        'bottleneckPrimaryBadge',
        'Unavailable',
        'neutral'
      );
    
      setText(
        'bottleneckPrimaryResource',
        'Unavailable'
      );
    
      setText(
        'bottleneckPrimaryDemand',
        '—'
      );
    
      setText(
        'bottleneckSeverity',
        '—'
      );
    
      const notesEl = qs('#bottleneckNotes');
    
      if (notesEl) {
        notesEl.innerHTML = `
          <p class="empty-note">
            ${escapeHtml(
              bottleneck.reason ||
              'No monitored resource signals were available.'
            )}
          </p>
        `;
      }
    
      resources.forEach(resetResource);
    
      return;
    }
  
  
    /* ----------------------------------------------------------
      Primary bottleneck
    ---------------------------------------------------------- */
  
    const primary =
      bottleneck.bottleneck || {};
  
    const primaryResource =
      primary.resource || '—';
  
    const primaryDemand =
      Number(primary.service_demand);
  
  
    setBadge(
      'bottleneckPrimaryBadge',
      primaryResource !== '—'
        ? primaryResource.toUpperCase()
        : '—',
      'accent'
    );
  
    setText(
      'bottleneckPrimaryResource',
      primaryResource !== '—'
        ? primaryResource.toUpperCase()
        : '—'
    );
  
    setText(
      'bottleneckPrimaryDemand',
      Number.isFinite(primaryDemand)
        ? fmt(primaryDemand, 6) + ' s/request'
        : '—'
    );
  
    setText(
      'bottleneckSeverity',
      bottleneck.bottleneck_severity || '—'
    );
  
  
    /* ----------------------------------------------------------
      Resource bars
    ---------------------------------------------------------- */
  
    const resourceData =
      bottleneck.resources || {};
  
  
    resources.forEach((key) => {
    
      const resource =
        resourceData[key];
    
      const bar =
        qs(`#bar-${key}`);
    
      const value =
        qs(`#val-${key}`);
    
      const demand =
        qs(`#demand-${key}`);
    
      const approx =
        qs(`#approx-${key}`);
    
      const wrapper =
        bar
          ? bar.closest('.resource-bar')
          : null;
    
    
      if (!resource) {
      
        if (bar) bar.style.width = '0%';
        if (value) value.textContent = 'n/a';
        if (demand) demand.textContent = 'n/a';
        if (approx) approx.textContent = 'Not monitored';
      
        return;
      }
    
    
      const utilization =
        Number(resource.utilization);
    
      const rawUtilization =
        Number(resource.raw_utilization);
    
      const serviceDemand =
        Number(resource.service_demand);
    
    
      /*
      * Use raw utilization when available so a resource that
      * exceeds configured capacity can visually indicate >100%.
      * The visual bar itself remains capped at 100%.
      */
    
      const displayUtilization =
        Number.isFinite(rawUtilization)
          ? rawUtilization
          : utilization;
    
    
      const percentage =
        Number.isFinite(displayUtilization)
          ? Math.max(
              0,
              Math.min(
                100,
                displayUtilization * 100
              )
            )
          : 0;
          
          
      if (bar) {
        bar.style.width = `${percentage}%`;
      }
    
    
      if (value) {
      
        value.textContent =
          Number.isFinite(displayUtilization)
            ? fmtPercent(
                displayUtilization * 100,
                1
              )
            : '—';
      }
    
    
      if (demand) {
      
        demand.textContent =
          Number.isFinite(serviceDemand)
            ? fmt(serviceDemand, 6) +
              ' s/request'
            : '—';
      }
    
    
      if (approx) {
      
        approx.textContent =
          resource.is_approximate
            ? 'Approximate'
            : 'Measured';
      }
    
    
      if (wrapper) {
      
        wrapper.dataset.critical =
          resource.exceeds_configured_capacity
            ? 'true'
            : 'false';
      
        wrapper.dataset.bottleneck =
          key === primaryResource
            ? 'true'
            : 'false';
      }
    
    });
  
  
    /* ----------------------------------------------------------
      Queue severity proxy
    ---------------------------------------------------------- */
  
    const queueBar =
      qs('#bar-queue');
  
    const severity =
      String(
        bottleneck.bottleneck_severity || ''
      ).toLowerCase();
    
    
    /*
    * The backend does not calculate queue utilization here.
    * Therefore the queue bar remains a visual severity proxy,
    * exactly as in your original implementation.
    */
    
    const severityMap = {
    
      'substantial headroom': 20,
    
      'moderate headroom': 45,
    
      'near ceiling': 75,
    
      'at or beyond theoretical ceiling': 100
    
    };
  
  
    const queuePercentage =
      severityMap[severity] ?? 0;
  
  
    if (queueBar) {
      queueBar.style.width =
        `${queuePercentage}%`;
    }
  
  
    setText(
      'val-queue',
      bottleneck.bottleneck_severity || '—'
    );
  
  
    /* ----------------------------------------------------------
      Asymptotic bounds
    ---------------------------------------------------------- */
  
    const bounds =
      bottleneck.asymptotic_bounds || {};
  
    const observed =
      bottleneck.observed_vs_bound || {};
  
  
    setText(
      'bottleneckObservedThroughput',
      Number.isFinite(
        Number(observed.observed_throughput)
      )
        ? fmt(
            observed.observed_throughput,
            2
          )
        : Number.isFinite(
            Number(bottleneck.throughput)
          )
          ? fmt(
              bottleneck.throughput,
              2
            )
          : '—'
    );
  
  
    setText(
      'bottleneckResourceBound',
      Number.isFinite(
        Number(bounds.bottleneck_throughput_bound)
      )
        ? fmt(
            bounds.bottleneck_throughput_bound,
            2
          )
        : '∞'
    );
  
  
    setText(
      'bottleneckConcurrencyBound',
      Number.isFinite(
        Number(bounds.concurrency_throughput_bound)
      )
        ? fmt(
            bounds.concurrency_throughput_bound,
            2
          )
        : '∞'
    );
  
  
    setText(
      'bottleneckAsymptoticBound',
      Number.isFinite(
        Number(bounds.asymptotic_throughput_bound)
      )
        ? fmt(
            bounds.asymptotic_throughput_bound,
            2
          )
        : '∞'
    );
  
  
    /* ----------------------------------------------------------
      Observed vs bound
    ---------------------------------------------------------- */
  
    const ratio =
      Number(observed.ratio_of_bound);
  
  
    setText(
      'bottleneckBoundRatio',
      Number.isFinite(ratio)
        ? fmtPercent(
            ratio * 100,
            1
          )
        : '—'
    );
  
  
    setText(
      'bottleneckBindingConstraint',
      bounds.binding_constraint
        ? formatBottleneckConstraint(
            bounds.binding_constraint
          )
        : '—'
    );
  
  
    setText(
      'bottleneckOptimalConcurrency',
      Number.isFinite(
        Number(
          bounds.optimal_concurrency_n_star
        )
      )
        ? fmt(
            bounds.optimal_concurrency_n_star,
            2
          ) + ' users'
        : '∞'
    );
  
  
    setText(
      'bottleneckTotalDemand',
      Number.isFinite(
        Number(
          bottleneck.total_service_demand
        )
      )
        ? fmt(
            bottleneck.total_service_demand,
            6
          ) + ' s/request'
        : '—'
    );
  
  
    /* ----------------------------------------------------------
      Bound status badge
    ---------------------------------------------------------- */
  
    const boundBadge =
      qs('#bottleneckBoundBadge');
  
  
    if (boundBadge) {
    
      if (observed.exceeds_bound) {
      
        setBadge(
          'bottleneckBoundBadge',
          'Exceeds theoretical bound',
          'danger'
        );
      
      } else if (
        Number.isFinite(ratio) &&
        ratio >= 0.85
      ) {
      
        setBadge(
          'bottleneckBoundBadge',
          'Near ceiling',
          'warning'
        );
      
      } else {
      
        setBadge(
          'bottleneckBoundBadge',
          'Within theoretical bound',
          'neutral'
        );
      }
    }
  
  
    /* ----------------------------------------------------------
      Signals
    ---------------------------------------------------------- */
  
    const signals =
      bottleneck.signals || {};
  
  
    setText(
      'signalDominant',
      signals.clear_dominant_bottleneck
        ? 'Yes'
        : 'No'
    );
  
    setText(
      'signalNearCeiling',
      signals.near_theoretical_ceiling
        ? 'Yes'
        : 'No'
    );
  
    setText(
      'signalExceedsCeiling',
      signals.exceeds_theoretical_ceiling
        ? 'Yes'
        : 'No'
    );
  
    setText(
      'signalCapacityExceeded',
      signals.resource_exceeds_configured_capacity
        ? 'Yes'
        : 'No'
    );
  
  
    /* ----------------------------------------------------------
      Notes / interpretation
    ---------------------------------------------------------- */
  
    const notesEl =
      qs('#bottleneckNotes');
  
    if (!notesEl) return;
  
  
    const notes = [];
  
  
    if (primary.note) {
      notes.push(primary.note);
    }
  
  
    if (bottleneck.bottleneck_severity) {
    
      notes.push(
        `Current throughput classification: ${
          bottleneck.bottleneck_severity
        }.`
      );
    }
  
  
    if (Number.isFinite(ratio)) {
    
      notes.push(
        `Observed throughput is ${
          fmtPercent(ratio * 100, 1)
        } of the derived asymptotic throughput bound.`
      );
    }
  
  
    if (
      bounds.binding_constraint ===
      'bottleneck_resource'
    ) {
    
      notes.push(
        'The monitored bottleneck resource is the binding throughput constraint.'
      );
    
    } else if (
      bounds.binding_constraint ===
      'concurrency'
    ) {
    
      notes.push(
        'Concurrency is currently the binding constraint before the bottleneck resource ceiling is reached.'
      );
    }
  
  
    if (
      signals.resource_exceeds_configured_capacity
    ) {
    
      notes.push(
        'At least one monitored resource exceeds its configured capacity.'
      );
    }
  
  
    notesEl.innerHTML =
      notes.length
        ? notes
            .map(
              note =>
                `<p>${escapeHtml(note)}</p>`
            )
            .join('')
        : `
            <p class="empty-note">
              No additional interpretation available.
            </p>
          `;
  }
  
  
  /* ------------------------------------------------------------
    Helper
  ------------------------------------------------------------ */
  
  function formatBottleneckConstraint(value) {
  
    if (!value) return '—';
  
    const map = {
    
      bottleneck_resource:
        'Bottleneck resource',
    
      concurrency:
        'Concurrency'
    
    };
  
    return map[value] || value;
  }

  /* ---- Capacity Planning ------------------------------------- */
  
  function renderCapacity(capacity) {
    const results = capacity && capacity.results;
  
    if (!results) {
      resetCapacityView();
      return;
    }
  
    const calculations = capacity.calculations || {};
    const health = capacity.health_summary || {};
    const signals = capacity.signals || {};
    const runtime = capacity.inputs && capacity.inputs.runtime
      ? capacity.inputs.runtime
      : {};
  
    const usl = capacity.inputs && capacity.inputs.usl
      ? capacity.inputs.usl
      : {};
  
    const littleLaw = capacity.inputs && capacity.inputs.little_law
      ? capacity.inputs.little_law
      : {};
  
    const queueing = capacity.inputs && capacity.inputs.queueing
      ? capacity.inputs.queueing
      : {};
  
    const bottleneck = capacity.inputs && capacity.inputs.bottleneck
      ? capacity.inputs.bottleneck
      : null;
  
  
    /* ==========================================================
      BASIC HELPERS
      ========================================================== */
  
    const safeNumber = (value, fallback = null) => {
      const n = Number(value);
      return Number.isFinite(n) ? n : fallback;
    };
  
    const formatUsers = (value) => {
      const n = safeNumber(value);
      return n === null ? '—' : fmtInt(n);
    };
  
    const formatReq = (value) => {
      const n = safeNumber(value);
      return n === null ? '—' : `${fmt(n, 2)} req/s`;
    };
  
    const formatBoolean = (value) => {
      return value ? 'Yes' : 'No';
    };
  
    const pressureToPercent = (pressure) => {
      switch (String(pressure || '').toLowerCase()) {
        case 'low':
        case 'normal':
          return 25;
      
        case 'moderate':
          return 55;
      
        case 'high':
          return 80;
      
        case 'critical':
          return 100;
      
        default:
          return 0;
      }
    };
  
  
    /* ==========================================================
      OVERALL HEALTH
      ========================================================== */
  
    const overallStatus =
      health.overall_status ||
      results.capacity_classification ||
      'Unknown';
  
    setText(
      'capacity-overall-status',
      overallStatus
    );
  
    setBadge(
      'capacity-health-badge',
      overallStatus,
      capacityStatusBadgeType(overallStatus)
    );
  
    setText(
      'capacity-main-risk',
      health.main_risk || 'None'
    );
  
    setText(
      'capacity-scaling-recommendation',
      formatScalingRecommendation(
        health.recommended_scaling
      )
    );
  
    setText(
      'capacity-prediction-confidence',
      capitalizeStatus(
        results.prediction_confidence || 'Unknown'
      )
    );
  
    setText(
      'capacity-health-description',
      buildCapacityHealthDescription(
        results,
        health,
        signals
      )
    );
  
  
    /* ==========================================================
      CAPACITY DIAL
      ========================================================== */
  
    const utilPct = clampPercent(
      results.user_capacity_used_percent ??
      results.throughput_capacity_used_percent ??
      0
    );
  
    setText(
      'capacity-utilization-value',
      fmtPercent(utilPct, 0)
    );
  
    const arc = qs('#capacityDialArc');
  
    if (arc) {
      const arcLength = (utilPct / 100) * 283;
    
      arc.setAttribute(
        'stroke-dasharray',
        `${arcLength} 283`
      );
    
      arc.setAttribute(
        'stroke',
        utilPct >= 100
          ? cssVar('--danger')
          : utilPct >= 90
            ? cssVar('--warning')
            : utilPct >= 70
              ? cssVar('--amber')
              : cssVar('--success')
      );
    }
  
  
    /* ==========================================================
      PRIMARY CAPACITY VALUES
      ========================================================== */
  
    animateNumber(
      'capacity-safe-users',
      safeNumber(results.safe_users, 0),
      0
    );
  
    animateNumber(
      'capacity-breaking-point',
      safeNumber(results.breaking_point, 0),
      0
    );
  
    animateNumber(
      'capacity-remaining-users',
      safeNumber(results.growth_potential_users, 0),
      0
    );
  
    setText(
      'capacity-margin',
      fmtPercent(
        results.capacity_margin,
        1
      )
    );
  
  
    /* ==========================================================
      CAPACITY REFERENCE
      ========================================================== */
  
    setText(
      'capacity-reference-users',
      formatUsers(results.capacity_reference_users)
    );
  
    setText(
      'capacity-current-users',
      formatUsers(results.current_users)
    );
  
    setText(
      'capacity-tested-range',
      results.tested_user_range || '—'
    );
  
    setText(
      'capacity-reference-reason',
      results.capacity_reference_reason || '—'
    );
  
    setBadge(
      'capacity-reference-badge',
      results.extrapolation_reliable
        ? 'USL reference'
        : 'Observed reference',
      results.extrapolation_reliable
        ? 'accent'
        : 'warning'
    );
  
  
    /* ==========================================================
      CAPACITY UTILIZATION
      ========================================================== */
  
    setText(
      'capacity-throughput-util',
      fmtPercent(
        results.throughput_capacity_used_percent,
        1
      )
    );
  
    setText(
      'capacity-user-util',
      fmtPercent(
        results.user_capacity_used_percent,
        1
      )
    );
  
    setText(
      'capacity-usl-user-util',
      fmtPercent(
        results.usl_user_capacity_used_percent,
        1
      )
    );
  
    setText(
      'capacity-request-pressure',
      fmt(
        results.request_pressure,
        2
      )
    );
  
    setBadge(
      'capacity-utilization-status',
      capacityUtilizationLabel(
        results.throughput_capacity_used_percent
      ),
      capacityUtilizationBadgeType(
        results.throughput_capacity_used_percent
      )
    );
  
  
    /* ==========================================================
      USL LIMITS
      ========================================================== */
  
    setText(
      'capacity-usl-optimal',
      formatUsers(results.usl_optimal_users)
    );
  
    setText(
      'capacity-usl-saturation',
      formatUsers(results.usl_saturation_point)
    );
  
    setText(
      'capacity-usl-peak',
      formatReq(results.usl_peak_throughput)
    );
  
    setText(
      'capacity-scalability-efficiency',
      fmtPercent(
        safeNumber(results.scalability_efficiency, 0) * 100,
        1
      )
    );
  
  
    /* ==========================================================
      EXTRAPOLATION WARNING
      ========================================================== */
  
    const warningEl = qs('#capacity-extrapolation-warning');
  
    if (warningEl) {
      const reliable = results.extrapolation_reliable === true;
    
      warningEl.dataset.status = reliable
        ? 'reliable'
        : 'warning';
    }
  
    setText(
      'capacity-extrapolation-message',
      results.extrapolation_reliable
        ? 'USL extrapolation is considered reliable for capacity estimation.'
        : 'USL extrapolation is not considered reliable. Capacity is therefore referenced to the observed maximum tested load.'
    );
  
  
    /* ==========================================================
      QUEUE CAPACITY
      ========================================================== */
  
    const queueUtil = safeNumber(
      results.queue_utilization,
      safeNumber(queueing.utilization, 0)
    );
  
    setText(
      'capacity-queue-util',
      fmtPercent(
        queueUtil * 100,
        1
      )
    );
  
    setText(
      'capacity-queue-length',
      fmt(
        results.queue_length,
        2
      )
    );
  
    setText(
      'capacity-queue-wait',
      fmt(
        results.queue_waiting_time,
        4
      )
    );
  
    setText(
      'capacity-queue-stability',
      results.queue_stability || '—'
    );
  
    setBadge(
      'capacity-queue-status',
      results.queue_stability || 'Unknown',
      queueStatusBadgeType(results.queue_stability)
    );
  
  
    /* ==========================================================
      RESOURCE PRESSURE
      ========================================================== */
  
    const pressure = results.resource_pressure || {};
  
    renderCapacityPressure(
      'cpu',
      pressure.cpu
    );
  
    renderCapacityPressure(
      'memory',
      pressure.memory
    );
  
    renderCapacityPressure(
      'disk',
      pressure.disk
    );
  
    renderCapacityPressure(
      'network',
      pressure.network
    );
  
    renderCapacityPressure(
      'queue',
      pressure.queue
    );
  
  
    /* ==========================================================
      RUNTIME SNAPSHOT
      ========================================================== */
  
    setText(
      'capacity-runtime-users',
      formatUsers(results.current_users)
    );
  
    setText(
      'capacity-runtime-throughput',
      fmt(
        results.current_throughput,
        2
      )
    );
  
    setText(
      'capacity-runtime-response',
      fmt(
        results.current_response_time,
        2
      )
    );
  
    setText(
      'capacity-runtime-cpu',
      fmtPercent(
        results.current_cpu_usage,
        1
      )
    );
  
    setText(
      'capacity-runtime-memory',
      fmtPercent(
        results.current_memory_usage,
        1
      )
    );
  
    setText(
      'capacity-runtime-errors',
      fmtPercent(
        results.current_error_rate,
        2
      )
    );
  
  
    /* ==========================================================
      BOTTLENECK
      ========================================================== */
  
    const bottleneckResource =
      results.bottleneck_resource ||
      (
        bottleneck &&
        bottleneck.bottleneck &&
        bottleneck.bottleneck.resource
      );
    
    const bottleneckDemand =
      results.bottleneck_service_demand ??
      (
        bottleneck &&
        bottleneck.bottleneck &&
        bottleneck.bottleneck.service_demand
      );
    
    const asymptoticBound =
      results.asymptotic_throughput_bound ??
      (
        bottleneck &&
        bottleneck.asymptotic_bounds &&
        bottleneck.asymptotic_bounds.asymptotic_throughput_bound
      );
    
    const optimalConcurrency =
      results.optimal_concurrency_n_star ??
      (
        bottleneck &&
        bottleneck.asymptotic_bounds &&
        bottleneck.asymptotic_bounds.optimal_concurrency_n_star
      );
    
    setText(
      'capacity-bottleneck-resource',
      bottleneckResource
        ? String(bottleneckResource).toUpperCase()
        : '—'
    );
  
    setText(
      'capacity-bottleneck-demand',
      bottleneckDemand === null ||
      bottleneckDemand === undefined
        ? '—'
        : fmt(bottleneckDemand, 6)
    );
  
    setText(
      'capacity-asymptotic-bound',
      asymptoticBound === null ||
      asymptoticBound === undefined
        ? '—'
        : fmt(asymptoticBound, 2)
    );
  
    setText(
      'capacity-optimal-concurrency',
      optimalConcurrency === null ||
      optimalConcurrency === undefined
        ? '—'
        : fmt(optimalConcurrency, 2)
    );
  
    setBadge(
      'capacity-bottleneck-badge',
      results.bottleneck_severity || 'Unknown',
      bottleneckSeverityBadgeType(
        results.bottleneck_severity
      )
    );
  
  
    /* ==========================================================
      LITTLE'S LAW CONSISTENCY
      ========================================================== */
  
    const consistency =
      results.little_law_consistency ||
      calculations.little_law_consistency ||
      {};
  
    setText(
      'capacity-little-law-status',
      consistency.consistent === true
        ? 'Consistent'
        : consistency.consistent === false
          ? 'Inconsistent'
          : '—'
    );
  
    setText(
      'capacity-little-law-difference',
      consistency.difference_percent === undefined
        ? '—'
        : fmtPercent(
            consistency.difference_percent,
            2
          )
    );
  
    setText(
      'capacity-arrival-rate',
      calculations.arrival_rate === undefined
        ? '—'
        : fmt(
            calculations.arrival_rate,
            2
          )
    );
  
  
    /* ==========================================================
      SIGNALS
      ========================================================== */
  
    setCapacitySignal(
      'capacity-signal-available',
      signals.capacity_available
    );
  
    setCapacitySignal(
      'capacity-signal-low',
      signals.capacity_low
    );
  
    setCapacitySignal(
      'capacity-signal-nearly-full',
      signals.capacity_nearly_full
    );
  
    setCapacitySignal(
      'capacity-signal-exceeded',
      signals.capacity_exceeded
    );
  
    setCapacitySignal(
      'capacity-signal-throughput',
      signals.throughput_capacity_exceeded
    );
  
    setCapacitySignal(
      'capacity-signal-safe',
      signals.safe_capacity_exceeded
    );
  
    setCapacitySignal(
      'capacity-signal-horizontal',
      signals.horizontal_scaling_candidate
    );
  
    setCapacitySignal(
      'capacity-signal-vertical',
      signals.vertical_scaling_candidate
    );
  
  
    /* ==========================================================
      INTERPRETATION
      ========================================================== */
  
    renderCapacityInterpretation(
      results,
      health,
      signals
    );
  }
  
  
  /* ==============================================================
    CAPACITY RESET
    ============================================================== */
  
  function resetCapacityView() {
    setText('capacity-utilization-value', '—');
  
    const arc = qs('#capacityDialArc');
  
    if (arc) {
      arc.setAttribute(
        'stroke-dasharray',
        '0 283'
      );
    }
  
    const ids = [
      'capacity-overall-status',
      'capacity-main-risk',
      'capacity-scaling-recommendation',
      'capacity-prediction-confidence',
      'capacity-health-description',
      'capacity-reference-users',
      'capacity-current-users',
      'capacity-tested-range',
      'capacity-reference-reason',
      'capacity-throughput-util',
      'capacity-user-util',
      'capacity-usl-user-util',
      'capacity-request-pressure',
      'capacity-usl-optimal',
      'capacity-usl-saturation',
      'capacity-usl-peak',
      'capacity-scalability-efficiency',
      'capacity-extrapolation-message',
      'capacity-queue-util',
      'capacity-queue-length',
      'capacity-queue-wait',
      'capacity-queue-stability',
      'capacity-runtime-users',
      'capacity-runtime-throughput',
      'capacity-runtime-response',
      'capacity-runtime-cpu',
      'capacity-runtime-memory',
      'capacity-runtime-errors',
      'capacity-bottleneck-resource',
      'capacity-bottleneck-demand',
      'capacity-asymptotic-bound',
      'capacity-optimal-concurrency',
      'capacity-little-law-status',
      'capacity-little-law-difference',
      'capacity-arrival-rate',
      'capacity-safe-users',
      'capacity-breaking-point',
      'capacity-remaining-users',
      'capacity-margin'
    ];
  
    ids.forEach((id) => setText(id, '—'));
  
    [
      'cpu',
      'memory',
      'disk',
      'network',
      'queue'
    ].forEach((resource) => {
      renderCapacityPressure(resource, null);
    });
  
    [
      'available',
      'low',
      'nearly-full',
      'exceeded',
      'throughput',
      'safe',
      'horizontal',
      'vertical'
    ].forEach((name) => {
      setCapacitySignal(
        `capacity-signal-${name}`,
        null
      );
    });
  
    const notes = qs('#capacityInterpretation');
  
    if (notes) {
      notes.innerHTML =
        '<p class="empty-note">Run the pipeline to generate the capacity interpretation.</p>';
    }
  }
  
  
  /* ==============================================================
    CAPACITY RESOURCE PRESSURE
    ============================================================== */
  
  function renderCapacityPressure(resource, pressure) {
    const valueEl =
      qs(`#capacity-pressure-${resource}`);
  
    const barEl =
      qs(`#capacity-pressure-bar-${resource}`);
  
    if (!valueEl || !barEl) return;
  
    if (!pressure) {
      valueEl.textContent = '—';
      barEl.style.width = '0%';
      barEl.dataset.level = 'unknown';
      return;
    }
  
    const pct = pressureToCapacityPercent(
      pressure
    );
  
    valueEl.textContent = pressure;
  
    barEl.style.width = `${pct}%`;
    barEl.dataset.level =
      String(pressure).toLowerCase();
  }
  
  
  /* ==============================================================
    CAPACITY SIGNAL
    ============================================================== */
  
  function setCapacitySignal(id, value) {
    const el = qs(`#${id}`);
  
    if (!el) return;
  
    const strong = el.querySelector('strong');
  
    if (value === null || value === undefined) {
      el.dataset.active = 'false';
    
      if (strong) {
        strong.textContent = '—';
      }
    
      return;
    }
  
    el.dataset.active = value
      ? 'true'
      : 'false';
  
    if (strong) {
      strong.textContent = value
        ? 'YES'
        : 'NO';
    }
  }
  
  
  /* ==============================================================
    CAPACITY INTERPRETATION
    ============================================================== */
  
  function renderCapacityInterpretation(
    results,
    health,
    signals
  ) {
    const notesEl =
      qs('#capacityInterpretation');
  
    if (!notesEl) return;
  
    const notes = [];
  
    const safeUsers =
      Number(results.safe_users);
  
    const currentUsers =
      Number(results.current_users);
  
    const breakingPoint =
      Number(results.breaking_point);
  
    const queueUtil =
      Number(results.queue_utilization);
  
    const throughputUtil =
      Number(results.throughput_capacity_used_percent);
  
    const margin =
      Number(results.capacity_margin);
  
  
    /* ----------------------------------------------------------
      Overall capacity
      ---------------------------------------------------------- */
  
    if (
      Number.isFinite(safeUsers) &&
      Number.isFinite(currentUsers)
    ) {
      if (currentUsers > safeUsers) {
        notes.push(
          `The current workload of ${fmtInt(currentUsers)} users is above the calculated safe capacity of ${fmtInt(safeUsers)} users.`
        );
      } else {
        notes.push(
          `The current workload of ${fmtInt(currentUsers)} users remains within the calculated safe capacity of ${fmtInt(safeUsers)} users.`
        );
      }
    }
  
  
    /* ----------------------------------------------------------
      Queue
      ---------------------------------------------------------- */
  
    if (Number.isFinite(queueUtil)) {
      if (queueUtil >= 0.85) {
        notes.push(
          `Queue utilization is ${fmtPercent(queueUtil * 100, 1)}, indicating high queue pressure and limited additional headroom.`
        );
      } else if (queueUtil >= 0.70) {
        notes.push(
          `Queue utilization is ${fmtPercent(queueUtil * 100, 1)}, indicating moderate-to-high pressure.`
        );
      } else {
        notes.push(
          `Queue utilization remains below the high-pressure threshold at ${fmtPercent(queueUtil * 100, 1)}.`
        );
      }
    }
  
  
    /* ----------------------------------------------------------
      Throughput
      ---------------------------------------------------------- */
  
    if (Number.isFinite(throughputUtil)) {
      if (throughputUtil > 100) {
        notes.push(
          `Observed throughput is ${fmtPercent(throughputUtil, 1)} of the modeled USL peak throughput, meaning the observed throughput exceeds that modeled peak estimate.`
        );
      } else {
        notes.push(
          `Observed throughput is using ${fmtPercent(throughputUtil, 1)} of the modeled USL peak throughput.`
        );
      }
    }
  
  
    /* ----------------------------------------------------------
      Breaking point
      ---------------------------------------------------------- */
  
    if (
      Number.isFinite(breakingPoint) &&
      Number.isFinite(currentUsers)
    ) {
      if (breakingPoint <= currentUsers) {
        notes.push(
          `The estimated breaking point is ${fmtInt(breakingPoint)} users, which is at or below the current tested workload.`
        );
      } else {
        notes.push(
          `The estimated breaking point is approximately ${fmtInt(breakingPoint)} users.`
        );
      }
    }
  
  
    /* ----------------------------------------------------------
      Margin
      ---------------------------------------------------------- */
  
    if (Number.isFinite(margin)) {
      notes.push(
        `The remaining safe-capacity margin is ${fmtPercent(margin, 1)}.`
      );
    }
  
  
    /* ----------------------------------------------------------
      Scaling
      ---------------------------------------------------------- */
  
    if (health.recommended_scaling === 'horizontal') {
      notes.push(
        'Horizontal scaling is recommended because the current capacity signals indicate workload or queue pressure.'
      );
    } else if (health.recommended_scaling === 'vertical') {
      notes.push(
        'Vertical scaling is indicated by resource pressure in the current workload.'
      );
    } else {
      notes.push(
        'No immediate scaling direction is indicated by the current capacity signals.'
      );
    }
  
  
    /* ----------------------------------------------------------
      Bottleneck
      ---------------------------------------------------------- */
  
    if (results.bottleneck_resource) {
      notes.push(
        `${String(results.bottleneck_resource).toUpperCase()} is the demand-based bottleneck among the monitored resources.`
      );
    }
  
  
    /* ----------------------------------------------------------
      Extrapolation
      ---------------------------------------------------------- */
  
    if (results.extrapolation_reliable === false) {
      notes.push(
        'USL extrapolation is marked as unreliable, so the capacity reference uses the observed maximum tested workload rather than relying on theoretical extrapolation.'
      );
    }
  
  
    notesEl.innerHTML = notes.length
      ? notes
          .map(
            (note) =>
              `<p>${escapeHtml(note)}</p>`
          )
          .join('')
      : '<p class="empty-note">No additional interpretation available.</p>';
  }
  
  
  /* ==============================================================
    CAPACITY HELPERS
    ============================================================== */
  
  function clampPercent(value) {
    const n = Number(value);
  
    if (!Number.isFinite(n)) {
      return 0;
    }
  
    return Math.max(
      0,
      Math.min(100, n)
    );
  }
  
  
  function pressureToCapacityPercent(pressure) {
    switch (
      String(pressure || '').toLowerCase()
    ) {
      case 'low':
      case 'normal':
        return 25;
    
      case 'moderate':
        return 55;
    
      case 'high':
        return 80;
    
      case 'critical':
        return 100;
    
      default:
        return 0;
    }
  }
  
  
  function capacityStatusBadgeType(status) {
    switch (
      String(status || '').toLowerCase()
    ) {
      case 'excellent':
      case 'healthy':
        return 'success';
    
      case 'good':
        return 'accent';
    
      case 'moderate':
      case 'near capacity':
        return 'warning';
    
      case 'limited':
      case 'at capacity':
      case 'overloaded':
        return 'warning';
    
      case 'poor':
      case 'critical':
        return 'danger';
    
      default:
        return 'neutral';
    }
  }
  
  
  function capacityUtilizationLabel(value) {
    const n = Number(value);
  
    if (!Number.isFinite(n)) {
      return 'Unknown';
    }
  
    if (n > 100) {
      return 'Exceeded';
    }
  
    if (n >= 90) {
      return 'Near Limit';
    }
  
    if (n >= 75) {
      return 'High Usage';
    }
  
    return 'Available';
  }
  
  
  function capacityUtilizationBadgeType(value) {
    const n = Number(value);
  
    if (!Number.isFinite(n)) {
      return 'neutral';
    }
  
    if (n > 100) {
      return 'danger';
    }
  
    if (n >= 90) {
      return 'warning';
    }
  
    if (n >= 75) {
      return 'warning';
    }
  
    return 'success';
  }
  
  
  function queueStatusBadgeType(status) {
    switch (
      String(status || '').toLowerCase()
    ) {
      case 'stable':
        return 'success';
    
      case 'near saturation':
        return 'warning';
    
      case 'unstable':
        return 'danger';
    
      default:
        return 'neutral';
    }
  }
  
  
  function bottleneckSeverityBadgeType(severity) {
    switch (
      String(severity || '').toLowerCase()
    ) {
      case 'substantial headroom':
        return 'success';
    
      case 'moderate headroom':
        return 'accent';
    
      case 'near ceiling':
        return 'warning';
    
      case 'at or beyond theoretical ceiling':
        return 'danger';
    
      default:
        return 'neutral';
    }
  }
  
  
  function formatScalingRecommendation(value) {
    switch (
      String(value || '').toLowerCase()
    ) {
      case 'horizontal':
        return 'Horizontal';
    
      case 'vertical':
        return 'Vertical';
    
      case 'none':
        return 'No scaling';
    
      default:
        return 'Unknown';
    }
  }
  
  
  function capitalizeStatus(value) {
    const text = String(value || '');
  
    if (!text) {
      return 'Unknown';
    }
  
    return (
      text.charAt(0).toUpperCase() +
      text.slice(1)
    );
  }
  
  
  function buildCapacityHealthDescription(
    results,
    health,
    signals
  ) {
    const status =
      health.overall_status ||
      results.capacity_classification ||
      'Unknown';
  
    const risk =
      health.main_risk ||
      'None';
  
    if (
      signals.capacity_exceeded ||
      signals.throughput_capacity_exceeded
    ) {
      return `Capacity is ${String(status).toLowerCase()}. The main risk is ${risk}, and the current workload has exceeded at least one calculated capacity boundary.`;
    }
  
    if (signals.queue_pressure) {
      return `Capacity is ${String(status).toLowerCase()} with ${risk} identified as the primary pressure source.`;
    }
  
    return `Capacity is classified as ${String(status).toLowerCase()} based on the combined mathematical and runtime analysis.`;
  }

  function healthBadgeVariant(classification) {
    const c = String(classification || '').toLowerCase();

    if (
      c.includes('collapsed') ||
      c.includes('collapse') ||
      c.includes('critical') ||
      c.includes('failed') ||
      c.includes('failure')
    ) {
      return 'danger';
      }

    if (
      c.includes('overload') ||
      c.includes('warning') ||
      c.includes('degraded') ||
      c.includes('limited')
    ) {
      return 'warning';
      }

    if (
      c.includes('healthy') ||
      c.includes('optimal') ||
      c.includes('stable') ||
      c.includes('safe')
    ) {
      return 'success';
      }

    return 'neutral';
  }


  function updateScalabilityResourceBar(id, value, max = 100) {
    const bar = qs(`#${id}`);
    if (!bar) return;

    const numericValue = Number(value);
    const numericMax = Number(max);

    if (
      !Number.isFinite(numericValue) ||
      !Number.isFinite(numericMax) ||
      numericMax <= 0
    ) {
      bar.style.width = '0%';
      return;
      }

    const percentage = Math.max(
      0,
      Math.min(100, (numericValue / numericMax) * 100)
    );

    bar.style.width = `${percentage}%`;
  }


  function formatScalabilityConstraint(value) {
    if (!value) return '—';

    const map = {
      bottleneck_resource: 'Bottleneck resource',
      concurrency: 'Concurrency'
    };

    return map[value] || String(value);
  }

  /* ---- Scalability -------------------------------------------- */

  function renderScalability(scalability, levels) {

    const predictions =
      (scalability && Array.isArray(scalability.predictions))
        ? scalability.predictions
        : [];

    const summary =
      (scalability && scalability.summary)
        ? scalability.summary
        : {};

    const body = qs('#scalabilityTableBody');

    /* ============================================================
      EMPTY STATE
      ============================================================ */

    if (!predictions.length) {

      if (body) {
        body.innerHTML = `
          <tr>
            <td colspan="8" class="data-table__empty">
              Scalability prediction unavailable for this run.
            </td>
          </tr>
        `;
      }

      setText('scalabilityTableCount', '0 scenarios');
      setBadge(
        'scalabilitySummaryStatus',
        'Unavailable',
        'neutral'
      );

      setText(
        'scalabilitySummaryText',
        'No future-load prediction data is available for this run.'
      );

      return;
    }


    /* ============================================================
      NORMALIZE PREDICTION VALUES
      ============================================================ */

    const rows = predictions.map((p) => {

      const users = Number(p.users);

      const throughput = Number(
        p.bounded_predicted_throughput ??
        p.predicted_throughput
      );

      const responseTime = Number(
        p.predicted_response_time
      );

      const errorRate = Number(
        p.predicted_error_rate
      );

      const cpu = Number(
        p.predicted_cpu
      );

      const memory = Number(
        p.predicted_memory
      );

      const disk = Number(
        p.predicted_disk_io
      );

      const network = Number(
        p.predicted_network_io
      );

      const efficiency = Number(
        p.scalability_efficiency
      );

      const capacityUtilization = Number(
        p.capacity_utilization
      );

      return {
        raw: p,
        users,
        throughput,
        responseTime,
        errorRate,
        cpu,
        memory,
        disk,
        network,
        efficiency,
        capacityUtilization,
        risk: p.saturation_risk || '—',
        classification: p.classification || '—',
        exceedsBound: Boolean(p.exceeds_asymptotic_bound)
      };

    });


    /* ============================================================
      SUMMARY
      ============================================================ */

    const recommendedUsers =
      Number(
        summary.recommended_max_users ??
        summary.safe_prediction_limit
      );

    const maximumUsers =
      Number(summary.maximum_predicted_users);

    const safeLimit =
      Number(summary.safe_prediction_limit);

    const collapsePoint =
      summary.first_collapsed_point != null
        ? Number(summary.first_collapsed_point)
        : null;

    const confidence =
      summary.prediction_confidence ||
      scalability.prediction_confidence ||
      '—';


    animateNumber(
      'scalabilityRecommendedUsers',
      Number.isFinite(recommendedUsers)
        ? recommendedUsers
        : 0,
      0
    );

    animateNumber(
      'scalabilityMaximumUsers',
      Number.isFinite(maximumUsers)
        ? maximumUsers
        : 0,
      0
    );

    animateNumber(
      'scalabilitySafeLimit',
      Number.isFinite(safeLimit)
        ? safeLimit
        : 0,
      0
    );


    setText(
      'scalabilityFirstCollapse',
      collapsePoint != null
        ? `${fmtInt(collapsePoint)} users`
        : 'None detected'
    );

    setText(
      'scalabilityConfidence',
      String(confidence)
    );


    /* ============================================================
      OVERALL STATUS
      ============================================================ */

    const firstCollapsed =
      rows.find((r) =>
        String(r.classification).toLowerCase().includes('collapsed')
      );

    const firstCritical =
      rows.find((r) =>
        String(r.risk).toLowerCase().includes('critical')
      );


    let overallStatus = 'Stable';
    let overallVariant = 'success';

    if (firstCollapsed) {

      overallStatus = 'Collapse predicted';
      overallVariant = 'danger';

    } else if (firstCritical) {

      overallStatus = 'Critical risk';
      overallVariant = 'danger';

    } else if (
      rows.some((r) =>
        String(r.risk).toLowerCase().includes('warning')
      )
    ) {

      overallStatus = 'Warning';
      overallVariant = 'warning';

    }


    setBadge(
      'scalabilitySummaryStatus',
      overallStatus,
      overallVariant
    );


    /* ============================================================
      SUMMARY EXPLANATION
      ============================================================ */

    let summaryText =
      `The model recommends a maximum of ${fmtInt(recommendedUsers)} concurrent users.`;

    if (collapsePoint != null) {

      summaryText +=
        ` Predicted system collapse begins at approximately ${fmtInt(collapsePoint)} users.`;

    }

    if (Number.isFinite(maximumUsers)) {

      summaryText +=
        ` The projection extends to ${fmtInt(maximumUsers)} users.`;

    }

    setText(
      'scalabilitySummaryText',
      summaryText
    );


    /* ============================================================
      TABLE
      ============================================================ */

    if (body) {

      body.innerHTML = rows.map((r) => {

        const healthVariant =
          healthBadgeVariant(r.classification);

        const riskText =
          r.risk !== '—'
            ? r.risk
            : r.classification;

        return `
          <tr
            data-users="${Number.isFinite(r.users) ? r.users : ''}"
            data-health="${escapeHtml(r.classification)}">

            <td class="mono">
              ${fmtInt(r.users)}
            </td>

            <td class="mono">
              ${fmt(r.throughput, 2)}
            </td>

            <td class="mono">
              ${fmt(r.responseTime, 1)} ms
            </td>

            <td class="mono">
              ${fmt(r.errorRate, 2)}%
            </td>

            <td class="mono">
              ${fmt(r.cpu, 2)}%
            </td>

            <td class="mono">
              ${fmt(r.capacityUtilization, 1)}%
            </td>

            <td class="mono">
              ${Number.isFinite(r.efficiency)
                ? fmt(r.efficiency * 100, 2) + '%'
                : '—'}
            </td>

            <td>
              <span class="badge badge--${healthVariant}">
                ${escapeHtml(riskText)}
              </span>
            </td>

          </tr>
        `;

      }).join('');

    }


    setText(
      'scalabilityTableCount',
      `${rows.length} scenarios`
    );


    /* ============================================================
      PEAK METRICS
      ============================================================ */

    const maxThroughput =
      Math.max(
        ...rows
          .map((r) => r.throughput)
          .filter(Number.isFinite)
      );

    const maxResponse =
      Number(
        summary.highest_response_time ??
        Math.max(
          ...rows
            .map((r) => r.responseTime)
            .filter(Number.isFinite)
        )
      );

    const maxError =
      Math.max(
        ...rows
          .map((r) => r.errorRate)
          .filter(Number.isFinite)
      );

    const maxCapacity =
      Math.max(
        ...rows
          .map((r) => r.capacityUtilization)
          .filter(Number.isFinite)
      );


    setText(
      'scalabilityPeakThroughput',
      fmt(maxThroughput, 2)
    );

    setText(
      'scalabilityPeakResponse',
      fmt(maxResponse, 1)
    );

    setText(
      'scalabilityPeakError',
      fmt(maxError, 2)
    );

    setText(
      'scalabilityPeakCapacity',
      fmt(maxCapacity, 1)
    );


    /* ============================================================
      RESOURCE HEALTH
      ============================================================ */

    const highestCpu =
      Number(
        summary.highest_cpu ??
        Math.max(...rows.map((r) => r.cpu).filter(Number.isFinite))
      );

    const highestMemory =
      Number(
        summary.highest_memory ??
        Math.max(...rows.map((r) => r.memory).filter(Number.isFinite))
      );

    const highestDisk =
      Number(
        summary.highest_disk_io ??
        Math.max(...rows.map((r) => r.disk).filter(Number.isFinite))
      );

    const highestNetwork =
      Number(
        summary.highest_network_io ??
        Math.max(...rows.map((r) => r.network).filter(Number.isFinite))
      );


    setText(
      'futureCpuValue',
      fmt(highestCpu, 2) + '%'
    );

    setText(
      'futureMemoryValue',
      fmt(highestMemory, 2) + '%'
    );

    setText(
      'futureDiskValue',
      fmt(highestDisk, 2)
    );

    setText(
      'futureNetworkValue',
      fmt(highestNetwork, 2)
    );


    updateScalabilityResourceBar(
      'futureCpuBar',
      highestCpu,
      100
    );

    updateScalabilityResourceBar(
      'futureMemoryBar',
      highestMemory,
      100
    );

    /*
    * Disk and network values are MB/s in the backend.
    * Use 100 MB/s as the configured reference limit.
    */

    updateScalabilityResourceBar(
      'futureDiskBar',
      highestDisk,
      100
    );

    updateScalabilityResourceBar(
      'futureNetworkBar',
      highestNetwork,
      100
    );


    /* ============================================================
      MODEL BOUND
      ============================================================ */

    const boundRow = rows.find(
      (r) =>
        Number.isFinite(
          Number(r.raw?.asymptotic_throughput_bound)
        )
    );

    const bound = boundRow
      ? Number(boundRow.raw.asymptotic_throughput_bound)
      : NaN;

    const bindingConstraint = boundRow
      ? boundRow.raw.bound_binding_constraint
      : null;

    const boundExceeded =
      rows.some((r) => r.exceedsBound);


    setText(
      'scalabilityAsymptoticBound',
      Number.isFinite(bound)
        ? `${fmt(bound, 2)} req/s`
        : '—'
    );


    setText(
      'scalabilityBindingConstraint',
      formatScalabilityConstraint(bindingConstraint)
    );


    setText(
      'scalabilityBoundExceeded',
      boundExceeded
        ? 'Yes'
        : 'No'
    );


    setText(
      'scalabilityRecommendedMaximum',
      Number.isFinite(recommendedUsers)
        ? `${fmtInt(recommendedUsers)} users`
        : '—'
    );


    /* ============================================================
      RISK PANEL
      ============================================================ */

    setText(
      'riskSafeLimit',
      Number.isFinite(safeLimit)
        ? `${fmtInt(safeLimit)} users`
        : '—'
    );

    setText(
      'riskCollapsePoint',
      collapsePoint != null
        ? `${fmtInt(collapsePoint)} users`
        : 'None detected'
    );

    setText(
      'riskResponseTime',
      Number.isFinite(maxResponse)
        ? `${fmt(maxResponse, 1)} ms`
        : '—'
    );

    setText(
      'riskErrorRate',
      Number.isFinite(maxError)
        ? `${fmt(maxError, 2)}%`
        : '—'
    );


    /* ============================================================
      CHART
      ============================================================ */

    const measuredLevels =
      Array.isArray(levels)
        ? levels
        : [];

    const measuredLabels =
      measuredLevels.map((l) =>
        fmtInt(l.users)
      );

    const predictedLabels =
      rows.map((r) =>
        fmtInt(r.users)
      );


    const allLabels =
      measuredLabels.concat(predictedLabels);


    const measuredData =
      measuredLevels
        .map((l) => Number(l.mean_throughput))
        .concat(rows.map(() => null));


    const predictedData =
      measuredLabels
        .map(() => null)
        .concat(rows.map((r) => r.throughput));


    /*
    * Add the theoretical asymptotic bound as a reference line.
    */

    const boundData =
      allLabels.map(() =>
        Number.isFinite(bound)
          ? bound
          : null
      );


    const chartDatasets = [

      measuredDataset(
        'Measured throughput',
        measuredData
      ),

      predictedDataset(
        'Predicted throughput',
        predictedData
      )

    ];


    /*
    * Only add bound if backend actually supplied one.
    */

    if (Number.isFinite(bound)) {

      chartDatasets.push({
        label: 'Asymptotic throughput bound',
        data: boundData,
        borderDash: [6, 6],
        pointRadius: 0,
        fill: false,
        tension: 0
      });

    }


    const chartBase =
      baseChartOptions();


    const chartOptions =
      Object.assign(
        {},
        chartBase,
        {
          interaction: {
            mode: 'index',
            intersect: false
          },

          plugins: Object.assign(
            {},
            chartBase.plugins || {},
            {
              tooltip: {
                enabled: true
              }
            }
          ),

          scales: Object.assign(
            {},
            chartBase.scales,
            {
              x: Object.assign(
                {},
                chartBase.scales
                  ? chartBase.scales.x
                  : {},
                {
                  title: {
                    display: true,
                    text: 'Concurrent users',
                    color: cssVar('--ink-muted')
                  }
                }
              ),

              y: Object.assign(
                {},
                chartBase.scales
                  ? chartBase.scales.y
                  : {},
                {
                  title: {
                    display: true,
                    text: 'Throughput (req/s)',
                    color: cssVar('--ink-muted')
                  }
                }
              )
            }
          )
        }
      );


    makeOrUpdateChart(
      'scalability',
      'scalabilityChart',
      {
        type: 'line',

        data: {
          labels: allLabels,
          datasets: chartDatasets
        },

        options: chartOptions
      }
    );


    /* ============================================================
      CHART STATUS
      ============================================================ */

    if (firstCollapsed) {

      setBadge(
        'scalabilityChartBadge',
        `Collapse at ${fmtInt(firstCollapsed.users)} users`,
        'danger'
      );

    } else {

      setBadge(
        'scalabilityChartBadge',
        'Projection available',
        'success'
      );

    }


    /* ============================================================
      INTERPRETATION
      ============================================================ */

    const notes = [];


    notes.push(
      `The recommended maximum operating capacity is approximately ${fmtInt(recommendedUsers)} concurrent users.`
    );


    if (collapsePoint != null) {

      notes.push(
        `The first predicted collapse occurs at ${fmtInt(collapsePoint)} users, indicating that workloads beyond the safe limit should not be treated as production-safe.`
      );

    }


    if (Number.isFinite(maxResponse)) {

      notes.push(
        `Predicted response time reaches ${fmt(maxResponse, 1)} ms at the highest projected load.`
      );

    }


    if (Number.isFinite(maxError) && maxError > 0) {

      notes.push(
        `The model predicts an error rate as high as ${fmt(maxError, 2)}%, indicating severe degradation under extreme future load.`
      );

    }


    if (Number.isFinite(bound)) {

      notes.push(
        `The theoretical throughput ceiling is approximately ${fmt(bound, 2)} req/s and is constrained by ${formatScalabilityConstraint(bindingConstraint)}.`
      );

    }


    if (highestCpu < 70) {

      notes.push(
        `CPU is not predicted to be the dominant limiting factor in the projected range; the major degradation is associated with system scalability and capacity pressure.`
      );

    }


    if (highestMemory < 10) {

      notes.push(
        `Predicted memory utilization remains relatively low across the projected scenarios.`
      );

    }


    const interpretation =
      qs('#scalabilityInterpretation');


    if (interpretation) {

      interpretation.innerHTML =
        notes
          .map((note) =>
            `<p>${escapeHtml(note)}</p>`
          )
          .join('');

    }

  }


  /* ============================================================
    RECOMMENDATIONS
    ============================================================ */

  function renderRecommendations(rec) {

    const primaryPanel = qs('#primaryRecPanel');
    const actionPanel = qs('#recActionPanel');

    /*
    * ----------------------------------------------------------
    * Reset
    * ----------------------------------------------------------
    */

    if (!rec || rec.success === false) {

      setText(
        'primaryRecText',
        rec && rec.error
          ? `Recommendation engine error: ${rec.error}`
          : 'Recommendation engine did not run.'
      );

      setText('primaryRecTag', 'Recommendation unavailable');

      setBadge(
        'recOverallBadge',
        'Unavailable',
        'neutral'
      );

      qs('#recSummaryStrip').hidden = true;
      qs('#recPrimaryMeta').hidden = true;
      actionPanel.hidden = true;

      qs('#recCardList').innerHTML =
        '<p class="empty-note">No recommendations generated.</p>';

      setText('recListCount', '0 recommendations');

      return;
    }


    /*
    * ----------------------------------------------------------
    * Backend blocks
    * ----------------------------------------------------------
    */

    const summary = rec.summary || {};
    const recommendations = Array.isArray(rec.recommendations)
      ? rec.recommendations
      : [];

    const resourceAnalysis = rec.resource_analysis || {};
    const bottleneck = resourceAnalysis.bottleneck || {};

    const capacity = rec.capacity || {};
    const scalability = rec.scalability || {};


    /*
    * ----------------------------------------------------------
    * Executive summary
    * ----------------------------------------------------------
    */

    const overallPriority =
      summary.overall_priority || 'INFO';

    const primaryRisk =
      summary.primary_risk || 'None';

    const scalingStrategy =
      summary.recommended_scaling || 'none';

    setText(
      'primaryRecTag',
      `Primary recommendation — ${overallPriority}`
    );

    setText(
      'primaryRecText',
      summary.executive_summary ||
        'No executive summary available.'
    );

    setBadge(
      'recOverallBadge',
      overallPriority,
      priorityBadgeVariant(overallPriority)
    );


    /*
    * ----------------------------------------------------------
    * Primary metadata
    * ----------------------------------------------------------
    */

    qs('#recPrimaryMeta').hidden = false;

    setText(
      'rec-primary-risk',
      primaryRisk
    );

    setText(
      'rec-scaling-strategy',
      scalingStrategy === 'none'
        ? 'No scaling change'
        : capitalizeFirst(scalingStrategy)
    );

    setText(
      'rec-safe-users',
      fmtInt(summary.safe_users)
    );

    setText(
      'rec-current-users',
      fmtInt(summary.current_users)
    );


    /*
    * ----------------------------------------------------------
    * Priority counts
    * ----------------------------------------------------------
    */

    qs('#recSummaryStrip').hidden = false;

    setText(
      'rec-count-critical',
      summary.critical_count ?? 0
    );

    setText(
      'rec-count-high',
      summary.high_count ?? 0
    );

    setText(
      'rec-count-medium',
      summary.medium_count ?? 0
    );

    setText(
      'rec-count-low',
      summary.low_count ?? 0
    );


    /*
    * ----------------------------------------------------------
    * Immediate action
    *
    * Recommendations are already sorted by backend priority.
    * Therefore the first recommendation is the strongest
    * action the engine wants the user to see.
    * ----------------------------------------------------------
    */

    const primaryRecommendation =
      recommendations.length
        ? recommendations[0]
        : null;

    if (primaryRecommendation) {

      actionPanel.hidden = false;

      setText(
        'recActionTitle',
        primaryRecommendation.title ||
          primaryRecommendation.id ||
          'Immediate recommendation'
      );

      setBadge(
        'recActionPriority',
        primaryRecommendation.priority || 'INFO',
        priorityBadgeVariant(
          primaryRecommendation.priority
        )
      );

      setText(
        'recActionProblem',
        primaryRecommendation.problem || '—'
      );

      setText(
        'recActionRecommendation',
        primaryRecommendation.recommendation || '—'
      );

      const actionList =
        Array.isArray(primaryRecommendation.actions)
          ? primaryRecommendation.actions
          : [];

      const actionEl = qs('#recActionList');

      actionEl.innerHTML = actionList.length
        ? actionList
            .map(
              (action) =>
                `<li>${escapeHtml(action)}</li>`
            )
            .join('')
        : '<li>No specific actions supplied.</li>';

      setText(
        'recActionImpact',
        primaryRecommendation.expected_impact ||
          '—'
      );

    } else {

      actionPanel.hidden = true;
    }


    /*
    * ----------------------------------------------------------
    * Bottleneck
    * ----------------------------------------------------------
    */

    const bottleneckResource =
      bottleneck.resource;

    const bottleneckSeverity =
      bottleneck.severity;

    setBadge(
      'recBottleneckSeverityBadge',
      bottleneckSeverity || 'Unknown',
      bottleneckSeverityVariant(bottleneckSeverity)
    );

    setText(
      'rb-resource',
      bottleneckResource
        ? formatResourceName(bottleneckResource)
        : '—'
    );

    setText(
      'rb-demand',
      fmt(bottleneck.service_demand, 6)
    );

    setText(
      'rb-bound',
      fmt(bottleneck.asymptotic_throughput_bound, 2)
    );

    setText(
      'rb-nstar',
      fmt(bottleneck.optimal_concurrency_n_star, 2)
    );


    /*
    * ----------------------------------------------------------
    * Resource pressure
    * ----------------------------------------------------------
    */

    const resourceKeys = [
      'cpu',
      'memory',
      'disk',
      'network',
      'queue'
    ];

    resourceKeys.forEach((key) => {

      const status =
        resourceAnalysis[key] || 'Unknown';

      setBadge(
        `rec-${key}-status`,
        status,
        pressureVariant(status)
      );

      updateRecommendationResourceState(
        key,
        status
      );
    });


    /*
    * ----------------------------------------------------------
    * Recommendation cards
    * ----------------------------------------------------------
    */

    const cardList =
      qs('#recCardList');

    if (!recommendations.length) {

      cardList.innerHTML =
        '<p class="empty-note">No recommendations generated for this run.</p>';

    } else {

      cardList.innerHTML =
        recommendations
          .map((r, index) =>
            renderRecommendationCard(r, index)
          )
          .join('');
    }

    setText(
      'recListCount',
      `${recommendations.length} recommendation${recommendations.length === 1 ? '' : 's'}`
    );


    /*
    * ----------------------------------------------------------
    * Recommendation card interactions
    * ----------------------------------------------------------
    */

    qsa('[data-rec-toggle]', cardList)
      .forEach((btn) => {

        btn.addEventListener('click', () => {

          const card =
            btn.closest('.rec-card');

          if (!card) return;

          const expanded =
            card.classList.toggle('is-expanded');

          btn.textContent =
            expanded
              ? 'Hide details'
              : 'Show details';
        });

      });


    /*
    * ----------------------------------------------------------
    * Capacity evidence
    * ----------------------------------------------------------
    */

    setText(
      'ev-current-users',
      fmtInt(capacity.current_users)
    );

    setText(
      'ev-safe-users',
      fmtInt(capacity.safe_users)
    );

    setText(
      'ev-breaking-point',
      fmtInt(capacity.breaking_point)
    );

    setText(
      'ev-capacity-margin',
      fmtPercent(
        capacity.capacity_margin_percent,
        1
      )
    );

    setText(
      'ev-capacity-class',
      capacity.capacity_classification || '—'
    );

    setText(
      'ev-first-overloaded',
      fmtInt(
        scalability.first_overloaded_point
      )
    );

    setText(
      'ev-first-collapsed',
      fmtInt(
        scalability.first_collapsed_point
      )
    );

    setText(
      'ev-recommended-max',
      fmtInt(
        scalability.recommended_max_users
      )
    );


    /*
    * ----------------------------------------------------------
    * Operational decision guidance
    * ----------------------------------------------------------
    */

    const safeUsers =
      Number(capacity.safe_users);

    const currentUsers =
      Number(capacity.current_users);

    const breakingPoint =
      Number(capacity.breaking_point);

    const recommendedMax =
      Number(scalability.recommended_max_users);

    const firstCollapsed =
      Number(scalability.first_collapsed_point);


    /*
    * Safe range
    */

    if (Number.isFinite(safeUsers)) {

      setText(
        'decision-safe-range',
        `Keep normal operation at or below approximately ${fmtInt(safeUsers)} concurrent users.`
      );

    } else {

      setText(
        'decision-safe-range',
        'A reliable safe-user boundary is not available.'
      );
    }


    /*
    * Growth boundary
    */

    if (
      Number.isFinite(firstCollapsed) &&
      Number.isFinite(currentUsers)
    ) {

      setText(
        'decision-growth-boundary',
        `The model predicts critical degradation beginning around ${fmtInt(firstCollapsed)} users; validate this boundary with additional load testing.`
      );

    } else if (
      Number.isFinite(recommendedMax)
    ) {

      setText(
        'decision-growth-boundary',
        `The recommended maximum operating level is approximately ${fmtInt(recommendedMax)} users.`
      );

    } else {

      setText(
        'decision-growth-boundary',
        'No reliable future-load boundary was supplied.'
      );
    }


    /*
    * Scaling strategy
    */

    if (scalingStrategy === 'horizontal') {

      setText(
        'decision-scaling',
        'Add application instances behind a load balancer and use autoscaling to distribute concurrent workload.'
      );

    } else if (scalingStrategy === 'vertical') {

      const resource =
        bottleneck.resource
          ? formatResourceName(bottleneck.resource)
          : 'the dominant resource';

      setText(
        'decision-scaling',
        `Increase ${resource} capacity before accepting additional workload.`
      );

    } else {

      setText(
        'decision-scaling',
        'No immediate infrastructure scaling change was recommended.'
      );
    }


    /*
    * Model confidence
    */

    const reliabilityRecommendation =
      recommendations.find(
        (r) =>
          r.category === 'USL Scalability' &&
          r.id &&
          r.id.startsWith('USL-REL')
      );

    if (reliabilityRecommendation) {

      setText(
        'decision-confidence',
        'USL extrapolation reliability is low. Gather additional load-test points before making firm capacity commitments.'
      );

    } else {

      setText(
        'decision-confidence',
        'Recommendations are based on the available runtime and mathematical-model evidence.'
      );
    }
  }


  /* ============================================================
    Recommendation card renderer
    ============================================================ */

  function renderRecommendationCard(rec, index) {

    const priority =
      rec.priority || 'INFO';

    const category =
      rec.category || 'General';

    const id =
      rec.id || `REC-${index + 1}`;

    const actions =
      Array.isArray(rec.actions)
        ? rec.actions
        : [];

    const evidence =
      rec.evidence &&
      typeof rec.evidence === 'object'
        ? rec.evidence
        : {};


    const evidenceHtml =
      Object.entries(evidence)
        .filter(
          ([, value]) =>
            value !== null &&
            value !== undefined &&
            value !== ''
        )
        .map(
          ([key, value]) => `
            <div class="rec-evidence-item">
              <span>${escapeHtml(
                formatEvidenceKey(key)
              )}</span>
              <strong class="mono">${escapeHtml(
                formatEvidenceValue(value)
              )}</strong>
            </div>
          `
        )
        .join('');


    const actionsHtml =
      actions.length
        ? `
          <div class="rec-detail-block">
            <span class="section-label">
              Recommended actions
            </span>

            <ul class="rec-card__actions">
              ${actions
                .map(
                  (action) =>
                    `<li>${escapeHtml(action)}</li>`
                )
                .join('')}
            </ul>
          </div>
        `
        : '';


    return `
      <article
        class="rec-card"
        data-priority="${escapeHtml(priority)}"
        data-category="${escapeHtml(category)}"
      >

        <div class="rec-card__head">

          <div class="rec-card__identity">

            <div class="rec-card__title-row">

              <span class="rec-card__index mono">
                ${String(index + 1).padStart(2, '0')}
              </span>

              <h4 class="rec-card__title">
                ${escapeHtml(
                  rec.title || 'Recommendation'
                )}
              </h4>

            </div>

            <div class="rec-card__meta">
              ${escapeHtml(category)}
              <span>·</span>
              ${escapeHtml(id)}
            </div>

          </div>

          <span
            class="badge badge--${priorityBadgeVariant(priority)}">
            ${escapeHtml(priority)}
          </span>

        </div>


        <div class="rec-card__content">

          <p class="rec-card__problem">
            ${escapeHtml(
              rec.problem || ''
            )}
          </p>

          <div class="rec-card__recommendation">

            <span class="section-label">
              Recommendation
            </span>

            <p>
              ${escapeHtml(
                rec.recommendation || ''
              )}
            </p>

          </div>

          ${
            rec.expected_impact
              ? `
                <div class="rec-card__impact">

                  <span class="section-label">
                    Expected impact
                  </span>

                  <p>
                    ${escapeHtml(
                      rec.expected_impact
                    )}
                  </p>

                </div>
              `
              : ''
          }


          <button
            type="button"
            class="rec-card__toggle"
            data-rec-toggle>
            Show details
          </button>


          <div class="rec-card__details">

            ${actionsHtml}

            ${
              evidenceHtml
                ? `
                  <div class="rec-detail-block">

                    <span class="section-label">
                      Evidence
                    </span>

                    <div class="rec-evidence-grid">
                      ${evidenceHtml}
                    </div>

                  </div>
                `
                : ''
            }

            ${
              rec.confidence
                ? `
                  <div class="rec-confidence">
                    <span>Confidence</span>
                    <strong>
                      ${escapeHtml(
                        rec.confidence
                      )}
                    </strong>
                  </div>
                `
                : ''
            }

          </div>

        </div>

      </article>
    `;
  }


  /* ============================================================
    Priority badge
    ============================================================ */

  function priorityBadgeVariant(priority) {

    const p =
      String(priority || '')
        .toLowerCase();

    if (p === 'critical') return 'danger';
    if (p === 'high') return 'warning';
    if (p === 'medium') return 'accent';
    if (p === 'low') return 'success';
    if (p === 'info') return 'neutral';

    return 'neutral';
  }


  /* ============================================================
    Resource pressure badge
    ============================================================ */

  function pressureVariant(status) {

    const s =
      String(status || '')
        .toLowerCase();

    if (
      s.includes('critical') ||
      s.includes('unstable')
    ) {
      return 'danger';
    }

    if (
      s.includes('high') ||
      s.includes('near saturation')
    ) {
      return 'warning';
    }

    if (
      s.includes('moderate') ||
      s.includes('approaching')
    ) {
      return 'accent';
    }

    if (
      s.includes('low') ||
      s.includes('stable')
    ) {
      return 'success';
    }

    return 'neutral';
  }


  /* ============================================================
    Bottleneck severity
    ============================================================ */

  function bottleneckSeverityVariant(severity) {

    const s =
      String(severity || '')
        .toLowerCase();

    if (
      s.includes('at or beyond') ||
      s.includes('ceiling')
    ) {
      return 'danger';
    }

    if (
      s.includes('near ceiling')
    ) {
      return 'warning';
    }

    if (
      s.includes('moderate headroom')
    ) {
      return 'accent';
    }

    if (
      s.includes('substantial headroom')
    ) {
      return 'success';
    }

    return 'neutral';
  }


  /* ============================================================
    Resource visual state
    ============================================================ */

  function updateRecommendationResourceState(
    key,
    status
  ) {

    const badge =
      qs(`#rec-${key}-status`);

    if (!badge) return;

    const card =
      badge.closest('.resource-rec-card');

    if (!card) return;

    const variant =
      pressureVariant(status);

    card.dataset.resourceState =
      variant;
  }


  /* ============================================================
    Resource name
    ============================================================ */

  function formatResourceName(resource) {

    const r =
      String(resource || '')
        .toLowerCase();

    const names = {
      cpu: 'CPU',
      memory: 'Memory',
      disk: 'Disk I/O',
      network: 'Network I/O',
      queue: 'Queue'
    };

    return names[r] ||
      capitalizeFirst(String(resource || 'Unknown'));
  }


  /* ============================================================
    Evidence key formatting
    ============================================================ */

  function formatEvidenceKey(key) {

    return String(key || '')
      .replace(/_/g, ' ')
      .replace(/\b\w/g, (c) =>
        c.toUpperCase()
      );
  }


  /* ============================================================
    Evidence value formatting
    ============================================================ */

  function formatEvidenceValue(value) {

    if (
      value === null ||
      value === undefined
    ) {
      return '—';
    }

    if (typeof value === 'boolean') {
      return value ? 'Yes' : 'No';
    }

    if (typeof value === 'number') {

      if (!Number.isFinite(value)) {
        return '∞';
      }

      if (
        Math.abs(value) >= 100
      ) {
        return value.toFixed(1);
      }

      if (
        Math.abs(value) >= 1
      ) {
        return value.toFixed(2);
      }

      return value.toFixed(4);
    }

    if (typeof value === 'object') {
      try {
        return JSON.stringify(value);
      } catch {
        return String(value);
      }
    }

    return String(value);
  }


  /* ============================================================
    Small helper
    ============================================================ */

  function capitalizeFirst(value) {

    const s =
      String(value || '');

    return s
      ? s.charAt(0).toUpperCase() + s.slice(1)
      : '';
  }

  /* ============================================================
     REPOSITORY LIST
     ============================================================ */

  function initRepoList() {
    qs('#refreshReposBtn')?.addEventListener('click', loadRepoList);
    loadRepoList();
  }

  async function loadRepoList() {
    try {
      const res = await api('/api/github/repositories');
      const repos = res.repositories || [];
      const body = qs('#repoTableBody');
      body.innerHTML = repos.length ? repos.map((r) => `
        <tr>
          <td>${escapeHtml(r.project_name)}</td>
          <td class="mono">${escapeHtml(r.path)}</td>
          <td class="mono">${fmtBytes(r.size_bytes)}</td>
          <td><button class="btn btn--ghost btn--sm" data-delete-repo="${escapeHtml(r.project_name)}">Delete</button></td>
        </tr>`).join('') : '<tr><td colspan="4" class="data-table__empty">No repositories cloned yet.</td></tr>';

      qsa('[data-delete-repo]', body).forEach((btn) => {
        btn.addEventListener('click', async () => {
          try {
            await api('/api/github/delete', { method: 'POST', body: { project_name: btn.dataset.deleteRepo } });
            toast('success', 'Deleted', `${btn.dataset.deleteRepo} removed from workspace.`);
            loadRepoList();
          } catch (err) {
            toast('error', 'Delete failed', err.message);
          }
        });
      });
    } catch (err) {
      toast('error', 'Could not load repositories', err.message);
    }
  }

  function fmtBytes(bytes) {
    if (bytes === null || bytes === undefined) return '—';
    const units = ['B', 'KB', 'MB', 'GB'];
    let val = Number(bytes); let i = 0;
    while (val >= 1024 && i < units.length - 1) { val /= 1024; i += 1; }
    return `${val.toFixed(1)} ${units[i]}`;
  }

  /* ============================================================
     MODEL PLAYGROUND
     ============================================================ */

  function initPlayground() {
    qsa('.playground-tab').forEach((tab) => {
      tab.addEventListener('click', () => {
        qsa('.playground-tab').forEach((t) => { t.classList.remove('is-active'); t.setAttribute('aria-selected', 'false'); });
        tab.classList.add('is-active');
        tab.setAttribute('aria-selected', 'true');
        qsa('.playground-form').forEach((f) => { f.hidden = f.dataset.modelForm !== tab.dataset.model; });
        qs('#playgroundResult').hidden = true;
      });
    });

    qs('[data-model-form="usl"]').addEventListener('submit', async (e) => {
      e.preventDefault();
      const user_levels = parseNumList(qs('#pg-usl-users').value);
      const throughput = parseNumList(qs('#pg-usl-throughput').value);
      const predict_at = parseNumList(qs('#pg-usl-predict').value);
      await runPlaygroundModel('/api/prediction/usl', { user_levels, throughput, predict_at: predict_at.length ? predict_at : undefined });
    });

    qs('[data-model-form="littles-law"]').addEventListener('submit', async (e) => {
      e.preventDefault();
      const arrival_rate = parseFloat(qs('#pg-ll-arrival').value);
      const average_response_time = parseFloat(qs('#pg-ll-rt').value);
      await runPlaygroundModel('/api/prediction/littles-law', { levels: [{ arrival_rate, average_response_time, response_time_unit: 'ms' }] });
    });

    qs('[data-model-form="queueing"]').addEventListener('submit', async (e) => {
      e.preventDefault();
      const arrival_rate = parseFloat(qs('#pg-q-arrival').value);
      const service_rate = parseFloat(qs('#pg-q-service').value);
      const num_servers = parseInt(qs('#pg-q-servers').value, 10) || 1;
      await runPlaygroundModel('/api/prediction/queueing', { arrival_rate, service_rate, num_servers });
    });

    qs('[data-model-form="bottleneck"]').addEventListener('submit', async (e) => {
      e.preventDefault();
      const throughput = parseFloat(qs('#pg-b-throughput').value);
      const current_users = parseFloat(qs('#pg-b-users').value);
      const cpu_usage = parseFloat(qs('#pg-b-cpu').value) || undefined;
      const disk_io = parseFloat(qs('#pg-b-disk').value) || undefined;
      const network_io = parseFloat(qs('#pg-b-net').value) || undefined;
      await runPlaygroundModel('/api/prediction/bottleneck', { throughput, current_users, cpu_usage, disk_io, network_io });
    });
  }

  function parseNumList(str) {
    return (str || '').split(',').map((s) => s.trim()).filter(Boolean).map(Number).filter((n) => Number.isFinite(n));
  }

  async function runPlaygroundModel(endpoint, body) {
    const resultEl = qs('#playgroundResult');
    const outputEl = qs('#playgroundOutput');
    resultEl.hidden = false;
    outputEl.textContent = 'Running…';
    try {
      const res = await api(endpoint, { method: 'POST', body });
      outputEl.textContent = JSON.stringify(res.result ?? res, null, 2);
    } catch (err) {
      outputEl.textContent = `Error: ${err.message}`;
    }
  }

  function initPlaygroundCopy() {
    qs('#playgroundCopyBtn')?.addEventListener('click', () => {
      const text = qs('#playgroundOutput').textContent;
      navigator.clipboard?.writeText(text).then(() => toast('info', 'Copied', 'Result JSON copied to clipboard.'));
    });
  }

  /* ============================================================
     REPORT EXPORT
     ============================================================ */

  function initReport() {
    qs('#exportReportBtn')?.addEventListener('click', () => {
      if (!state.fullResult) return;
      const payload = {
        project_name: state.projectName,
        github_url: state.githubUrl,
        generated_at: new Date().toISOString(),
        framework: state.detectionResult,
        validation: state.validation,
        routes: state.routesResult,
        result: state.fullResult,
      };
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${state.projectName || 'signal-report'}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast('success', 'Exported', 'Report downloaded as JSON.');
    });
  }

  /* ============================================================
     INIT
     ============================================================ */

  document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    initNav();
    initMobileNav();
    initStartForm();
    initRepoList();
    initPlayground();
    initPlaygroundCopy();
    initReport();
    resetPipeline();

    api('/api/status').then((res) => {
      if (res && res.success) logLine('Backend service is online.', 'success');
    }).catch(() => logLine('Could not reach backend service.', 'error'));
  });

})();