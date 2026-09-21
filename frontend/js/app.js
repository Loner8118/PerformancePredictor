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
    dependencyResult: null,
    descriptor: null,
    buildResult: null,
    runResult: null,
    dbMonitorId: null,
    charts: {},
  };

  const PIPELINE_STAGES = ['clone', 'detect', 'deps', 'entry', 'validate', 'build', 'run', 'health', 'routes', 'generate', 'loadtest'];
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
    'database-monitoring': ['Database Monitoring', 'Database connection and pool metrics'],
    usl: ['Scalability (USL)', 'Fits contention and coherency to the measured throughput curve.'],
    'littles-law': ["Little's Law", 'L = λ × W'],
    queueing: ['Queueing Theory', 'M/M/1 utilization and saturation.'],
    bottleneck: ['Bottleneck Analysis', 'Which resource is limiting the system.'],
    'forced-flow': ['Forced Flow Law', 'Component-level service demand for databases, caches, and external APIs.'],
    amdahl: ["Amdahl's Law", 'Optimization ceiling for the identified bottleneck.'],
    capacity: ['Capacity Planning', 'Safe operating capacity, combined from every model.'],
    scalability: ['Future Load Prediction', 'Projected system health beyond tested load.'],
    slo: ['SLO Capacity', 'Up to how many users the response-time and error-rate budget holds.'],
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

      // 3. Dependency detection (static, best-effort — never fatal)
      setStage('deps', 'active');
      try {
        const depRes = await api('/api/dependencies/detect', { method: 'POST', body: { project_name: state.projectName } });
        state.dependencyResult = depRes.dependencies;
        renderDependencies(depRes.dependencies);
        setStage('deps', 'done');
        logLine(`Dependencies detected: ${describeDependencies(depRes.dependencies)}.`);
      } catch (depErr) {
        // Static dependency detection is advisory — a failure here shouldn't
        // stop a run that can still be load-tested perfectly well.
        state.dependencyResult = null;
        renderDependencies(null, depErr.message);
        setStage('deps', 'done');
        logLine(`Dependency detection skipped: ${depErr.message}`, 'error');
      }

      // 4. Entry point
      setStage('entry', 'active');
      const epRes = await api('/api/entry-point', { method: 'POST', body: { project_name: state.projectName, detection_result: state.detectionResult } });
      state.locationResult = epRes.entry_point;
      setText('fw-entry-point', epRes.entry_point.entry_point_file || epRes.entry_point.run_command_hint || '—');
      setStage('entry', 'done');
      logLine(`Entry point resolved: ${epRes.entry_point.entry_point_file || epRes.entry_point.run_command_hint || 'unresolved'}.`);
      refreshDescriptor();

      // 5. Validate
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

      // 6. Docker build
      setStage('build', 'active');
      showSection('deployment', false);
      const buildRes = await api('/api/docker/build', { method: 'POST', body: { project_name: state.projectName, image_name: state.imageName, detection_result: state.detectionResult, location_result: state.locationResult } });
      if (!buildRes.success) { setStage('build', 'error'); renderBuild(buildRes, false); throw new ApiError(buildRes.message || 'Docker build failed.', 0, buildRes); }
      state.containerPort = buildRes.container_port;
      state.buildResult = buildRes;
      renderBuild(buildRes, true);
      setStage('build', 'done');
      logLine(`Docker image built: ${buildRes.image_name} (container port ${buildRes.container_port}).`, 'success');

      // 7. Docker run
      setStage('run', 'active');
      const runRes = await api('/api/docker/run', { method: 'POST', body: { image_name: state.imageName, container_name: 'performance-container', container_port: state.containerPort } });
      if (!runRes.success) { setStage('run', 'error'); renderRun(runRes, false); throw new ApiError(runRes.message || 'Container failed to start.', 0, runRes); }
      state.containerId = runRes.container_id;
      state.hostPort = runRes.port;
      state.host = runRes.host;
      state.runResult = runRes;
      renderRun(runRes, true);
      setStage('run', 'done');
      logLine(`Container started: ${runRes.container_id.slice(0, 12)} on port ${runRes.port}.`, 'success');
      refreshDescriptor();

      // 8. Health check
      setStage('health', 'active');
      await runHealthCheck();

      // 9. Route discovery
      setStage('routes', 'active');
      showSection('routes', false);
      const routesRes = await api('/api/routes', { method: 'POST', body: { project_name: state.projectName, detection_result: state.detectionResult, location_result: state.locationResult } });
      state.routesResult = routesRes.routes;
      renderRoutes(routesRes.routes);
      setStage('routes', 'done');
      logLine(`Discovered ${routesRes.count} route(s).`, 'success');
      refreshDescriptor();

      // 10. Locust generate
      setStage('generate', 'active');
      const locustRes = await api('/api/locust/generate', { method: 'POST', body: { project_name: state.projectName, route_discovery_result: state.routesResult } });
      state.locustResult = locustRes.locust;
      renderLocust(locustRes.locust);
      setStage('generate', 'done');
      logLine(`Locust configuration generated (${locustRes.locust.task_count} task(s)).`, 'success');

      // 11. Load testing + automatic database/cache monitoring
      setStage('loadtest', 'active');
      showSection('load-testing', false);
      initLoadLevelChips();

      setDatabaseMonitoringState('accent', 'Monitoring');
      logLine(
        'Starting load test with automatic database/cache monitoring...',
        'info'
      );

      const loadRes = await api('/api/load-testing/run', {
        method: 'POST',
        body: {
          project_name: state.projectName,
          container_id: state.containerId,
          port: state.hostPort,
          dependency_result: state.dependencyResult
        }
      });

      state.fullResult = loadRes;

      finishLoadLevelChips();

      renderFullResult(loadRes);

      setStage('loadtest', 'done');

      if (
        loadRes.database_metrics &&
        loadRes.database_metrics.available !== false
      ) {
        logLine(
          `Database monitoring completed successfully.`,
          'success'
        );
      } else if (loadRes.database_monitoring) {
        logLine(
          `Database monitoring was not available: ${
            loadRes.database_monitoring.reason ||
            'no usable runtime configuration was found.'
          }`
        );
      } else {
        logLine(
          'No database/cache runtime metrics were returned.'
        );
      }

      logLine(
        'Load test complete — full mathematical analysis ready.',
        'success'
      );

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

    // Clear stage results carried over from any previous run, so a partially
    // failed run can't leave the old run's descriptor/dependencies on screen.
    state.dependencyResult = null;
    state.descriptor = null;
    state.buildResult = null;
    state.runResult = null;

    renderDependencies(null);
    renderDescriptor(null);
    renderForcedFlow(null);
    renderAmdahl(null);
    renderSLO(null);
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

  /* ---- Dependency detection --------------------------------- */

  function dependencyName(dep) {
    if (!dep) return 'Not detected';
    if (typeof dep === 'string') return dep;
    return dep.type || dep.name || dep.engine || 'Detected';
  }

  function describeDependencies(deps) {
    if (!deps) return 'none';
    const parts = [];
    if (deps.database) parts.push(dependencyName(deps.database));
    if (deps.cache) parts.push(dependencyName(deps.cache));
    const otherCount = (deps.other_dependencies || []).length;
    if (otherCount) parts.push(`${otherCount} other`);
    return parts.length ? parts.join(', ') : 'none detected';
  }

  function renderDependencies(deps, errorMessage) {
    if (!deps) {
      setBadge('dependencyStatusBadge', errorMessage ? 'Failed' : 'Pending', errorMessage ? 'danger' : 'neutral');
      ['dep-database', 'dep-database-confidence', 'dep-cache', 'dep-cache-confidence'].forEach((id) => setText(id, '—'));
      setText('dep-other-count', '0');
      const list = qs('#dep-other-list');
      if (list) {
        list.innerHTML = `<div class="signal-empty">${escapeHtml(errorMessage || 'Dependency detection has not been run yet.')}</div>`;
      }
      const notes = qs('#dep-notes');
      if (notes) notes.innerHTML = '<p class="empty-note">No dependency notes yet.</p>';
      return;
    }

    const database = deps.database;
    const cache = deps.cache;
    const other = deps.other_dependencies || [];

    const found = Boolean(database || cache || other.length);
    setBadge('dependencyStatusBadge', found ? 'Detected' : 'None found', found ? 'success' : 'neutral');

    setText('dep-database', dependencyName(database));
    setText('dep-database-confidence', database ? (database.confidence || '—') : '—');
    setText('dep-cache', dependencyName(cache));
    setText('dep-cache-confidence', cache ? (cache.confidence || '—') : '—');
    setText('dep-other-count', String(other.length));

    const list = qs('#dep-other-list');
    if (list) {
      list.innerHTML = other.length
        ? other.map((dep) => `
            <div class="signal-row">
              <span class="signal-row__name">${escapeHtml(dependencyName(dep))}</span>
              <span class="signal-row__meta mono">${escapeHtml(dep.source || '—')}</span>
            </div>`).join('')
        : '<div class="signal-empty">No additional dependencies detected.</div>';
    }

    renderNoteList('#dep-notes', deps.notes, 'No dependency notes for this repository.');
  }

  /* ---- Application descriptor -------------------------------- */

  // Rebuilt at every pipeline checkpoint rather than only at the end —
  // build_application_descriptor() accepts partial input by design, so each
  // call just fills in more of the same normalized shape.
  async function refreshDescriptor() {
    if (!state.detectionResult) return;
    try {
      const res = await api('/api/application-descriptor', {
        method: 'POST',
        body: {
          detection_result: state.detectionResult,
          location_result: state.locationResult,
          route_discovery_result: state.routesResult,
          docker_build_result: state.buildResult,
          docker_run_result: state.runResult,
          dependency_result: state.dependencyResult,
        },
      });
      state.descriptor = res.descriptor;
      renderDescriptor(res.descriptor);
    } catch (err) {
      // Advisory only — the descriptor is a convenience view over data the
      // rest of the UI already renders from its own stage results.
      renderDescriptor(null, err.message);
    }
  }

  function renderDescriptor(descriptor, errorMessage) {
    if (!descriptor) {
      setBadge('descriptorStatusBadge', errorMessage ? 'Failed' : 'Not built', errorMessage ? 'danger' : 'neutral');
      ['desc-confidence', 'desc-ambiguous', 'desc-route-count', 'desc-route-tier', 'desc-host-port', 'desc-ready'].forEach((id) => setText(id, '—'));
      const readiness = qs('#desc-readiness');
      if (readiness) readiness.innerHTML = '<div class="kv-grid__item"><span>—</span><strong>—</strong></div>';
      const notes = qs('#desc-notes');
      if (notes) notes.innerHTML = `<p class="empty-note">${escapeHtml(errorMessage || 'Build the descriptor to see notes.')}</p>`;
      return;
    }

    const readiness = descriptor.readiness || {};
    const ready = readiness.ready_for_load_testing === true;

    setBadge('descriptorStatusBadge', ready ? 'Ready' : 'Built', ready ? 'success' : 'accent');
    setText('desc-confidence', descriptor.overall_confidence || '—');
    setText('desc-ambiguous', fmtBool(descriptor.ambiguous));
    setText('desc-route-count', fmtInt(descriptor.route_count, '0'));
    setText('desc-route-tier', descriptor.route_discovery_tier || '—');

    const hostPort = descriptor.host
      ? `${descriptor.host}${descriptor.host_port ? ` (:${descriptor.host_port})` : ''}`
      : '—';
    setText('desc-host-port', hostPort);
    setText('desc-ready', fmtBool(readiness.ready_for_load_testing));

    const readinessEl = qs('#desc-readiness');
    if (readinessEl) {
      const entries = Object.entries(readiness);
      readinessEl.innerHTML = entries.length
        ? entries.map(([key, value]) => `
            <div class="kv-grid__item">
              <span>${escapeHtml(formatSignalName(key))}</span>
              <strong>${value === true ? 'Ready' : value === false ? 'Not ready' : escapeHtml(String(value))}</strong>
            </div>`).join('')
        : '<div class="kv-grid__item"><span>—</span><strong>—</strong></div>';
    }

    // Descriptor notes are {stage, message} objects, not plain strings.
    const noteLines = (descriptor.notes || []).map((note) =>
      note && typeof note === 'object'
        ? `${formatSignalName(note.stage || 'note')}: ${note.message || ''}`
        : String(note)
    );
    renderNoteList('#desc-notes', noteLines, 'No descriptor notes for this run.');
  }

  /* ---- Shared note-list renderer ----------------------------- */

  function renderNoteList(selector, notes, emptyMessage) {
    const el = qs(selector);
    if (!el) return;
    const items = (notes || []).filter(Boolean);
    el.innerHTML = items.length
      ? items.map((note) => `<p class="note-list__item">${escapeHtml(
          typeof note === 'string' ? note : (note.message || JSON.stringify(note))
        )}</p>`).join('')
      : `<p class="empty-note">${escapeHtml(emptyMessage)}</p>`;
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
    renderDatabaseMonitoring(result);
    renderUSL(result.prediction && result.prediction.usl, levels);
    renderLittlesLaw(result.little_law, levels);
    renderQueueing(result.queueing);
    renderBottleneck(result.bottleneck);
    renderForcedFlow(result.forced_flow);
    renderAmdahl(result.amdahl);
    renderCapacity(result.capacity);
    renderScalability(result.prediction && result.prediction.scalability, levels);
    renderSLO(result.slo);
    renderRecommendations(result.recommendations);
    renderOverviewStats(result);

    // The pipeline runs its own dependency detection internally, so its
    // result can differ from the standalone call made at stage 3 (e.g. the
    // stage-3 call failed but the pipeline's succeeded). Prefer the
    // pipeline's when it produced usable data.
    if (result.dependencies && result.dependencies.success !== false) {
      state.dependencyResult = result.dependencies;
      renderDependencies(result.dependencies);
      refreshDescriptor();
    }
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

/* ============================================================
USL — UNIVERSAL SCALABILITY LAW Frontend renderer
============================================================ */

function renderUSL(usl, levels) {

const safeLevels = Array.isArray(levels)
? levels
: [];

/* ==========================================================
Helpers
========================================================== */

const finiteNumber = (value) => {

const number = Number(value);

return Number.isFinite(number)
  ? number
  : null;

};

const safeObject = (value) => {

return value && typeof value === 'object'
  ? value
  : {};

};

const formatNumber = (value, decimals = 2) => {

const number = finiteNumber(value);

return number === null
  ? '—'
  : fmt(number, decimals);

};

const formatInteger = (value) => {

const number = finiteNumber(value);

return number === null
  ? '—'
  : fmtInt(number);

};

const setEmptyState = () => {

const ids = [

  'usl-sigma',
  'usl-kappa',
  'usl-baseline',
  'usl-peak',
  'usl-optimal',
  'usl-saturation',

  'usl-r2',
  'usl-rmse',
  'usl-loo-r2',

  'usl-observations',
  'usl-tested-range',
  'usl-peak-range',
  'usl-extrapolated-count',

  'usl-sigma-stderr',
  'usl-kappa-stderr',
  'usl-kappa-relative-uncertainty',

  'uslSummaryClassification',
  'uslSummaryOptimal',
  'uslSummaryPeak',
  'uslSummaryReliability'

];


ids.forEach((id) => setText(id, '—'));


setText(
  'usl-fit-summary',
  'No fit assessment available.'
);


setText(
  'uslClassificationDescription',
  'No scalability classification available.'
);


setText(
  'uslSigmaInterpretation',
  '—'
);


setText(
  'uslKappaInterpretation',
  '—'
);


setText(
  'uslUncertaintyNote',
  'Parameter uncertainty assessment is not available.'
);


setBadge(
  'uslFitStatus',
  'Fit unavailable',
  'neutral'
);


setBadge(
  'uslFitStatusSecondary',
  '—',
  'neutral'
);


setBadge(
  'uslReliabilityBadge',
  'Extrapolation unavailable',
  'neutral'
);


setBadge(
  'uslClassification',
  '—',
  'neutral'
);


setBadge(
  'uslResidualBadge',
  '—',
  'neutral'
);


setBadge(
  'uslPredictionStatus',
  'Model estimates unavailable',
  'neutral'
);


const observedBody =
  qs('#uslObservedTableBody');


if (observedBody) {

  observedBody.innerHTML = `
    <tr>
      <td colspan="5" class="data-table__empty">
        USL model results are unavailable.
      </td>
    </tr>
  `;

}


const predictionBody =
  qs('#uslPredictionTableBody');


if (predictionBody) {

  predictionBody.innerHTML = `
    <tr>
      <td colspan="5" class="data-table__empty">
        No model estimates are available.
      </td>
    </tr>
  `;

}


const warningPanel =
  qs('#uslExtrapolationPanel');


if (warningPanel) {

  warningPanel.hidden = true;

}


/*
 * Clear chart rather than leaving an old result visible.
 */
makeOrUpdateChart(
  'usl',
  'uslChart',
  {
    type: 'line',
    data: {
      labels: [],
      datasets: []
    }
  }
);

};

/* ==========================================================
Invalid / failed backend result
========================================================== */

if (
!usl
|| usl.success === false
|| !usl.parameters
) {

setEmptyState();

return;

}

/* ==========================================================
Backend sections
========================================================== */

const parameters =
safeObject(usl.parameters);

const metrics =
safeObject(usl.usl_metrics);

const fit =
safeObject(usl.fit_quality);

const reliability =
safeObject(usl.prediction_reliability);

const uncertainty =
safeObject(usl.parameter_uncertainty);

const classification =
safeObject(usl.scalability_classification);

const interpretation =
safeObject(usl.parameter_interpretation);

const observedRange =
safeObject(usl.observed_range);

/* ==========================================================
Core parameters
========================================================== */

setText(
'usl-sigma',
formatNumber(parameters.sigma, 6)
);

setText(
'usl-kappa',
formatNumber(parameters.kappa, 8)
);

setText(
'usl-baseline',
formatNumber(
parameters.baseline_throughput,
2
)
);

setText(
'usl-peak',
formatNumber(
metrics.peak_throughput,
2
)
);

setText(
'usl-optimal',
formatInteger(
metrics.optimal_users
)
);

setText(
'usl-saturation',
formatInteger(
metrics.saturation_point
)
);

/* ==========================================================
Tested range
========================================================== */

let minUsers =
finiteNumber(observedRange.min_users);

let maxUsers =
finiteNumber(observedRange.max_users);

/*

* Fallback only for displaying the measured range.
* This does NOT create or modify a model prediction.
  */

if (
minUsers === null
|| maxUsers === null
) {

const measuredUsers =
  safeLevels
    .map((level) => finiteNumber(level.users))
    .filter((value) => value !== null);


if (measuredUsers.length) {

  minUsers =
    Math.min(...measuredUsers);

  maxUsers =
    Math.max(...measuredUsers);

}

}

if (
minUsers !== null
&& maxUsers !== null
) {

setText(
  'usl-tested-range',
  `${formatInteger(minUsers)} – ${formatInteger(maxUsers)}`
);

} else {

setText(
  'usl-tested-range',
  '—'
);

}

setText(
'usl-observations',
formatInteger(usl.observations)
);

/* ==========================================================
Extrapolation points
========================================================== */

const extrapolatedPoints =
Array.isArray(usl.extrapolated_points)
? usl.extrapolated_points
: [];

setText(
'usl-extrapolated-count',
formatInteger(
extrapolatedPoints.length
)
);

/* ==========================================================
Observed peak status
========================================================== */

const peakObserved =
reliability.peak_observed_in_tested_range;

if (peakObserved === true) {

setText(
  'usl-peak-range',
  'Yes'
);

} else if (peakObserved === false) {

setText(
  'usl-peak-range',
  'No'
);

} else {

setText(
  'usl-peak-range',
  'Unavailable'
);

}

/* ==========================================================
Fit quality
========================================================== */

const r2 =
finiteNumber(fit.r2);

const rmse =
finiteNumber(fit.rmse);

setText(
'usl-r2',
r2 === null
? '—'
: r2.toFixed(3)
);

setText(
'usl-rmse',
rmse === null
? '—'
: formatNumber(rmse, 2)
);

/* ==========================================================
LOO validation
========================================================== */

const loo =
safeObject(reliability.loo_cv);

const looR2 =
finiteNumber(loo.loo_r2);

if (
loo.available === true
&& looR2 !== null
) {

setText(
  'usl-loo-r2',
  looR2.toFixed(3)
);

} else {

setText(
  'usl-loo-r2',
  'N/A'
);

}

/* ==========================================================
Fit status
========================================================== */

const fitStatus =
typeof usl.fit_quality_status === 'string'
? usl.fit_quality_status
: 'Unknown';

const fitStatusLower =
fitStatus.toLowerCase();

let fitVariant = 'neutral';

if (
fitStatusLower === 'excellent'
|| fitStatusLower === 'good'
) {

fitVariant = 'success';

} else if (
fitStatusLower === 'moderate'
) {

fitVariant = 'warning';

} else if (
fitStatusLower === 'poor'
) {

fitVariant = 'danger';

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

/* ==========================================================
Fit explanation
========================================================== */

let fitSummary =
'Fit quality could not be determined.';

if (fitStatusLower === 'excellent') {

fitSummary =
  'The backend classified the USL fit as excellent for the measured observations.';

} else if (fitStatusLower === 'good') {

fitSummary =
  'The backend classified the USL fit as good for the measured observations.';

} else if (fitStatusLower === 'moderate') {

fitSummary =
  'The backend classified the fit as moderate. Model estimates should be interpreted with the reported uncertainty.';

} else if (fitStatusLower === 'poor') {

fitSummary =
  'The measured throughput does not closely follow the fitted USL model. Extrapolation should therefore be treated cautiously.';

}

setText(
'usl-fit-summary',
fitSummary
);

/* ==========================================================
Extrapolation reliability
========================================================== */

const reliabilityStatus =
typeof reliability.status === 'string'
? reliability.status
: 'unknown';

const usableForExtrapolation =
reliability.usable_for_extrapolation === true;

let reliabilityVariant =
'neutral';

if (usableForExtrapolation) {

reliabilityVariant = 'success';

} else if (
reliabilityStatus.toLowerCase() === 'low'
|| reliabilityStatus.toLowerCase() === 'moderate'
) {

reliabilityVariant = 'warning';

}

setBadge(
'uslReliabilityBadge',
`Extrapolation: ${reliabilityStatus}`,
reliabilityVariant
);

/* ==========================================================
Scalability classification
========================================================== */

const classificationName =
typeof classification.classification === 'string'
? classification.classification
: 'Unknown';

const classificationDescription =
typeof classification.description === 'string'
? classification.description
: 'No scalability interpretation is available.';

const classificationLower =
classificationName.toLowerCase();

let classificationVariant =
'neutral';

if (
classificationLower.includes('retrograde')
) {

classificationVariant = 'danger';

} else if (
classificationLower.includes('sublinear')
) {

classificationVariant = 'warning';

} else if (
classificationLower.includes('linear')
) {

classificationVariant = 'success';

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

/* ==========================================================
Parameter interpretation
========================================================== */

setText(
'uslSigmaInterpretation',
interpretation.sigma
|| 'Contention interpretation unavailable.'
);

setText(
'uslKappaInterpretation',
interpretation.kappa
|| 'Coherency interpretation unavailable.'
);

/* ==========================================================
Parameter uncertainty
========================================================== */

setText(
'usl-sigma-stderr',
formatNumber(
uncertainty.sigma_stderr,
8
)
);

setText(
'usl-kappa-stderr',
formatNumber(
uncertainty.kappa_stderr,
8
)
);

const kappaRelative =
finiteNumber(
reliability.kappa_relative_uncertainty
);

if (kappaRelative !== null) {

setText(
  'usl-kappa-relative-uncertainty',
  fmtPercent(
    kappaRelative * 100,
    1
  )
);

} else {

setText(
  'usl-kappa-relative-uncertainty',
  'N/A'
);

}

let uncertaintyNote =
'Parameter uncertainty assessment is not available.';

if (kappaRelative !== null) {

if (kappaRelative > 0.5) {

  uncertaintyNote =
    'κ has high relative uncertainty. Derived values such as the theoretical optimum should therefore not be treated as precise capacity limits.';

} else {

  uncertaintyNote =
    'The current measurements provide a measurable estimate of the USL coherency parameter. The reported uncertainty should still be considered when interpreting extrapolated results.';

}

}

setText(
'uslUncertaintyNote',
uncertaintyNote
);

/* ==========================================================
Observed vs model
========================================================== */

const observedVsPredicted =
safeObject(usl.observed_vs_predicted);

const observedEntries =
Object.entries(observedVsPredicted)

  .map(([key, value]) => {

    const row =
      safeObject(value);


    return {

      users:
        finiteNumber(key),

      observed:
        finiteNumber(row.observed),

      predicted:
        finiteNumber(row.predicted),

      residual:
        finiteNumber(row.residual),

      /*
       * Prefer a backend-provided deviation if available.
       * Otherwise calculate only the display-level absolute
       * percentage error from already supplied values.
       */

      deviation:
        finiteNumber(row.deviation_percent)

    };

  })

  .filter(
    (row) =>
      row.users !== null
  )

  .sort(
    (a, b) =>
      a.users - b.users
  );

const observedBody =
qs('#uslObservedTableBody');

if (observedBody) {

if (!observedEntries.length) {

  observedBody.innerHTML = `
    <tr>
      <td colspan="5" class="data-table__empty">
        No observed-vs-model data available.
      </td>
    </tr>
  `;

} else {

  observedBody.innerHTML =
    observedEntries
      .map((row) => {

        let absoluteError =
          null;


        if (
          row.residual !== null
        ) {

          absoluteError =
            Math.abs(row.residual);

        }


        let deviation =
          row.deviation;


        /*
         * Display-only fallback.
         * This does not create a prediction.
         */

        if (
          deviation === null
          && row.observed !== null
          && row.observed !== 0
          && row.residual !== null
        ) {

          deviation =
            Math.abs(
              row.residual
              / row.observed
            ) * 100;

        }


        const residualText =
          row.residual === null
            ? '—'
            : `${row.residual >= 0 ? '+' : ''}${formatNumber(row.residual, 2)}`;


        return `
          <tr>

            <td class="mono">
              ${formatInteger(row.users)}
            </td>


            <td class="mono">
              ${
                row.observed === null
                  ? '—'
                  : formatNumber(row.observed, 2)
              }
            </td>


            <td class="mono">
              ${
                row.predicted === null
                  ? '—'
                  : formatNumber(row.predicted, 2)
              }
            </td>


            <td class="mono">

              <span class="badge badge--neutral">
                ${residualText}
              </span>

            </td>


            <td class="mono">
              ${
                deviation === null
                  ? '—'
                  : fmtPercent(deviation, 1)
              }
            </td>

          </tr>
        `;

      })
      .join('');

}

}

/* ==========================================================
Residual summary
========================================================== */

const residuals =
observedEntries
.map((row) => row.residual)
.filter((value) => value !== null);

if (residuals.length) {

const meanAbsoluteResidual =
  residuals.reduce(
    (sum, value) =>
      sum + Math.abs(value),
    0
  ) / residuals.length;


setBadge(
  'uslResidualBadge',
  `Mean |residual| ${formatNumber(meanAbsoluteResidual, 2)} req/s`,
  'neutral'
);

} else {

setBadge(
  'uslResidualBadge',
  'Residuals unavailable',
  'neutral'
);

}

/* ==========================================================
Future model estimates
========================================================== */

const predictions =
safeObject(usl.predictions);

const efficiency =
safeObject(usl.efficiency);

const confidence =
safeObject(usl.predictions_with_confidence);

const predictionEntries =
Object.entries(predictions)

  .map(([key, value]) => {

    const users =
      finiteNumber(key);


    const throughput =
      finiteNumber(value);


    const efficiencyValue =
      efficiency[key] !== undefined
        ? finiteNumber(efficiency[key])
        : null;


    const confidenceValue =
      confidence[key]
        ? safeObject(confidence[key])
        : null;


    return {

      key,
      users,
      throughput,
      efficiency: efficiencyValue,
      confidence: confidenceValue

    };

  })

  .filter(
    (row) =>
      row.users !== null
  )

  .sort(
    (a, b) =>
      a.users - b.users
  );

/* ==========================================================
Prediction status
========================================================== */

if (predictionEntries.length) {

setBadge(
  'uslPredictionStatus',
  'Model estimates',
  'warning'
);

} else {

setBadge(
  'uslPredictionStatus',
  'No model estimates',
  'neutral'
);

}

/* ==========================================================
Future prediction table
========================================================== */

const predictionBody =
qs('#uslPredictionTableBody');

if (predictionBody) {

if (!predictionEntries.length) {

  predictionBody.innerHTML = `
    <tr>
      <td colspan="5" class="data-table__empty">
        No model estimates are available.
      </td>
    </tr>
  `;

} else {

  predictionBody.innerHTML =
    predictionEntries
      .map((row) => {

        const ci =
          row.confidence;


        let confidenceText =
          '—';


        if (
          ci
          && finiteNumber(ci.lower) !== null
          && finiteNumber(ci.upper) !== null
        ) {

          confidenceText =
            `${formatNumber(ci.lower, 2)} – ${formatNumber(ci.upper, 2)}`;

        }


        const isExtrapolated =
          extrapolatedPoints.some(
            (point) =>
              finiteNumber(point) === row.users
          );


        return `
          <tr>

            <td class="mono">
              ${formatInteger(row.users)}
            </td>


            <td class="mono">

              ${
                row.throughput === null
                  ? '—'
                  : `<strong>${formatNumber(row.throughput, 2)}</strong>
                     <span class="table-unit">req/s</span>`
              }

            </td>


            <td class="mono">

              ${
                row.efficiency !== null
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
                    : 'Model estimate'
                }

              </span>

            </td>

          </tr>
        `;

      })
      .join('');

}

}

/* ==========================================================
Extrapolation warning
========================================================== */

const warningPanel =
qs('#uslExtrapolationPanel');

if (warningPanel) {

const warningText =
  typeof usl.extrapolation_warning === 'string'
    ? usl.extrapolation_warning
    : 'Model estimates extend beyond the measured load range.';


if (
  extrapolatedPoints.length > 0
) {

  warningPanel.hidden = false;


  setText(
    'uslExtrapolationWarning',
    warningText
  );

} else {

  warningPanel.hidden = true;

}

}

/* ==========================================================
Summary
========================================================== */

setText(
'uslSummaryClassification',
classificationName
);

const optimalUsers =
finiteNumber(metrics.optimal_users);

setText(
'uslSummaryOptimal',
optimalUsers === null
? 'Unavailable'
: `${formatInteger(optimalUsers)} users`
);

const peakThroughput =
finiteNumber(metrics.peak_throughput);

setText(
'uslSummaryPeak',
peakThroughput === null
? 'Unavailable'
: `${formatNumber(peakThroughput, 2)} req/s`
);

setText(
'uslSummaryReliability',
reliabilityStatus === 'unknown'
? 'Unknown'
: reliabilityStatus.charAt(0).toUpperCase()
+ reliabilityStatus.slice(1)
);

/* ==========================================================
Chart data
========================================================== */

const measuredRows =
safeLevels

  .map((level) => ({

    users:
      finiteNumber(level.users),

    throughput:
      finiteNumber(level.mean_throughput)

  }))

  .filter(
    (row) =>
      row.users !== null
      && row.throughput !== null
  )

  .sort(
    (a, b) =>
      a.users - b.users
  );

const fittedRows =
observedEntries

  .map((row) => ({

    users:
      row.users,

    throughput:
      row.predicted

  }))

  .filter(
    (row) =>
      row.users !== null
      && row.throughput !== null
  )

  .sort(
    (a, b) =>
      a.users - b.users
  );

const futureRows =
predictionEntries

  .map((row) => ({

    users:
      row.users,

    throughput:
      row.throughput,

    confidence:
      row.confidence

  }))

  .filter(
    (row) =>
      row.users !== null
      && row.throughput !== null
  )

  .sort(
    (a, b) =>
      a.users - b.users
  );

/*

* The chart uses only actual measured points and actual backend
* model outputs.
*
* It does not create intermediate predictions.
  */

const chartUsers =
Array.from(
new Set(

    measuredRows
      .map((row) => row.users)

      .concat(
        futureRows.map(
          (row) => row.users
        )
      )

  )
)
.sort(
  (a, b) => a - b
);

const chartLabels =
chartUsers.map(
(users) =>
formatInteger(users)
);

/* ==========================================================
Measured data
========================================================== */

const measuredChartData =
chartUsers.map((users) => {

  const row =
    measuredRows.find(
      (item) =>
        item.users === users
    );


  return row
    ? row.throughput
    : null;

});

/* ==========================================================
Model data
========================================================== */

const modelChartData =
chartUsers.map((users) => {

  const fitted =
    fittedRows.find(
      (item) =>
        item.users === users
    );


  if (fitted) {

    return fitted.throughput;

  }


  const future =
    futureRows.find(
      (item) =>
        item.users === users
    );


  return future
    ? future.throughput
    : null;

});

/* ==========================================================
Confidence interval
========================================================== */

const lowerConfidenceData =
chartUsers.map((users) => {

  const future =
    futureRows.find(
      (item) =>
        item.users === users
    );


  if (
    future
    && future.confidence
    && finiteNumber(
      future.confidence.lower
    ) !== null
  ) {

    return finiteNumber(
      future.confidence.lower
    );

  }


  return null;

});

const upperConfidenceData =
chartUsers.map((users) => {

  const future =
    futureRows.find(
      (item) =>
        item.users === users
    );


  if (
    future
    && future.confidence
    && finiteNumber(
      future.confidence.upper
    ) !== null
  ) {

    return finiteNumber(
      future.confidence.upper
    );

  }


  return null;

});

/* ==========================================================
Chart
========================================================== */

makeOrUpdateChart(

'usl',

'uslChart',

{

  type: 'line',

  data: {

    labels: chartLabels,

    datasets: [

      /*
       * Actual measurements.
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

          tension: 0

        }

      ),


      /*
       * Backend USL model values.
       *
       * No artificial smoothing is applied.
       */

      Object.assign(

        predictedDataset(
          'USL model',
          modelChartData
        ),

        {

          spanGaps: false,

          pointRadius: 3,

          pointHoverRadius: 6,

          tension: 0

        }

      ),


      /*
       * Lower model interval.
       */

      Object.assign(

        predictedDataset(
          '95% model interval — lower',
          lowerConfidenceData
        ),

        {

          borderDash: [4, 5],

          pointRadius: 0,

          tension: 0,

          spanGaps: false

        }

      ),


      /*
       * Upper model interval.
       */

      Object.assign(

        predictedDataset(
          '95% model interval — upper',
          upperConfidenceData
        ),

        {

          borderDash: [4, 5],

          pointRadius: 0,

          tension: 0,

          spanGaps: false,

          fill: '-1',

          backgroundColor:
            'rgba(255,255,255,0.04)'

        }

      )

    ]

  },


  options:

    baseChartOptions({

      plugins: {

        legend: {

          display: true,

          labels: {

            color:
              cssVar('--ink-muted'),

            font: {

              family: 'Inter',

              size: 11.5

            }

          }

        },

        tooltip:
          baseChartOptions()
            .plugins
            .tooltip

      },


      scales: {

        x: Object.assign(

          {},

          baseChartOptions()
            .scales
            .x,

          {

            title: {

              display: true,

              text:
                'Concurrent users',

              color:
                cssVar('--ink-muted')

            }

          }

        ),


        y: Object.assign(

          {},

          baseChartOptions()
            .scales
            .y,

          {

            title: {

              display: true,

              text:
                'Throughput (req/s)',

              color:
                cssVar('--ink-muted')

            }

          }

        )

      }

    })

}

);

}

/* ---- Little's Law -------------------------------------------- */

function renderLittlesLaw(littleLaw, levels) {

  /*
   * Little's Law frontend renderer
   *
   * Core relationship:
   *
   *     L = λ × W
   *
   * where:
   *
   *     L = average number of requests/jobs in the system
   *     λ = measured throughput in requests per second
   *     W = average response time in seconds
   *
   * For a closed workload:
   *
   *     N = X × (R + Z)
   *
   * where:
   *
   *     N = derived closed-workload concurrency
   *     X = throughput
   *     R = response time
   *     Z = think time
   *
   * In the current model:
   *
   *     Z = 0
   *
   * Therefore:
   *
   *     N = X × R
   *       = L
   *
   * Important:
   *
   *     L is NOT application capacity.
   *     Derived closed-workload N is NOT safe user capacity.
   *
   * Capacity decisions are made later using the combined
   * mathematical engine:
   *
   *     USL
   *     Little's Law
   *     Queueing
   *     Bottleneck
   *     SLO
   *     Capacity
   *     Scalability
   *
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
      'No Little’s Law occupancy information is available.'
    );

    setText(
      'littleLawResponseDescription',
      'No response-time information is available.'
    );

    setText(
      'littleLawArrivalDescription',
      'No throughput information is available.'
    );

    setText(
      'littleLawLInterpretation',
      'No Little’s Law result is available.'
    );

    setText(
      'littleLawLambdaInterpretation',
      'No throughput result is available.'
    );

    setText(
      'littleLawWInterpretation',
      'No response-time result is available.'
    );

    setText(
      'littleLawHighOccupancy',
      '—'
    );

    setText(
      'littleLawCriticalOccupancy',
      '—'
    );

    const body = qs('#littlesLawTableBody');

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


        /* ---------------------------------------------------- */
        /* Load level / concurrent users                        */
        /* ---------------------------------------------------- */

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


        /* ---------------------------------------------------- */
        /* Throughput                                            */
        /* ---------------------------------------------------- */

        /*
         * Current backend may expose:
         *
         *     throughput
         *     arrival_rate
         *     lambda
         *
         * The application currently feeds measured Locust
         * throughput into Little's Law.
         */

        const arrivalRate =
          Number(
            r.throughput ??
            r.arrival_rate ??
            r.lambda
          );


        /* ---------------------------------------------------- */
        /* Average response time                                */
        /* ---------------------------------------------------- */

        /*
         * Backend response time is normalized to seconds
         * by the mathematical engine.
         */

        const responseTime =
          Number(
            r.response_time ??
            r.average_response_time
          );


        /* ---------------------------------------------------- */
        /* Little's Law L                                       */
        /* ---------------------------------------------------- */

        /*
         * Backend key:
         *
         *     requests_in_system
         *
         * Fallbacks are retained for compatibility.
         */

        const requestsInSystem =
          Number(
            r.requests_in_system ??
            r.concurrent_requests ??
            r.L
          );


        /* ---------------------------------------------------- */
        /* Derived closed-workload N                             */
        /* ---------------------------------------------------- */

        /*
         * Current mathematical engine uses:
         *
         *     predicted_users
         *
         * This value represents:
         *
         *     N = X × (R + Z)
         *
         * With the current assumption Z = 0:
         *
         *     N = X × R = L
         *
         * It is NOT application capacity.
         */

        const derivedClosedWorkloadN =
          Number(
            r.predicted_closed_concurrency ??
            r.predicted_users
          );


        /* ---------------------------------------------------- */
        /* Observed concurrency                                 */
        /* ---------------------------------------------------- */

        const observedConcurrency =
          Number(
            r.observed_concurrency ??
            users
          );


        /* ---------------------------------------------------- */
        /* Classification                                      */
        /* ---------------------------------------------------- */

        /*
         * Classification describes workload/occupancy behavior.
         *
         * It must NOT be interpreted as:
         *
         *     safe capacity
         *     maximum users
         *     overload limit
         */

        const classification =
          r.load_classification ??
          r.classification ??
          '—';


        /* ---------------------------------------------------- */
        /* Per-load-level signals                               */
        /* ---------------------------------------------------- */

        const signals =
          r.signals || {};


        return {

          original: r,

          index,

          users,

          observedConcurrency,

          arrivalRate,

          responseTime,

          requestsInSystem,

          derivedClosedWorkloadN,

          classification,

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

          queueGrowth:
            signals.queue_growth

        };

      })
      .filter(
        (row) =>
          Number.isFinite(row.users) ||
          Number.isFinite(row.arrivalRate) ||
          Number.isFinite(row.requestsInSystem)
      );


  if (!normalizedRows.length) {
    return;
  }


  /* ---------------------------------------------------------- */
  /* Sort by workload                                           */
  /* ---------------------------------------------------------- */

  normalizedRows.sort(
    (a, b) => {

      if (
        Number.isFinite(a.users) &&
        Number.isFinite(b.users)
      ) {

        return a.users - b.users;

      }

      return a.index - b.index;

    }
  );


  /* ---------------------------------------------------------- */
  /* Summary values                                             */
  /* ---------------------------------------------------------- */

  const validArrivalRates =
    normalizedRows
      .map(row => row.arrivalRate)
      .filter(Number.isFinite);


  const validResponseTimes =
    normalizedRows
      .map(row => row.responseTime)
      .filter(Number.isFinite);


  const validLValues =
    normalizedRows
      .map(row => row.requestsInSystem)
      .filter(Number.isFinite);


  const validDerivedNValues =
    normalizedRows
      .map(row => row.derivedClosedWorkloadN)
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


  const peakDerivedN =
    validDerivedNValues.length
      ? Math.max(...validDerivedNValues)
      : null;


  const highestUsers =
    normalizedRows
      .map(row => row.users)
      .filter(Number.isFinite)
      .reduce(
        (max, value) => Math.max(max, value),
        -Infinity
      );


  /* ---------------------------------------------------------- */
  /* Top metric cards                                           */
  /* ---------------------------------------------------------- */

  setText(
    'littleLawPeakArrival',
    Number.isFinite(peakArrival)
      ? `${fmt(peakArrival, 2)} req/s`
      : '—'
  );


  setText(
    'littleLawPeakResponse',
    Number.isFinite(peakResponse)
      ? `${fmt(peakResponse, 4)} s`
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


  /* ---------------------------------------------------------- */
  /* Formula values                                             */
  /* ---------------------------------------------------------- */

  /*
   * These use the peak measured values so the existing HTML
   * cards remain compatible.
   */

  setText(
    'littleLawLambda',
    Number.isFinite(peakArrival)
      ? `${fmt(peakArrival, 2)} req/s`
      : '—'
  );


  setText(
    'littleLawW',
    Number.isFinite(peakResponse)
      ? `${fmt(peakResponse, 4)} s`
      : '—'
  );


  setText(
    'littleLawL',
    Number.isFinite(peakL)
      ? `${fmt(peakL, 2)} requests`
      : '—'
  );


  setText(
    'littleLawUsers',
    Number.isFinite(highestUsers)
      ? `${fmtInt(highestUsers)} users`
      : '—'
  );


  setText(
    'littleLawMaxL',
    Number.isFinite(peakL)
      ? `${fmt(peakL, 2)} requests`
      : '—'
  );


  /* ---------------------------------------------------------- */
  /* Overall status                                             */
  /* ---------------------------------------------------------- */

  /*
   * Little's Law classification is descriptive only.
   *
   * It is NOT a capacity verdict.
   */

  const lastRow =
    normalizedRows[normalizedRows.length - 1];


  const overallClassification =
    lastRow?.classification || 'Measured';


  let overallVariant = 'neutral';


  const classificationLower =
    String(overallClassification).toLowerCase();


  if (
    classificationLower.includes('critical') ||
    classificationLower.includes('heavy')
  ) {

    overallVariant = 'danger';

  } else if (
    classificationLower.includes('moderate')
  ) {

    overallVariant = 'warning';

  } else {

    overallVariant = 'neutral';

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
  /* Overall trends                                             */
  /* ---------------------------------------------------------- */

  const occupancyTrend =
    lastRow?.occupancyTrend;

  const responseTrend =
    lastRow?.responseTimeTrend;

  const arrivalTrend =
    lastRow?.arrivalRateTrend;

  const queueGrowth =
    lastRow?.queueGrowth;


  /* ---------------------------------------------------------- */
  /* Occupancy trend                                            */
  /* ---------------------------------------------------------- */

  setBadge(
    'littleLawOccupancyTrend',
    occupancyTrend || 'No trend',
    occupancyTrend === 'increasing'
      ? 'warning'
      : occupancyTrend === 'decreasing'
        ? 'neutral'
        : 'neutral'
  );


  let occupancyDescription =
    'No occupancy trend was reported.';


  if (occupancyTrend === 'increasing') {

    occupancyDescription =
      'The number of requests remaining in the system is increasing as workload rises. This indicates growing system occupancy and should be considered together with response time and throughput.';

  } else if (occupancyTrend === 'decreasing') {

    occupancyDescription =
      'The number of requests remaining in the system decreased at the latest measured load level.';

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
    responseTrend || 'No trend',
    responseTrend === 'increasing'
      ? 'warning'
      : responseTrend === 'decreasing'
        ? 'neutral'
        : 'neutral'
  );


  let responseDescription =
    'No response-time trend was reported.';


  if (responseTrend === 'increasing') {

    responseDescription =
      'Average response time is increasing as workload rises, indicating that requests are taking longer to complete at higher load.';

  } else if (responseTrend === 'decreasing') {

    responseDescription =
      'Average response time decreased at the latest measured load level.';

  } else if (responseTrend) {

    responseDescription =
      `The latest measured response-time trend is ${responseTrend}.`;

  }


  setText(
    'littleLawResponseDescription',
    responseDescription
  );


  /* ---------------------------------------------------------- */
  /* Throughput trend                                           */
  /* ---------------------------------------------------------- */

  setBadge(
    'littleLawArrivalTrend',
    arrivalTrend || 'No trend',
    arrivalTrend === 'increasing'
      ? 'neutral'
      : arrivalTrend === 'decreasing'
        ? 'warning'
        : 'neutral'
  );


  let arrivalDescription =
    'No throughput trend was reported.';


  if (arrivalTrend === 'increasing') {

    arrivalDescription =
      'Throughput is still increasing at the latest measured load level.';

  } else if (arrivalTrend === 'decreasing') {

    arrivalDescription =
      'Throughput decreased at the latest measured load level. Combined with rising response time, this can indicate increasing contention or saturation and should be evaluated by the other prediction models.';

  } else if (arrivalTrend) {

    arrivalDescription =
      `The latest measured throughput trend is ${arrivalTrend}.`;

  }


  setText(
    'littleLawArrivalDescription',
    arrivalDescription
  );


  /* ---------------------------------------------------------- */
  /* Occupancy signals                                          */
  /* ---------------------------------------------------------- */

  const highOccupancyRows =
    normalizedRows.filter(
      row => row.highOccupancy === true
    );


  const criticalOccupancyRows =
    normalizedRows.filter(
      row => row.criticalOccupancy === true
    );


  const highOccupancy =
    highOccupancyRows.length > 0;


  const criticalOccupancy =
    criticalOccupancyRows.length > 0;


  setText(
    'littleLawHighOccupancy',
    highOccupancy
      ? 'Yes'
      : 'No'
  );


  setText(
    'littleLawCriticalOccupancy',
    criticalOccupancy
      ? 'Yes'
      : 'No'
  );


  /* ---------------------------------------------------------- */
  /* Table                                                       */
  /* ---------------------------------------------------------- */

  const body =
    qs('#littlesLawTableBody');


  if (body) {

    body.innerHTML =
      normalizedRows
        .map(row => {

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

            occupancyVariant = 'neutral';

          }


          /* ---------------------------------------------- */
          /* Classification is descriptive only             */
          /* ---------------------------------------------- */

          let classificationVariant =
            'neutral';


          const cls =
            String(row.classification)
              .toLowerCase();


          if (
            cls.includes('critical') ||
            cls.includes('heavy')
          ) {

            classificationVariant = 'danger';

          } else if (
            cls.includes('moderate')
          ) {

            classificationVariant = 'warning';

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
  /* Interpretation                                             */
  /* ---------------------------------------------------------- */

  /*
   * Find the load level with the highest measured L.
   *
   * This is useful for explaining Little's Law.
   *
   * It is NOT treated as application capacity.
   */

  const peakRow =
    normalizedRows.reduce(
      (best, row) => {

        if (!best) {
          return row;
        }


        if (
          Number.isFinite(row.requestsInSystem) &&
          Number.isFinite(best.requestsInSystem) &&
          row.requestsInSystem > best.requestsInSystem
        ) {

          return row;

        }


        return best;

      },
      null
    );


  if (peakRow) {

    /* -------------------------------------------------------- */
    /* L interpretation                                         */
    /* -------------------------------------------------------- */

    setText(
      'littleLawLInterpretation',

      Number.isFinite(peakRow.requestsInSystem)

        ? `At ${
            Number.isFinite(peakRow.users)
              ? fmtInt(peakRow.users)
              : 'the measured'
          } concurrent users, Little's Law estimates an average of ${
            fmt(
              peakRow.requestsInSystem,
              2
            )
          } requests in the system.`

        : 'Requests-in-system information is unavailable.'
    );


    /* -------------------------------------------------------- */
    /* Lambda interpretation                                    */
    /* -------------------------------------------------------- */

    setText(
      'littleLawLambdaInterpretation',

      Number.isFinite(peakRow.arrivalRate)

        ? `The corresponding measured throughput was ${
            fmt(
              peakRow.arrivalRate,
              2
            )
          } requests per second.`

        : 'Throughput information is unavailable.'
    );


    /* -------------------------------------------------------- */
    /* W interpretation                                         */
    /* -------------------------------------------------------- */

    setText(
      'littleLawWInterpretation',

      Number.isFinite(peakRow.responseTime)

        ? `The corresponding average response time was ${
            fmt(
              peakRow.responseTime,
              4
            )
          } seconds (${
            fmt(
              peakRow.responseTime * 1000,
              1
            )
          } ms).`

        : 'Response-time information is unavailable.'
    );

  }


  /* ---------------------------------------------------------- */
  /* Closed-workload interpretation                             */
  /* ---------------------------------------------------------- */

  /*
   * Current backend assumption:
   *
   *     Z = 0 seconds
   *
   * Therefore:
   *
   *     N = X × (R + Z)
   *       = X × R
   *       = L
   *
   * The derived N value is therefore numerically equal to L
   * under the current zero-think-time assumption.
   *
   * IMPORTANT:
   *
   *     N is NOT safe capacity.
   *     N is NOT maximum users.
   *     N is NOT an overload threshold.
   *
   * It is simply the concurrency quantity derived from the
   * closed-workload form of Little's Law.
   */

  const closedRows =
    normalizedRows.filter(
      row =>
        Number.isFinite(
          row.derivedClosedWorkloadN
        )
    );


  /*
   * The current HTML does not contain a dynamic element for
   * displaying derived N, so we intentionally do not inject
   * another value into the page.
   *
   * The mathematical engine still provides the value through:
   *
   *     row.derivedClosedWorkloadN
   *
   * if another component needs it later.
   */


  /* ---------------------------------------------------------- */
  /* Queue-growth context                                       */
  /* ---------------------------------------------------------- */

  if (queueGrowth === 'high_growth') {

    /*
     * Do not turn this into a capacity verdict.
     *
     * It is simply a signal for the other models.
     */

    if (
      occupancyTrend === 'increasing' &&
      responseTrend === 'increasing'
    ) {

      setText(
        'littleLawOccupancyDescription',

        'Requests remaining in the system are increasing while response time is also rising at the latest load level. This is a performance warning signal and should be evaluated with queueing, bottleneck, and SLO analysis.'
      );

    }

  }


  /* ---------------------------------------------------------- */
  /* Chart                                                       */
  /* ---------------------------------------------------------- */

  const chartRows =
    normalizedRows.filter(
      row =>
        Number.isFinite(row.users) &&
        Number.isFinite(row.requestsInSystem)
    );


  if (!chartRows.length) {
    return;
  }


  const labels =
    chartRows.map(
      row => fmtInt(row.users)
    );


  const occupancyData =
    chartRows.map(
      row => row.requestsInSystem
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
              spanGaps: false
            }
          )

        ]

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
                  size: 11.5
                }
              }
            },

            tooltip:
              baseChartOptions().plugins.tooltip

          },


          scales: {

            x: Object.assign(
              {},
              baseChartOptions().scales.x,
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
              baseChartOptions().scales.y,
              {

                title: {
                  display: true,
                  text: 'Average requests in system (L)',
                  color: cssVar('--ink-muted')
                }

              }
            )

          }

        })

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


/* ---- Forced Flow Law --------------------------------------- */

function renderForcedFlow(forcedFlow) {
  const body = qs('#forcedFlowTableBody');

  const emptyRow = (msg) => {
    if (body) {
      body.innerHTML = `
        <tr>
          <td colspan="7" class="data-table__empty">
            ${escapeHtml(msg)}
          </td>
        </tr>
      `;
    }
  };

  /* ----------------------------------------------------------
     Reset summary values
     ---------------------------------------------------------- */

  setText('forcedFlowSystemThroughput', '—');
  setText('forcedFlowComponentCount', '—');
  setText('forcedFlowDominantComponent', '—');

  setBadge(
    'forcedFlowAvailabilityBadge',
    'Unavailable',
    'neutral'
  );

  setBadge(
    'forcedFlowDominantBadge',
    '—',
    'neutral'
  );

  /* ----------------------------------------------------------
     Reset ranking
     ---------------------------------------------------------- */

  const rankingEl = qs('#forcedFlowRanking');

  if (rankingEl) {
    rankingEl.innerHTML = `
      <p class="empty-note">
        No component ranking available yet.
      </p>
    `;
  }

  /* ----------------------------------------------------------
     Handle unavailable / invalid result
     ---------------------------------------------------------- */

  if (!forcedFlow || forcedFlow.available !== true) {

    const reason =
      (forcedFlow && forcedFlow.reason)
      || 'No Forced Flow instrumentation was supplied for this run.';

    setText('forcedFlowReason', reason);

    emptyRow(
      'No component instrumentation available for this run.'
    );

    renderNoteList(
      '#forcedFlowNotes',
      null,
      reason
    );

    return;
  }

  /* ----------------------------------------------------------
     Available
     ---------------------------------------------------------- */

  setBadge(
    'forcedFlowAvailabilityBadge',
    'Available',
    'success'
  );

  setText(
    'forcedFlowReason',
    'Component-level instrumentation was supplied and analyzed.'
  );

  /* ----------------------------------------------------------
     System throughput
     ---------------------------------------------------------- */

  const systemThroughput = forcedFlow.system_throughput;

  setText(
    'forcedFlowSystemThroughput',
    Number.isFinite(Number(systemThroughput))
      ? `${fmt(systemThroughput, 2)} /s`
      : '—'
  );

  /* ----------------------------------------------------------
     Component data
     ---------------------------------------------------------- */

  const perComponent =
    forcedFlow.per_component &&
    typeof forcedFlow.per_component === 'object'
      ? forcedFlow.per_component
      : {};

  const componentNames = Object.keys(perComponent);

  setText(
    'forcedFlowComponentCount',
    String(componentNames.length)
  );

  /* ----------------------------------------------------------
     Ranking
     ---------------------------------------------------------- */

  const ranked =
    Array.isArray(forcedFlow.ranked_by_demand)
      ? forcedFlow.ranked_by_demand.filter(
          (name) => Object.prototype.hasOwnProperty.call(
            perComponent,
            name
          )
        )
      : componentNames;

  const dominant =
    forcedFlow.dominant_component &&
    Object.prototype.hasOwnProperty.call(
      perComponent,
      forcedFlow.dominant_component
    )
      ? forcedFlow.dominant_component
      : (ranked.length ? ranked[0] : null);

  if (dominant) {

    setBadge(
      'forcedFlowDominantBadge',
      formatSignalName(dominant),
      'accent'
    );

    setText(
      'forcedFlowDominantComponent',
      formatSignalName(dominant)
    );

  } else {

    setBadge(
      'forcedFlowDominantBadge',
      '—',
      'neutral'
    );

    setText(
      'forcedFlowDominantComponent',
      '—'
    );
  }

  /* ----------------------------------------------------------
     No usable components
     ---------------------------------------------------------- */

  if (!ranked.length) {

    emptyRow(
      'No component had usable instrumentation data.'
    );

    renderNoteList(
      '#forcedFlowNotes',
      forcedFlow.notes || [],
      'No component-level analysis notes.'
    );

    return;
  }

  /* ----------------------------------------------------------
     Per-component table
     ---------------------------------------------------------- */

  if (body) {

    body.innerHTML = ranked.map((name) => {

      const info = perComponent[name] || {};
      const comparison = info.throughput_comparison;

      /* ---- Visits / request ---- */

      const visits =
        Number.isFinite(Number(info.visits_per_request))
          ? fmt(info.visits_per_request, 2)
          : '—';

      /* ---- Service time ---- */

      const serviceTime =
        Number.isFinite(Number(info.service_time_seconds))
          ? `${fmt(
              Number(info.service_time_seconds) * 1000,
              2
            )} ms`
          : '—';

      /* ---- Component throughput ---- */

      const componentThroughput =
        Number.isFinite(Number(info.component_throughput))
          ? `${fmt(info.component_throughput, 2)} /s`
          : '—';

      /* ---- Service demand ---- */

      const serviceDemand =
        Number.isFinite(Number(info.service_demand))
          ? `${fmt(
              Number(info.service_demand) * 1000,
              3
            )} ms`
          : '—';

      /* ---- Observed throughput ---- */

      const observedThroughput =
        comparison &&
        Number.isFinite(Number(comparison.observed_throughput))
          ? `${fmt(
              comparison.observed_throughput,
              2
            )} /s`
          : '<span class="muted">Not measured</span>';

      /* ---- Comparison ---- */

      let comparisonCell =
        '<span class="muted">Not measured</span>';

      if (comparison) {

        const rel = comparison.relative_difference;

        const relText =
          rel === null ||
          rel === undefined ||
          !Number.isFinite(Number(rel))
            ? '—'
            : fmtPercent(Number(rel) * 100, 1);

        if (comparison.consistent === true) {

          comparisonCell = `
            <span class="badge badge--success">
              Consistent
            </span>
            <span class="mono">
              ${relText}
            </span>
          `;

        } else if (comparison.consistent === false) {

          comparisonCell = `
            <span class="badge badge--warn">
              Diverges
            </span>
            <span class="mono">
              ${relText}
            </span>
          `;

        } else {

          comparisonCell = `
            <span class="mono">
              ${relText}
            </span>
          `;
        }
      }

      return `
        <tr>
          <td>
            <strong>
              ${escapeHtml(formatSignalName(name))}
            </strong>
          </td>

          <td class="mono">
            ${visits}
          </td>

          <td class="mono">
            ${serviceTime}
          </td>

          <td class="mono">
            ${componentThroughput}
          </td>

          <td class="mono">
            ${serviceDemand}
          </td>

          <td class="mono">
            ${observedThroughput}
          </td>

          <td>
            ${comparisonCell}
          </td>
        </tr>
      `;
    }).join('');
  }

  /* ----------------------------------------------------------
     Ranking display
     ---------------------------------------------------------- */

  if (rankingEl) {

    rankingEl.innerHTML = ranked.map((name, index) => {

      const info = perComponent[name] || {};

      const demand =
        Number.isFinite(Number(info.service_demand))
          ? `${fmt(
              Number(info.service_demand) * 1000,
              3
            )} ms/request`
          : '—';

      const label =
        index === 0
          ? 'Highest demand'
          : `Rank ${index + 1}`;

      return `
        <div class="note-item">
          <strong>
            ${index + 1}. ${escapeHtml(
              formatSignalName(name)
            )}
          </strong>

          <span class="muted">
            ${label} · ${demand}
          </span>
        </div>
      `;

    }).join('');
  }

  /* ----------------------------------------------------------
     Notes + throughput comparison notes
     ---------------------------------------------------------- */

  const comparisonNotes = ranked
    .map((name) => {

      const comparison =
        (perComponent[name] || {}).throughput_comparison;

      if (
        comparison &&
        comparison.note
      ) {
        return `${formatSignalName(name)}: ${comparison.note}`;
      }

      return null;

    })
    .filter(Boolean);

  const notes = [
    ...(Array.isArray(forcedFlow.notes)
      ? forcedFlow.notes
      : []),
    ...comparisonNotes
  ];

  renderNoteList(
    '#forcedFlowNotes',
    notes,
    'No Forced Flow notes for this run.'
  );
}


  /* ---- Amdahl's Law ------------------------------------------ */

  function renderAmdahl(amdahl) {
    const body = qs('#amdahlTableBody');
    const emptyRow = (msg) => {
      if (body) body.innerHTML = `<tr><td colspan="5" class="data-table__empty">${escapeHtml(msg)}</td></tr>`;
    };

    if (!amdahl || amdahl.available === false) {
      const reason = (amdahl && amdahl.reason)
        || 'Amdahl analysis requires bottleneck service-demand data, which was not available.';
      setBadge('amdahlBottleneckBadge', 'Unavailable', 'neutral');
      ['amdahlCurrentThroughput', 'amdahlMaxSpeedup', 'amdahlImpliedThroughput', 'amdahlNextBottleneck']
        .forEach((id) => setText(id, '—'));
      emptyRow('No Amdahl analysis available for this run.');
      renderNoteList('#amdahlNotes', null, reason);
      return;
    }

    const bottleneckName = amdahl.bottleneck_resource || amdahl.bottleneck || '—';
    setBadge(
      'amdahlBottleneckBadge',
      bottleneckName !== '—' ? String(bottleneckName).toUpperCase() : '—',
      bottleneckName !== '—' ? 'accent' : 'neutral'
    );

    setText('amdahlCurrentThroughput', fmt(amdahl.current_throughput, 2));

    const maxSpeedup = amdahl.max_speedup ?? amdahl.maximum_speedup;
    setText('amdahlMaxSpeedup', maxSpeedup === null || maxSpeedup === undefined ? '—' : `${fmt(maxSpeedup, 2)}×`);
    setText('amdahlImpliedThroughput', fmt(amdahl.max_throughput ?? amdahl.implied_max_throughput, 2));

    const next = amdahl.next_bottleneck;
    if (next && typeof next === 'object') {
      const demand = next.service_demand;
      setText(
        'amdahlNextBottleneck',
        `${formatSignalName(next.resource || '—')}${
          demand === null || demand === undefined ? '' : ` · ${fmt(demand * 1000, 3)} ms`
        }`
      );
    } else {
      setText('amdahlNextBottleneck', next ? formatSignalName(String(next)) : '—');
    }

    // per_resource may arrive as a dict keyed by resource name or as a list.
    const perResourceRaw = amdahl.per_resource || amdahl.resources || {};
    const rows = Array.isArray(perResourceRaw)
      ? perResourceRaw
      : Object.entries(perResourceRaw).map(([name, info]) =>
          Object.assign({ resource: name }, info && typeof info === 'object' ? info : {}));

    if (!rows.length) {
      emptyRow('No per-resource optimization ceilings were computed.');
    } else if (body) {
      const sorted = rows.slice().sort((a, b) =>
        Number(b.max_speedup ?? b.maximum_speedup ?? 0) - Number(a.max_speedup ?? a.maximum_speedup ?? 0));

      body.innerHTML = sorted.map((row) => {
        const fraction = row.fraction ?? row.resource_fraction;
        const speedup = row.max_speedup ?? row.maximum_speedup;
        return `
          <tr>
            <td>${escapeHtml(formatSignalName(row.resource || '—'))}</td>
            <td class="mono">${row.service_demand === null || row.service_demand === undefined
              ? '—' : `${fmt(row.service_demand * 1000, 3)} ms`}</td>
            <td class="mono">${fraction === null || fraction === undefined ? '—' : fmtPercent(fraction * 100, 1)}</td>
            <td class="mono">${speedup === null || speedup === undefined ? '—' : `${fmt(speedup, 2)}×`}</td>
            <td class="mono">${fmt(row.max_throughput ?? row.implied_max_throughput, 2)}</td>
          </tr>`;
      }).join('');
    }

    renderNoteList('#amdahlNotes', amdahl.notes, 'No Amdahl notes for this run.');
  }

  /* ---- SLO capacity ------------------------------------------ */

  function renderSLO(slo) {
    const body = qs('#sloTableBody');
    const emptyRow = (msg) => {
      if (body) body.innerHTML = `<tr><td colspan="5" class="data-table__empty">${escapeHtml(msg)}</td></tr>`;
    };

    if (!slo || slo.success === false || !slo.evaluations) {
      const reason = (slo && slo.error) || 'SLO analysis did not run for this experiment.';
      ['slo-capacity-observed', 'slo-capacity-overall', 'slo-first-failure-observed', 'slo-first-failure-overall']
        .forEach((id) => setText(id, '—'));
      setText('slo-max-response-time', '—');
      setText('slo-max-error-rate', '—');
      emptyRow('No SLO analysis available yet.');
      renderNoteList('#sloNotes', null, reason);
      return;
    }

    const thresholds = slo.thresholds || {};
    setText('slo-max-response-time', thresholds.max_response_time_seconds === undefined
      ? '—' : `${fmt(thresholds.max_response_time_seconds * 1000, 0)} ms`);
    setText('slo-max-error-rate', thresholds.max_error_rate_percent === undefined
      ? '—' : fmtPercent(thresholds.max_error_rate_percent, 2));

    // A null capacity means even the lowest level failed the budget — which
    // is a real finding, not missing data, so it reads "None" rather than "—".
    const capText = (value) => (value === null || value === undefined ? 'None' : fmtInt(value));
    setText('slo-capacity-observed', capText(slo.slo_capacity_observed));
    setText('slo-capacity-overall', capText(slo.slo_capacity_overall));

    // first_failure being null means nothing failed, up to the highest level.
    const failText = (value) => (value === null || value === undefined ? 'None' : fmtInt(value));
    setText('slo-first-failure-observed', failText(slo.first_failure_observed_at));
    setText('slo-first-failure-overall', failText(slo.first_failure_overall_at));

    const evaluations = slo.evaluations || [];
    if (!evaluations.length) {
      emptyRow('No load levels were evaluated against the SLO.');
    } else if (body) {
      body.innerHTML = evaluations.map((ev) => {
        const sourceBadge = ev.source === 'predicted'
          ? '<span class="badge badge--neutral">Predicted</span>'
          : '<span class="badge badge--accent">Observed</span>';

        const rtClass = ev.response_time_ok ? '' : ' is-breach';
        const errClass = ev.error_rate_ok ? '' : ' is-breach';

        const verdict = ev.passed
          ? '<span class="badge badge--success">PASS</span>'
          : '<span class="badge badge--danger">FAIL</span>';

        return `
          <tr>
            <td class="mono">${fmtInt(ev.users)}</td>
            <td>${sourceBadge}</td>
            <td class="mono${rtClass}">${fmt(ev.response_time * 1000, 1)} ms</td>
            <td class="mono${errClass}">${fmtPercent(ev.error_rate_percent, 2)}</td>
            <td>${verdict}</td>
          </tr>`;
      }).join('');
    }

    renderNoteList('#sloNotes', slo.notes, 'No SLO notes for this run.');
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
/* ============================================================
   DATABASE / CACHE MONITORING
   ============================================================ */

/*
 * Database monitoring is now automatic.
 *
 * The backend:
 *   1. detects the database dependency,
 *   2. resolves its runtime configuration,
 *   3. starts DBMonitor,
 *   4. runs Locust,
 *   5. stops DBMonitor,
 *   6. returns database_metrics.
 *
 * The frontend never receives or sends database passwords.
 */

function resetDatabaseMonitoringView() {
  setBadge('dbMonitorStatusBadge', 'Waiting', 'neutral');

  setText('dbMonitorId', 'Automatic');
  setText('dbMonitorActiveConn', '—');
  setText('dbMonitorMaxConn', '—');
  setText('dbMonitorPoolUtil', '—');
  setText('dbMonitorSampleCount', '—');
  setText('dbMonitorError', '—');

  const engineEl = qs('#dbMonitorDetectedEngine');
  const sourceEl = qs('#dbMonitorDetectedSource');
  const hostEl = qs('#dbMonitorDetectedHost');

  if (engineEl) engineEl.textContent = '—';
  if (sourceEl) sourceEl.textContent = '—';
  if (hostEl) hostEl.textContent = '—';
}


function setDatabaseMonitoringState(stateName, text) {
  setBadge('dbMonitorStatusBadge', text, stateName);
}


function renderDatabaseMonitoring(result) {
  /*
   * The backend returns two related objects:
   *
   * database_monitoring:
   *   safe information about whether monitoring was available.
   *
   * database_metrics:
   *   actual metrics collected during the load test.
   */

  resetDatabaseMonitoringView();

  if (!result) {
    setDatabaseMonitoringState('neutral', 'No data');
    return;
  }

  const monitorInfo = result.database_monitoring || null;
  const metrics = result.database_metrics || null;

  // ------------------------------------------------------------
  // Show detected/monitorable database information
  // ------------------------------------------------------------

  if (monitorInfo) {
    const engine = monitorInfo.engine || '—';
    const source = Array.isArray(monitorInfo.source)
      ? monitorInfo.source.join(', ')
      : (monitorInfo.source || '—');

    setText(
      'dbMonitorDetectedEngine',
      engine === '—' ? '—' : capitalizeFirst(engine)
    );

    setText('dbMonitorDetectedSource', source);

    /*
     * Host is safe to display because it is not a credential.
     * Do not display username/password/database credentials.
     */
    setText(
      'dbMonitorDetectedHost',
      monitorInfo.host
        ? `${monitorInfo.host}${monitorInfo.port ? `:${monitorInfo.port}` : ''}`
        : '—'
    );
  }

  // ------------------------------------------------------------
  // No DB monitoring available
  // ------------------------------------------------------------

  if (!metrics || metrics.available === false) {
    const reason =
      (metrics && metrics.error) ||
      (monitorInfo && monitorInfo.reason) ||
      'No runtime database metrics were collected.';

    setDatabaseMonitoringState('neutral', 'Unavailable');
    setText('dbMonitorError', reason);

    logLine(`Database monitoring unavailable: ${reason}`);

    return;
  }

  // ------------------------------------------------------------
  // Actual database metrics
  // ------------------------------------------------------------

  const activeAvg = metrics.active_connections_avg;
  const activePeak = metrics.active_connections_peak;

  const poolAvg = metrics.connection_pool_utilization_avg_pct;
  const poolPeak = metrics.connection_pool_utilization_peak_pct;

  const maxConnections = metrics.max_connections;
  const sampleCount = metrics.sample_count;

  const pair = (avg, peak, decimals = 1, suffix = '') => {
    const a =
      avg === null || avg === undefined
        ? '—'
        : `${fmt(avg, decimals)}${suffix}`;

    const p =
      peak === null || peak === undefined
        ? '—'
        : `${fmt(peak, decimals)}${suffix}`;

    return `${a} / ${p}`;
  };

  setDatabaseMonitoringState('success', 'Collected');

  setText(
    'dbMonitorActiveConn',
    pair(activeAvg, activePeak, 1)
  );

  setText(
    'dbMonitorMaxConn',
    maxConnections === null || maxConnections === undefined
      ? '—'
      : fmtInt(maxConnections)
  );

  setText(
    'dbMonitorPoolUtil',
    pair(poolAvg, poolPeak, 1, '%')
  );

  setText(
    'dbMonitorSampleCount',
    sampleCount === null || sampleCount === undefined
      ? '—'
      : fmtInt(sampleCount)
  );

  setText(
    'dbMonitorError',
    metrics.monitoring_error || 'None'
  );

  // ------------------------------------------------------------
  // Status message
  // ------------------------------------------------------------

  const engine = metrics.engine || monitorInfo?.engine || 'database';

  if (metrics.monitoring_error) {
    logLine(
      `${capitalizeFirst(engine)} monitoring completed with a warning: ${metrics.monitoring_error}`,
      'error'
    );
  } else {
    logLine(
      `${capitalizeFirst(engine)} monitoring completed — ${sampleCount || 0} sample(s) collected.`,
      'success'
    );
  }

  // ------------------------------------------------------------
  // Optional pressure warning
  // ------------------------------------------------------------

  if (
    poolPeak !== null &&
    poolPeak !== undefined &&
    Number(poolPeak) >= 85
  ) {
    logLine(
      `${capitalizeFirst(engine)} connection-pool utilization peaked at ${fmt(poolPeak, 1)}%.`,
      'error'
    );
  }
}

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

    qs('[data-model-form="forced-flow"]')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const system_throughput = parseFloat(qs('#pg-ff-throughput').value);
      const names = (qs('#pg-ff-names').value || '').split(',').map((s) => s.trim()).filter(Boolean);
      const visits = parseNumList(qs('#pg-ff-visits').value);
      const serviceTimes = parseNumList(qs('#pg-ff-service').value);

      if (!names.length || names.length !== visits.length || names.length !== serviceTimes.length) {
        const out = qs('#playgroundOutput');
        qs('#playgroundResult').hidden = false;
        if (out) out.textContent = 'Error: component names, visits/request, and service times must each have the same number of comma-separated values.';
        return;
      }

      const components = {};
      names.forEach((name, i) => {
        components[name] = { visits_per_request: visits[i], service_time_seconds: serviceTimes[i] };
      });

      await runPlaygroundModel('/api/prediction/forced-flow', { system_throughput, components });
    });

    qs('[data-model-form="amdahl"]')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const raw = qs('#pg-amdahl-bottleneck').value.trim();
      let bottleneck_results;
      try {
        bottleneck_results = JSON.parse(raw);
      } catch (parseErr) {
        qs('#playgroundResult').hidden = false;
        const out = qs('#playgroundOutput');
        if (out) out.textContent = `Error: bottleneck_results is not valid JSON — ${parseErr.message}`;
        return;
      }
      await runPlaygroundModel('/api/prediction/amdahl', { bottleneck_results });
    });

    qs('[data-model-form="slo"]')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const users = parseNumList(qs('#pg-slo-users').value);
      const responseTimes = parseNumList(qs('#pg-slo-rt').value);
      const errorRates = parseNumList(qs('#pg-slo-err').value);

      if (!users.length || users.length !== responseTimes.length || users.length !== errorRates.length) {
        qs('#playgroundResult').hidden = false;
        const out = qs('#playgroundOutput');
        if (out) out.textContent = 'Error: users, response times, and error rates must each have the same number of comma-separated values.';
        return;
      }

      const levels = users.map((u, i) => ({
        users: u,
        response_time: responseTimes[i],
        error_rate_percent: errorRates[i],
        source: 'observed',
      }));

      const maxRt = parseFloat(qs('#pg-slo-max-rt').value);
      const maxErr = parseFloat(qs('#pg-slo-max-err').value);

      const body = { levels };
      if (Number.isFinite(maxRt)) body.max_response_time_seconds = maxRt;
      if (Number.isFinite(maxErr)) body.max_error_rate_percent = maxErr;

      await runPlaygroundModel('/api/prediction/slo', body);
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
        entry_point: state.locationResult,
        validation: state.validation,
        dependencies: state.dependencyResult,
        application_descriptor: state.descriptor,
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
