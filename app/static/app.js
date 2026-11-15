// 原生 JS：分段切换 + 幂等导入 + 工作台执行对齐 + 复核筛选；无框架、无打包器。
(function () {
  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  // ---- 1) 分段控件：[data-tab-target] → 同面板内 .tab-panel 只显示目标 ----
  qsa('[data-tab-target]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var group = btn.closest('.segmented') || btn.closest('.tabs');
      if (group) {
        qsa('button', group).forEach(function (b) { b.classList.remove('active'); });
      }
      btn.classList.add('active');
      var target = document.getElementById(btn.getAttribute('data-tab-target'));
      if (!target) return;
      var host = btn.closest('.panel') || document;
      qsa('.tab-panel', host).forEach(function (panel) { panel.hidden = (panel !== target); });
      var workspace = qs('.workspace');
      if (workspace) workspace.scrollTop = 0;
    });
  });

  // ---- 2) 统一 POST JSON ----
  window.postJSON = async function (url, payload) {
    var res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {})
    });
    var data = null;
    try { data = await res.json(); } catch (e) { data = { detail: '响应不是 JSON' }; }
    if (!res.ok) {
      var msg = (data && (data.detail || data.message)) || ('请求失败：' + res.status);
      throw new Error(msg);
    }
    return data;
  };

  window.showError = function (host, err) {
    if (!host) return;
    host.hidden = false;
    host.textContent = '执行失败：' + (err && err.message ? err.message : String(err));
  };

  function fmtNum(value, digits) {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'boolean') return value ? '是' : '否';
    if (typeof value === 'number') return value.toFixed(digits === undefined ? 2 : digits);
    return String(value);
  }

  function tagForGrade(grade) {
    var cls = grade === 'A' ? 'ok' : (grade === 'B' ? 'warn' : 'err');
    return '<span class="tag ' + cls + '">' + grade + '</span>';
  }

  // ---- 3) 总览页：校验并载入样例遥测（幂等） ----
  var importBtn = qs('#import-btn');
  if (importBtn) {
    importBtn.addEventListener('click', async function () {
      var host = qs('#import-msg');
      importBtn.disabled = true;
      if (host) {
        host.hidden = false;
        host.textContent = '正在校验并幂等导入…';
      }
      try {
        var data = await window.postJSON('/api/import', {});
        if (host) {
          host.innerHTML = '导入完成：数据集 <code>' + data.dataset_id + '</code>，共 ' + data.rows +
            ' 行、' + data.devices.length + ' 台设备，网格 ' + data.window.cells + ' 格。' +
            '（sha256 ' + data.sha256.slice(0, 12) + '…，重复点击不会新增数据集）';
        }
      } catch (err) {
        window.showError(host, err);
      } finally {
        importBtn.disabled = false;
      }
    });
  }

  // ---- 4) 工作台：设备 → 指标联动 + 执行对齐 ----
  var form = qs('#align-form');
  if (form && form.getAttribute('data-page') === 'workbench') {
    var deviceSel = qs('#device');
    var metricSel = qs('#metric');
    var bar = qs('#progress-bar');
    var progressWrap = qs('#progress-wrap');
    var progressText = qs('#progress-text');
    var stageList = qs('#stage-list');
    var errorBox = qs('#error-box');
    var summaryBox = qs('#summary-box');
    var submitBtn = qs('#align-btn');

    // 页面初始渲染的「指标 → 中文名」映射，联动时沿用同一套业务文案
    var metricNames = { temperature: '温度 temperature', pressure: '压力 pressure' };
    qsa('option', metricSel).forEach(function (opt) {
      if (opt.value) metricNames[opt.value] = opt.textContent.trim();
    });

    function syncMetrics() {
      var opt = deviceSel.options[deviceSel.selectedIndex];
      var metrics = [];
      try { metrics = JSON.parse(opt.getAttribute('data-metrics') || '[]'); } catch (e) { metrics = []; }
      var keep = metricSel.value;
      metricSel.innerHTML = '';
      metrics.forEach(function (m) {
        var o = document.createElement('option');
        o.value = m;
        o.textContent = metricNames[m] || m;
        metricSel.appendChild(o);
      });
      if (!metrics.length) {
        var o2 = document.createElement('option');
        o2.value = '';
        o2.textContent = '该设备没有可对齐指标';
        metricSel.appendChild(o2);
      } else if (metrics.indexOf(keep) !== -1) {
        metricSel.value = keep;
      }
    }
    deviceSel.addEventListener('change', syncMetrics);
    syncMetrics();

    var ticker = null;
    function startProgress() {
      progressWrap.hidden = false;
      errorBox.hidden = true;
      summaryBox.hidden = true;
      stageList.innerHTML = '';
      var pct = 6;
      bar.style.width = pct + '%';
      progressText.textContent = '正在执行：漂移估计 → 网格重采样 → 缺口判定 → 摘要 → 评分…';
      ticker = setInterval(function () {
        pct = Math.min(pct + 7, 92);
        bar.style.width = pct + '%';
      }, 220);
    }
    function stopProgress(ok) {
      if (ticker) { clearInterval(ticker); ticker = null; }
      bar.style.width = ok ? '100%' : '0%';
    }

    form.addEventListener('submit', async function (ev) {
      ev.preventDefault();
      if (!metricSel.value) { window.showError(errorBox, new Error('请选择指标')); return; }
      startProgress();
      submitBtn.disabled = true;
      var payload = {
        device_id: deviceSel.value,
        metric: metricSel.value,
        offset_threshold: qs('#threshold').value
      };
      try {
        var data = await window.postJSON('/api/align', payload);
        stopProgress(true);
        progressText.textContent = '执行完成，总耗时 ' + fmtNum(data.total_ms, 2) + ' ms';
        renderStages(data);
        renderSummary(data);
      } catch (err) {
        stopProgress(false);
        progressText.textContent = '执行失败，可修改参数重试';
        window.showError(errorBox, err);
      } finally {
        submitBtn.disabled = false;
      }
    });

    function renderStages(data) {
      var rows = data.stages.map(function (s) {
        return '<tr><td>' + s.name + '</td><td class="muted small">' + s.detail + '</td><td class="num">' +
          fmtNum(s.ms, 2) + ' ms</td></tr>';
      }).join('');
      stageList.innerHTML = '<div class="table-wrap auto"><table class="dense"><thead><tr><th>阶段</th><th>说明</th>' +
        '<th>耗时</th></tr></thead><tbody>' + rows +
        '<tr><td colspan="2"><strong>合计</strong></td><td class="num"><strong>' + fmtNum(data.total_ms, 2) +
        ' ms</strong></td></tr></tbody></table></div>';
    }

    function renderSummary(data) {
      var drift = data.drift;
      var driftText = drift.available
        ? ('时钟偏移 <strong>' + fmtNum(drift.offset_s, 2) + ' s</strong>' +
           (drift.drifted ? ' <span class="tag err">判定漂移</span>' : ' <span class="tag ok">时钟正常</span>') +
           '，走时偏差 ' + fmtNum(drift.rate_ppm, 1) + ' ppm，校正后残差 RMSE ' + fmtNum(drift.rmse_s, 3) + ' s')
        : '基准设备，无需漂移校正';
      var cov = data.coverage;
      summaryBox.hidden = false;
      summaryBox.innerHTML =
        '<h3>对齐结论</h3>' +
        '<p class="small">' + driftText + '</p>' +
        '<p class="small">缺口 <strong>' + data.gaps.count + '</strong> 处（插补 ' + data.gaps.interpolated_cells +
          ' 格 / 未修复 ' + data.gaps.unrepaired_cells + ' 格），carried ' + data.grid.held_cells + ' 格，' +
          '覆盖率 <strong>' + (cov.coverage * 100).toFixed(2) + '%</strong> ' + tagForGrade(cov.grade) + '</p>' +
        '<p><a class="btn small" href="/result/' + data.run_id + '">查看完整结果与依据 →</a></p>';
    }
  }

  // ---- 5) 复核页：设备 / 指标 / 关键字筛选（纯前端，不新增接口） ----
  var pairTable = qs('#pair-table');
  if (pairTable) {
    var devSel = qs('#review-device');
    var metSel = qs('#review-metric');
    var search = qs('#review-search');
    var stat = qs('#review-stat');
    var rows = qsa('tbody tr', pairTable);
    var total = rows.length;

    var applyFilter = function () {
      var needle = (search ? search.value : '').trim().toLowerCase();
      var device = devSel ? devSel.value : '';
      var metric = metSel ? metSel.value : '';
      var shown = 0;
      rows.forEach(function (tr) {
        var haystack = ((tr.getAttribute('data-search') || '') + ' ' + tr.textContent).toLowerCase();
        var hit = (!needle || haystack.indexOf(needle) !== -1) &&
          (!device || tr.getAttribute('data-device') === device) &&
          (!metric || tr.getAttribute('data-metric') === metric);
        tr.hidden = !hit;
        if (hit) shown += 1;
      });
      if (stat) {
        var active = needle || device || metric;
        stat.textContent = active ? ('命中 ' + shown + ' / ' + total + ' 条序列') : ('共 ' + total + ' 条序列');
      }
    };
    if (search) {
      search.addEventListener('input', applyFilter);
      search.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') { search.value = ''; applyFilter(); }
      });
    }
    if (devSel) devSel.addEventListener('change', applyFilter);
    if (metSel) metSel.addEventListener('change', applyFilter);
    applyFilter();
  }
})();
