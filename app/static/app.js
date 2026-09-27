const state = {
  workbook: null,
  branches: [],
  versions: [],
  branch: 'main',
  versionId: 1,
  headVersionId: 1,
  versionDetail: null,
  sheet: null,
  active: { sheet: '', column: 0, row: 0 },
  edits: new Map(),
  lastPreview: null,
  constraintSets: [],
  bindings: [],
  bindingHistory: [],
  pinHistory: [],
  setDetailCache: {},
  viewedSet: null,
};

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? '');
}

function cellKey(sheet, column, row) {
  return `${sheet}!${columnName(column)}${row + 1}`;
}

function columnName(index) {
  let n = index + 1;
  let out = '';
  while (n) {
    const rem = (n - 1) % 26;
    out = String.fromCharCode(65 + rem) + out;
    n = Math.floor((n - 1) / 26);
  }
  return out;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const error = new Error(data?.detail?.message || data?.detail || `HTTP ${response.status}`);
    error.data = data?.detail || data;
    error.status = response.status;
    throw error;
  }
  return data;
}

function setStatus(message, kind = '') {
  const status = $('status');
  status.textContent = message;
  status.className = `status ${kind}`;
}

function editKey(item = state.active) {
  return cellKey(item.sheet, item.column, item.row);
}

function normalizedEditContent(sheet, column, row) {
  const key = cellKey(sheet, column, row);
  if (state.edits.has(key)) return state.edits.get(key);
  const cell = state.versionDetail?.cells.find(
    (c) => c.sheet_name === sheet && c.column_index === column && c.row_index === row,
  );
  return cell?.content ?? '';
}

function currentCells() {
  const map = new Map();
  for (const cell of state.versionDetail?.cells || []) {
    map.set(cellKey(cell.sheet_name, cell.column_index, cell.row_index), cell);
  }
  for (const [address, content] of state.edits) {
    const separator = address.indexOf('!');
    const sheet = address.slice(0, separator);
    const ref = address.slice(separator + 1);
    const match = /^([A-Z]+)([0-9]+)$/.exec(ref);
    if (!match) continue;
    const column = colIndex(match[1]);
    const row = Number(match[2]) - 1;
    if (!content) {
      map.delete(address);
    } else {
      map.set(address, {
        sheet_name: sheet,
        column_index: column,
        row_index: row,
        content,
        kind: content.startsWith('=') ? 'formula' : 'constant',
        value: { kind: 'draft' },
      });
    }
  }
  return map;
}

function colName(name) {
  return name.toUpperCase().split('').reduce((acc, ch) => acc * 26 + ch.charCodeAt(0) - 64, 0) - 1;
}
const colIndex = colName;

function displayValue(value) {
  if (!value || value.kind === 'blank') return '';
  if (value.kind === 'number') {
    if (!Number.isFinite(value.value)) return value.value > 0 ? '#NUM!' : value.value < 0 ? '#NUM!' : '#NUM!';
    return String(value.value);
  }
  if (value.kind === 'boolean') return value.value ? 'TRUE' : 'FALSE';
  if (value.kind === 'error') return `#${value.code.replace('_ERROR', '')}`;
  if (value.kind === 'draft') return '● 未提交';
  return value.value ?? '';
}

async function init() {
  state.workbook = await api('/api/workbook');
  state.sheet = state.workbook.sheets[0].name;
  state.active.sheet = state.sheet;
  await refreshBranches();
  bindEvents();
  await loadHeadForCurrentBranch();
  await loadConstraints();
}

async function refreshBranches() {
  state.branches = await api('/api/branches');
  if (!state.branches.some((b) => b.name === state.branch)) state.branch = 'main';
  const branchSelect = $('branch-select');
  branchSelect.textContent = '';
  for (const branch of state.branches) {
    const option = document.createElement('option');
    option.value = branch.name;
    option.textContent = branch.name;
    option.selected = branch.name === state.branch;
    branchSelect.append(option);
  }
  renderMergeBranchOptions();
}

async function refreshVersions(selectHead = false) {
  state.versions = await api(`/api/versions?branch=${encodeURIComponent(state.branch)}`);
  const branch = state.branches.find((b) => b.name === state.branch);
  state.headVersionId = branch?.head_version_id || state.versions.at(-1)?.id;
  if (selectHead || !state.versions.some((v) => v.id === state.versionId)) {
    state.versionId = state.headVersionId;
  }
  const select = $('version-select');
  select.textContent = '';
  for (const version of [...state.versions].reverse()) {
    const option = document.createElement('option');
    option.value = version.id;
    option.textContent = `v${version.version_number} · ${version.message}`;
    option.selected = version.id === state.versionId;
    select.append(option);
  }
  const head = state.versions.find((v) => v.id === state.headVersionId);
  $('expected-version').textContent = head ? `提交将基于 v${head.version_number}，生成新版本号；旧版本不可变。` : '';
}

async function loadVersion(versionId = state.versionId) {
  state.versionId = Number(versionId);
  state.versionDetail = await api(`/api/versions/${state.versionId}`);
  state.viewedSet = state.versionDetail.version.effective_constraint_set || null;
  renderSheetTabs();
  renderGrid();
  renderEdges();
  renderAudit(state.versionDetail.version.audit || {});
  renderDiagnostics([]);
  renderConstraintResults(state.versionDetail.version.audit?.constraints || null);
  renderViewedVersionRules();
  state.lastPreview = null;
}

async function loadHeadForCurrentBranch() {
  state.edits.clear();
  await refreshVersions(true);
  await loadVersion(state.headVersionId);
  $('workbook-title').textContent =
    `${state.workbook.name} · ${state.workbook.columns} 列 × ${state.workbook.rows} 行 · IEEE-754 double`;
}

function renderMergeBranchOptions() {
  const select = $('merge-source');
  select.textContent = '';
  for (const branch of state.branches.filter((b) => b.name !== state.branch)) {
    const option = document.createElement('option');
    option.value = branch.name;
    option.textContent = branch.name;
    select.append(option);
  }
}

function renderSheetTabs() {
  const tabs = $('sheet-tabs');
  tabs.textContent = '';
  for (const sheet of state.workbook.sheets) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `sheet-tab ${sheet.name === state.sheet ? 'active' : ''}`;
    button.textContent = sheet.name;
    button.addEventListener('click', () => {
      state.sheet = sheet.name;
      state.active = { sheet: sheet.name, column: 0, row: 0 };
      renderSheetTabs();
      renderGrid();
    });
    tabs.append(button);
  }
}

function renderGrid() {
  const table = $('sheet-table');
  table.textContent = '';
  const cells = currentCells();
  const audit = state.lastPreview?.audit || state.versionDetail.version.audit || {};
  const changed = new Set(audit.changed || []);
  const affected = new Set(audit.affected || []);
  const errorSet = new Set();
  for (const error of state.lastPreview?.errors || []) {
    if (error.cell) errorSet.add(error.cell);
  }

  const colgroup = document.createElement('colgroup');
  const corner = document.createElement('col');
  corner.style.width = '48px';
  colgroup.append(corner);
  for (let c = 0; c < state.workbook.columns; c += 1) {
    const col = document.createElement('col');
    col.style.width = '112px';
    colgroup.append(col);
  }
  table.append(colgroup);

  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  const cornerCell = document.createElement('th');
  cornerCell.className = 'corner';
  cornerCell.textContent = '';
  headRow.append(cornerCell);
  for (let c = 0; c < state.workbook.columns; c += 1) {
    const th = document.createElement('th');
    th.textContent = columnName(c);
    headRow.append(th);
  }
  thead.append(headRow);
  table.append(thead);

  const tbody = document.createElement('tbody');
  for (let r = 0; r < state.workbook.rows; r += 1) {
    const tr = document.createElement('tr');
    const rowHead = document.createElement('td');
    rowHead.className = 'row-head';
    rowHead.textContent = r + 1;
    tr.append(rowHead);
    for (let c = 0; c < state.workbook.columns; c += 1) {
      const key = cellKey(state.sheet, c, r);
      const cell = cells.get(key);
      const td = document.createElement('td');
      td.title = cell?.content || '';
      td.className = [
        cell?.kind === 'formula' ? 'formula' : '',
        state.edits.has(key) ? 'changed' : '',
        affected.has(key) ? 'affected' : '',
        errorSet.has(key) ? 'error-cell' : '',
        state.active.sheet === state.sheet && state.active.column === c && state.active.row === r ? 'active' : '',
      ].filter(Boolean).join(' ');
      td.textContent = displayValue(cell?.value);
      td.addEventListener('click', () => selectCell(c, r));
      tr.append(td);
    }
    tbody.append(tr);
  }
  table.append(tbody);
  updateFormulaBar();
}

function selectCell(column, row) {
  state.active = { sheet: state.sheet, column, row };
  renderGrid();
}

function updateFormulaBar() {
  const address = `${columnName(state.active.column)}${state.active.row + 1}`;
  $('active-address').textContent = address;
  $('path-cell').value = cellKey(state.active.sheet, state.active.column, state.active.row);
  $('formula-input').value = normalizedEditContent(state.active.sheet, state.active.column, state.active.row);
}

function payloadEdits() {
  return [...state.edits.entries()].map(([address, content]) => {
    const index = address.indexOf('!');
    return {
      sheet: address.slice(0, index),
      address: address.slice(index + 1),
      content,
    };
  });
}

async function runValidate() {
  if (state.versionId !== state.headVersionId) {
    setStatus('正在查看历史版本；请切回分支头部再编辑。', 'error');
    return;
  }
  const head = state.versions.find((v) => v.id === state.headVersionId);
  try {
    setStatus('正在按受影响子图拓扑预演…');
    const preview = await api('/api/validate', {
      method: 'POST',
      body: JSON.stringify({
        branch: state.branch,
        expected_version_number: head.version_number,
        edits: payloadEdits(),
      }),
    });
    state.lastPreview = preview;
    renderDiagnostics(preview.errors || []);
    renderAudit(preview.audit || {});
    renderConstraintResults(preview.audit?.constraints || null);
    renderGrid();
    if (preview.ok) setStatus('预演通过：尚未生成新版本', 'ok');
    else if (preview.conflict) setStatus(preview.conflict.message, 'error');
    else if (preview.constraint_violations?.length) {
      setStatus(`预演被约束门禁拦截：${preview.constraint_violations[0].rule}`, 'error');
    } else setStatus(`预演失败：${preview.errors?.[0]?.message || '存在诊断'}`, 'error');
  } catch (error) {
    setStatus(error.message, 'error');
    renderDiagnostics(error.data?.errors || [error.data || { message: error.message }]);
  }
}

async function runCommit() {
  const head = state.versions.find((v) => v.id === state.headVersionId);
  try {
    setStatus('校验通过后在一个 SQLite 事务中原子提交…');
    const result = await api('/api/commits', {
      method: 'POST',
      body: JSON.stringify({
        branch: state.branch,
        expected_version_number: head.version_number,
        edits: payloadEdits(),
        message: $('message-input').value || '未命名编辑',
      }),
    });
    state.edits.clear();
    $('message-input').value = '';
    await refreshBranches();
    await refreshVersions(true);
    await loadVersion(result.version.id);
    setStatus(`已不可变提交 v${result.version.version_number}`, 'ok');
  } catch (error) {
    state.lastPreview = error.data;
    renderDiagnostics(error.data?.errors || [error.data || { message: error.message }]);
    renderAudit(error.data?.audit || {});
    renderConstraintResults(error.data?.audit?.constraints || null);
    renderGrid();
    setStatus(`提交被拒绝，旧版本未污染：${error.message}`, 'error');
  }
}

function renderDiagnostics(errors) {
  const box = $('diagnostics');
  box.textContent = '';
  if (!errors || errors.length === 0) {
    box.innerHTML = '<div class="diagnostic ok"><strong>未发现循环、缺失引用或类型错误</strong></div>';
    return;
  }
  for (const error of errors) {
    const div = document.createElement('div');
    div.className = `diagnostic ${error.severity === 'warn' ? 'warn' : ''}`;
    const title = document.createElement('strong');
    title.textContent = `${error.code || 'ERROR'} · ${error.cell || error.target || ''}`;
    div.append(title);
    const msg = document.createElement('div');
    msg.textContent = error.message || '';
    div.append(msg);
    if (error.formula) {
      const formula = document.createElement('div');
      formula.className = 'path';
      formula.textContent = `公式: ${error.formula}${error.start != null ? `；列 ${error.start}-${error.end ?? error.start}` : ''}`;
      div.append(formula);
    }
    if (error.path) {
      const path = document.createElement('div');
      path.className = 'path';
      path.textContent = error.path.join(' → ');
      div.append(path);
    }
    box.append(div);
  }
}

function renderAudit(audit) {
  const box = $('audit-panel');
  box.textContent = '';
  if (!audit || Object.keys(audit).length === 0) {
    box.textContent = '该版本没有审计记录。';
    return;
  }
  const sections = [
    ['直接变更', audit.changed],
    ['受影响子图', audit.affected],
    ['拓扑重算顺序', audit.recalculation_order],
  ];
  for (const [title, items] of sections) {
    const h = document.createElement('h3');
    h.textContent = `${title}（${items?.length || 0}）`;
    box.append(h);
    const pre = document.createElement('pre');
    pre.className = 'code-block';
    pre.textContent = (items || []).join('\n') || '无';
    box.append(pre);
  }
  if (audit.constraints) {
    const report = audit.constraints;
    const h = document.createElement('h3');
    h.textContent = `约束门禁（${report.constraint_set}@v${report.constraint_set_version}）`;
    box.append(h);
    const pre = document.createElement('pre');
    pre.className = 'code-block';
    const lines = [
      `评估规则: ${(report.evaluated_rules || []).join('、') || '无（无受影响规则）'}`,
      `跳过规则: ${(report.skipped_rules || []).join('、') || '无'}`,
      `违规数量: ${report.violations?.length || 0}`,
    ];
    pre.textContent = lines.join('\n');
    box.append(pre);
  }
  const h = document.createElement('h3');
  h.textContent = `前后值差异（${audit.diffs?.length || 0}）`;
  box.append(h);
  if (!audit.diffs?.length) {
    const p = document.createElement('p');
    p.className = 'muted';
    p.textContent = '无值变化。';
    box.append(p);
    return;
  }
  const table = document.createElement('table');
  table.className = 'diff-table';
  table.innerHTML = '<thead><tr><th>单元格</th><th>旧值</th><th>新值</th><th>原因</th></tr></thead>';
  const tbody = document.createElement('tbody');
  for (const diff of audit.diffs) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${diff.cell}</td><td>${esc(diff.before.content || '∅')} → ${esc(displayValue(diff.before.value))}</td>` +
      `<td>${esc(diff.after.content || '∅')} → ${esc(displayValue(diff.after.value))}</td>` +
      `<td>${diff.edited ? '编辑' : '依赖重算'}</td>`;
    tbody.append(tr);
  }
  table.append(tbody);
  box.append(table);
}

function renderEdges() {
  const box = $('edge-list');
  box.textContent = '';
  const edges = [...(state.versionDetail?.edges || [])]
    .sort((a, b) =>
      a.source.localeCompare(b.source) || a.target.localeCompare(b.target) || a.ref_start - b.ref_start);
  for (const edge of edges.slice(0, 160)) {
    const div = document.createElement('div');
    div.className = 'edge-item';
    div.textContent = `${edge.source} → ${edge.target}  [${edge.reference_kind} ${edge.ref_start}:${edge.ref_end}]`;
    box.append(div);
  }
  if (edges.length > 160) {
    const more = document.createElement('div');
    more.className = 'muted';
    more.textContent = `其余 ${edges.length - 160} 条边可通过 API 查看。`;
    box.append(more);
  }
}

function dependencyPaths(kind) {
  const wanted = $('path-cell').value.trim();
  const edges = state.versionDetail?.edges || [];
  const adjacency = new Map();
  for (const edge of edges) {
    const from = kind === 'incoming' ? edge.target : edge.source;
    const to = kind === 'incoming' ? edge.source : edge.target;
    if (!adjacency.has(from)) adjacency.set(from, []);
    adjacency.get(from).push({ next: to, edge });
  }
  for (const list of adjacency.values()) {
    list.sort((a, b) => a.next.localeCompare(b.next));
  }

  const queue = [{ node: wanted, path: [] }];
  const seen = new Set([wanted]);
  const rendered = [];
  while (queue.length) {
    const current = queue.shift();
    for (const item of adjacency.get(current.node) || []) {
      const path = [...current.path, item];
      rendered.push(path.map((step) =>
        kind === 'incoming'
          ? `${step.edge.source} → ${step.edge.target}@${step.edge.ref_start}:${step.edge.ref_end}`
          : `${step.edge.source} → ${step.edge.target}@${step.edge.ref_start}:${step.edge.ref_end}`,
      ).join('  ⇒  '));
      if (!seen.has(item.next)) {
        seen.add(item.next);
        queue.push({ node: item.next, path });
      }
    }
  }
  $('dependency-paths').textContent = rendered.length
    ? Array.from(new Set(rendered)).join('\n')
    : '没有找到路径。';
}

async function createBranch() {
  const name = $('new-branch-name').value.trim();
  if (!name) return setStatus('请输入分支名', 'error');
  try {
    await api('/api/branches', {
      method: 'POST',
      body: JSON.stringify({ name, from_version_id: state.versionId }),
    });
    state.branch = name;
    $('new-branch-name').value = '';
    await refreshBranches();
    await loadHeadForCurrentBranch();
    setStatus(`已从当前查看版本创建分支 ${name}`, 'ok');
  } catch (error) {
    setStatus(error.message, 'error');
  }
}

async function previewMerge() {
  const source = $('merge-source').value;
  if (!source) return setStatus('没有可选来源分支', 'error');
  try {
    const preview = await api('/api/merges/preview', {
      method: 'POST',
      body: JSON.stringify({ target_branch: state.branch, source_branch: source }),
    });
    state.lastMergePreview = preview;
    renderMergeResult(preview);
    renderDiagnostics(preview.semantic_errors || []);
    renderAudit(preview.semantic_audit || {});
    renderConstraintResults(preview.semantic_audit?.constraints || null);
    setStatus(preview.ok ? '合并预演通过，可执行合并' : '合并存在冲突或语义错误，已阻止', preview.ok ? 'ok' : 'error');
  } catch (error) {
    setStatus(error.message, 'error');
    renderMergeResult(error.data);
  }
}

function renderMergeResult(preview) {
  const box = $('merge-result');
  box.textContent = '';
  if (!preview) return;
  const badge = document.createElement('p');
  badge.innerHTML = preview.ok
    ? '<span class="badge good">可以自动合并</span>'
    : '<span class="badge bad">存在冲突 / 语义错误</span>';
  box.append(badge);
  const baseLabel = preview.virtual_base
    ? `虚拟基线（${(preview.merge_bases || []).map((base) => `v${base.version_number}`).join('、')} 合成）`
    : preview.ancestor_version
      ? `v${preview.ancestor_version.version_number}`
      : '无';
  const items = [
    `目标: v${preview.target_version?.version_number} @ ${preview.target_branch}`,
    `来源: v${preview.source_version?.version_number} @ ${preview.source_branch}`,
    `合并基线: ${baseLabel}`,
  ];
  const meta = document.createElement('pre');
  meta.className = 'code-block';
  meta.textContent = items.join('\n');
  box.append(meta);
  for (const [title, list, formatter] of [
    ['冲突', preview.conflicts, (c) => `${c.cell}: 目标=${c.target || '∅'} / 来源=${c.source || '∅'} / 基点=${c.base || '∅'}`],
    ['来源独有变更', preview.source_only_changes, (c) => `${c.cell} = ${c.content || '∅'}`],
  ]) {
    const h = document.createElement('h3');
    h.textContent = `${title}（${list?.length || 0}）`;
    box.append(h);
    const pre = document.createElement('pre');
    pre.className = 'code-block';
    pre.textContent = (list || []).map(formatter).join('\n') || '无';
    box.append(pre);
  }
  if (preview.constraint_sets) {
    const h = document.createElement('h3');
    h.textContent = `跨分支规则冲突（${preview.constraint_conflicts?.length || 0}）`;
    box.append(h);
    const pre = document.createElement('pre');
    pre.className = 'code-block';
    const fmt = (ref) => (ref ? `${ref.constraint_set}@v${ref.constraint_set_version}` : '未绑定');
    const lines = [
      `目标绑定: ${fmt(preview.constraint_sets.target)}`,
      `来源绑定: ${fmt(preview.constraint_sets.source)}`,
      '合并后以目标分支绑定的约束集为准。',
    ];
    for (const conflict of preview.constraint_conflicts || []) {
      if (conflict.message) lines.push(`· ${conflict.message}`);
      else if (conflict.kind === 'changed') lines.push(`· 规则「${conflict.rule}」双方定义不同`);
      else if (conflict.kind === 'source_only') lines.push(`· 规则「${conflict.rule}」仅来源分支有`);
      else if (conflict.kind === 'target_only') lines.push(`· 规则「${conflict.rule}」仅目标分支有`);
    }
    pre.textContent = lines.join('\n');
    box.append(pre);
  }
}

async function executeMerge() {
  const source = $('merge-source').value;
  const preview = state.lastMergePreview;
  const targetVersion = preview?.target_branch === state.branch
    && preview?.source_branch === source
    ? preview.target_version?.version_number
    : undefined;
  try {
    const result = await api('/api/merges', {
      method: 'POST',
      body: JSON.stringify({
        target_branch: state.branch,
        source_branch: source,
        expected_version_number: targetVersion,
      }),
    });
    state.lastMergePreview = null;
    await refreshBranches();
    await refreshVersions(true);
    await loadVersion(result.version.id);
    setStatus(`合并已原子提交为 v${result.version.version_number}`, 'ok');
  } catch (error) {
    setStatus(`合并被阻止：${error.message}`, 'error');
    renderMergeResult(error.data);
    renderDiagnostics(error.data?.semantic_errors || error.data?.conflicts || []);
    renderConstraintResults(error.data?.semantic_audit?.constraints || null);
  }
}

const RULE_TYPE_LABELS = {
  number_range: '数值范围',
  not_empty: '非空',
  allowed_values: '允许值',
  unique: '唯一性',
};

function describeRule(rule) {
  const parts = [`${rule.name}（${RULE_TYPE_LABELS[rule.type] || rule.type}）`, `目标 ${rule.target}`];
  if (rule.min != null || rule.max != null) {
    parts.push(`范围 ${rule.min != null ? rule.min : '-∞'} ~ ${rule.max != null ? rule.max : '+∞'}`);
  }
  if (rule.values) parts.push(`允许值 ${rule.values.join('、')}`);
  if (rule.condition) parts.push(`条件 ${rule.condition}`);
  return parts.join(' · ');
}

function currentBinding() {
  return state.bindings.find((b) => b.branch_name === state.branch) || null;
}

function viewingHistory() {
  return state.versionId !== state.headVersionId;
}

function sourceLabel(source) {
  return source === 'pinned' ? '显式钉住' : '提交时生效';
}

function renderViewedVersionRules() {
  const info = $('binding-info');
  info.classList.remove('muted');
  const rulesBox = $('bound-rules');
  rulesBox.textContent = '';
  const viewed = state.viewedSet;
  const binding = currentBinding();
  if (viewingHistory()) {
    // 历史版本：展示该版本当时生效（或被显式钉住）的规则，而非分支头规则。
    if (viewed) {
      info.innerHTML = `历史版本 <strong>v${state.versionDetail.version.version_number}</strong> 生效规则：` +
        `<strong>${esc(viewed.constraint_set)}@v${viewed.constraint_set_version}</strong>` +
        `（${sourceLabel(viewed.source)}` +
        `，钉版修订 ${viewed.pin_revision ?? 0}）`;
      for (const rule of viewed.rules || []) {
        const div = document.createElement('div');
        div.className = 'rule-item';
        div.textContent = describeRule(rule);
        rulesBox.append(div);
      }
    } else {
      info.textContent = `历史版本 v${state.versionDetail.version.version_number} 没有关联约束集。`;
      rulesBox.innerHTML = '<p class="muted">该版本提交时分支未绑定约束集。</p>';
    }
    return;
  }
  if (binding) {
    info.innerHTML = `当前分支 <strong>${esc(state.branch)}</strong> 绑定 ` +
      `<strong>${esc(binding.constraint_set)}@v${binding.constraint_set_version}</strong>` +
      `（绑定代数 ${binding.binding_number}，钉住工作簿 v${binding.workbook_version_number}）`;
    if (viewed) {
      for (const rule of viewed.rules || []) {
        const div = document.createElement('div');
        div.className = 'rule-item';
        div.textContent = describeRule(rule);
        rulesBox.append(div);
      }
    }
  } else {
    info.textContent = `当前分支 ${state.branch} 未绑定约束集，提交不受门禁限制。`;
    rulesBox.innerHTML = '<p class="muted">无生效规则。</p>';
  }
}

async function getSetDetail(name) {
  if (!state.setDetailCache[name]) {
    state.setDetailCache[name] = await api(`/api/constraint-sets/${encodeURIComponent(name)}`);
  }
  return state.setDetailCache[name];
}

async function loadConstraints() {
  const query = encodeURIComponent(state.branch);
  const [sets, bindings, history, pins] = await Promise.all([
    api('/api/constraint-sets'),
    api('/api/constraint-bindings'),
    api(`/api/constraint-bindings/history?branch=${query}`),
    api(`/api/constraint-bindings/pins?branch=${query}`),
  ]);
  state.constraintSets = sets;
  state.bindings = bindings;
  state.bindingHistory = history;
  state.pinHistory = pins;
  renderConstraintPanel();
}

async function renderConstraintPanel() {
  // “当前生效规则”卡片随查看版本切换（历史版本显示该版本当时的规则）。
  renderViewedVersionRules();

  const setSelect = $('constraint-set-select');
  const previous = setSelect.value;
  setSelect.textContent = '';
  for (const set of state.constraintSets) {
    const option = document.createElement('option');
    option.value = set.name;
    option.textContent = `${set.name}（最新 v${set.latest_version}）`;
    setSelect.append(option);
  }
  const binding = currentBinding();
  if (binding && state.constraintSets.some((s) => s.name === binding.constraint_set)) {
    setSelect.value = binding.constraint_set;
  } else if (previous) {
    setSelect.value = previous;
  }
  await refreshConstraintVersionOptions();
  updatePinControls();

  const historyBox = $('binding-history');
  historyBox.textContent = '';
  const renderEntry = (item, isPin) => {
    const div = document.createElement('div');
    div.className = 'edge-item';
    const target = `版本 #${item.workbook_version_id}（v${item.workbook_version_number}）`;
    const label = isPin ? `钉版修订 #${item.pin_number}` : `绑定代数 #${item.binding_number}`;
    div.textContent = `${label} · ${item.constraint_set}@v${item.constraint_set_version}` +
      ` · ${isPin ? '钉到' : '绑定时头'}${target} · ${item.created_at}`;
    historyBox.append(div);
  };
  if (!state.bindingHistory.length && !state.pinHistory.length) {
    historyBox.innerHTML = '<p class="muted">当前分支还没有绑定或钉版记录。</p>';
  }
  if (state.bindingHistory.length) {
    const heading = document.createElement('p');
    heading.className = 'muted';
    heading.textContent = `分支换绑（${state.bindingHistory.length}）：决定后续预演/提交/合并门禁`;
    historyBox.append(heading);
  }
  for (const item of [...state.bindingHistory].reverse()) {
    renderEntry(item, false);
  }
  if (state.pinHistory.length) {
    const heading = document.createElement('p');
    heading.className = 'muted';
    heading.textContent = `历史版本钉版（${state.pinHistory.length}）：只改变对应历史版本的规则`;
    historyBox.append(heading);
  }
  for (const item of [...state.pinHistory].reverse()) {
    renderEntry(item, true);
  }
}

function updatePinControls() {
  const checkbox = $('constraint-pin-version');
  const button = $('constraint-bind-btn');
  const versionNumber = state.versionDetail?.version.version_number;
  if (viewingHistory()) {
    checkbox.disabled = false;
    checkbox.parentElement.lastChild.textContent =
      `钉到当前查看的历史版本 v${versionNumber}（不选则绑定到当前分支头）`;
  } else {
    checkbox.disabled = true;
    checkbox.checked = false;
    checkbox.parentElement.lastChild.textContent =
      '切换到历史版本后，可把所选规则版本钉到该历史版本';
  }
  button.textContent = checkbox.checked && viewingHistory()
    ? '钉到当前历史版本'
    : '绑定所选版本到当前分支';
}

async function refreshConstraintVersionOptions() {
  const name = $('constraint-set-select').value;
  const versionSelect = $('constraint-version-select');
  versionSelect.textContent = '';
  if (!name) return;
  const detail = await getSetDetail(name);
  const binding = currentBinding();
  for (const version of [...detail.versions].reverse()) {
    const option = document.createElement('option');
    option.value = version.version_number;
    option.textContent = `v${version.version_number} · ${version.message || '（无说明）'}`;
    versionSelect.append(option);
  }
  if (binding && binding.constraint_set === name) {
    versionSelect.value = String(binding.constraint_set_version);
  }
  await showSelectedConstraintRules();
}

async function showSelectedConstraintRules() {
  const name = $('constraint-set-select').value;
  const versionNumber = Number($('constraint-version-select').value);
  if (!name || !versionNumber) return;
  const detail = await getSetDetail(name);
  const version = detail.versions.find((v) => v.version_number === versionNumber);
  if (!version) return;
  const text = JSON.stringify(version.rules, null, 2);
  $('constraint-rules-view').textContent = text;
  $('constraint-rules-editor').value = JSON.stringify(version.rules, null, 2);
  $('constraint-set-name').value = name;
}

function editorRules() {
  let rules;
  try {
    rules = JSON.parse($('constraint-rules-editor').value);
  } catch (error) {
    throw new Error(`规则 JSON 无法解析: ${error.message}`);
  }
  return rules;
}

async function createConstraintSet() {
  try {
    const name = $('constraint-set-name').value.trim();
    if (!name) return setStatus('请输入约束集名称', 'error');
    const result = await api('/api/constraint-sets', {
      method: 'POST',
      body: JSON.stringify({
        name,
        rules: editorRules(),
        message: $('constraint-set-message').value,
        expected_version_number: 0,
      }),
    });
    state.setDetailCache = {};
    await loadConstraints();
    setStatus(`已创建约束集 ${name}@v${result.constraint_set.version_number}`, 'ok');
  } catch (error) {
    setStatus(`创建约束集失败：${error.message}`, 'error');
  }
}

async function saveConstraintSetVersion() {
  try {
    const name = $('constraint-set-select').value || $('constraint-set-name').value.trim();
    const meta = state.constraintSets.find((s) => s.name === name);
    if (!meta) return setStatus('请选择要修改的约束集', 'error');
    const result = await api(`/api/constraint-sets/${encodeURIComponent(name)}/versions`, {
      method: 'POST',
      body: JSON.stringify({
        rules: editorRules(),
        message: $('constraint-set-message').value,
        expected_version_number: meta.latest_version,
      }),
    });
    state.setDetailCache = {};
    await loadConstraints();
    setStatus(`已保存 ${name}@v${result.constraint_set.version_number}（旧版本保持不可变）`, 'ok');
  } catch (error) {
    setStatus(`保存新版本失败：${error.message}`, 'error');
  }
}

async function bindConstraintSet() {
  try {
    const name = $('constraint-set-select').value;
    const version = Number($('constraint-version-select').value);
    if (!name || !version) return setStatus('请选择要绑定的约束集版本', 'error');
    const pinToViewed = $('constraint-pin-version').checked && viewingHistory();
    const payload = {
      constraint_set: name,
      constraint_set_version: version,
    };
    if (pinToViewed) {
      // 版本级钉版：不传 branch，由后端按版本归属分支校验并落记录。
      // 钉版与分支绑定是两类状态：expected_version_number 是目标版本自身
      // 的钉版修订号（未钉版为 0），不读取、也不前进分支绑定代数。
      payload.version_id = state.versionId;
      payload.expected_version_number = state.viewedSet?.pin_revision ?? 0;
    } else {
      // 分支换绑：沿用该分支当前绑定代数。
      payload.branch = state.branch;
      payload.expected_version_number = currentBinding() ? currentBinding().binding_number : 0;
    }
    const result = await api('/api/constraint-bindings', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    $('constraint-pin-version').checked = false;
    await loadConstraints();
    if (pinToViewed) {
      // 刷新当前历史版本详情，展示刚钉住的规则与新的钉版修订号。
      await loadVersion(state.versionId);
      const owner = state.versionDetail.version.branch_name;
      setStatus(
        `已把 ${name}@v${version} 钉到 ${owner} 分支的历史版本 v${state.versionDetail.version.version_number}` +
        `（钉版修订 ${result.pin.pin_number}，不影响分支门禁）`,
        'ok',
      );
    } else {
      setStatus(
        `已把 ${name}@v${version} 绑定到 ${state.branch}（绑定代数 ${result.binding.binding_number}）`,
        'ok',
      );
    }
  } catch (error) {
    setStatus(`绑定失败：${error.message}`, 'error');
  }
}

function renderConstraintResults(report) {
  const box = $('constraint-results');
  box.textContent = '';
  box.classList.remove('muted');
  if (!report) {
    box.classList.add('muted');
    box.textContent = '当前版本或操作没有约束门禁记录（分支未绑定约束集，或尚未预演/提交）。';
    return;
  }
  const meta = document.createElement('p');
  meta.innerHTML = `约束集 <strong>${esc(report.constraint_set)}@v${report.constraint_set_version}</strong>` +
    ` · 评估 ${report.evaluated_rules?.length || 0} 条 · 跳过 ${report.skipped_rules?.length || 0} 条`;
  box.append(meta);
  if (!report.violations?.length) {
    const ok = document.createElement('div');
    ok.className = 'diagnostic ok';
    ok.innerHTML = '<strong>全部受影响约束通过</strong>';
    box.append(ok);
    return;
  }
  for (const violation of report.violations) {
    const div = document.createElement('div');
    div.className = 'diagnostic';
    const title = document.createElement('strong');
    title.textContent = `${violation.rule} · ${violation.cell} · ${violation.constraint_set}@v${violation.constraint_set_version}`;
    div.append(title);
    const detail = document.createElement('div');
    detail.textContent = `${violation.message}；实际值 ${displayValue(violation.actual)}；期望 ${violation.expectation}`;
    div.append(detail);
    if (violation.path?.length) {
      const path = document.createElement('div');
      path.className = 'path';
      path.textContent = `依赖路径: ${violation.path.join(' → ')}`;
      div.append(path);
    }
    box.append(div);
  }
}

function bindEvents() {
  $('branch-select').addEventListener('change', async (event) => {
    state.branch = event.target.value;
    renderMergeBranchOptions();
    await loadHeadForCurrentBranch();
    await loadConstraints();
  });
  $('version-select').addEventListener('change', async (event) => {
    state.edits.clear();
    await loadVersion(Number(event.target.value));
    renderConstraintPanel();
    setStatus(state.versionId === state.headVersionId ? '正在查看分支头部' : '正在查看不可变历史版本（只读，可钉住规则）');
  });
  $('reload-btn').addEventListener('click', async () => {
    await refreshBranches();
    await loadHeadForCurrentBranch();
    setStatus('已刷新', 'ok');
  });
  $('formula-input').addEventListener('change', (event) => {
    if (state.versionId !== state.headVersionId) {
      setStatus('历史版本只读，不能编辑。', 'error');
      updateFormulaBar();
      return;
    }
    state.edits.set(editKey(), event.target.value);
    state.lastPreview = null;
    renderGrid();
    setStatus('已记录本地编辑；需要预演或提交才触发重算');
  });
  $('validate-btn').addEventListener('click', runValidate);
  $('commit-btn').addEventListener('click', runCommit);
  $('branch-btn').addEventListener('click', createBranch);
  $('merge-preview-btn').addEventListener('click', previewMerge);
  $('merge-btn').addEventListener('click', executeMerge);
  $('constraint-set-select').addEventListener('change', refreshConstraintVersionOptions);
  $('constraint-version-select').addEventListener('change', showSelectedConstraintRules);
  $('constraint-pin-version').addEventListener('change', updatePinControls);
  $('constraint-create-btn').addEventListener('click', createConstraintSet);
  $('constraint-save-btn').addEventListener('click', saveConstraintSetVersion);
  $('constraint-bind-btn').addEventListener('click', bindConstraintSet);
  document.querySelectorAll('[data-path]').forEach((button) => {
    button.addEventListener('click', () => dependencyPaths(button.dataset.path));
  });
}

init()
  .then(() => {
    window.__appReady = true;
    document.dispatchEvent(new Event('app:ready'));
  })
  .catch((error) => {
    setStatus(`初始化失败: ${error.message}`, 'error');
    console.error(error);
  });
