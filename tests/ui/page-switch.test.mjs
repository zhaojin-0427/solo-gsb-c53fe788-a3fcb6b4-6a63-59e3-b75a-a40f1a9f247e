/**
 * 页面切换回归测试（jsdom，无浏览器依赖）：
 * 1. 初始加载显示分支头版本生效规则；
 * 2. 切换到历史版本时，"当前生效规则" 与版本信息恢复该版本当时规则，
 *    且钉住复选框只在历史版本下可用；
 * 3. 在历史版本勾选钉住并提交，请求体只带 version_id（不带 branch），
 *    expected_version_number 取目标版本自身的钉版修订号（未钉版为 0），
 *    与分支绑定代数无关；
 * 4. 钉版成功后分支绑定卡片与绑定代数保持不变，钉版进入独立钉版历史；
 * 5. 历史版本下编辑被阻止（不触发 validate/commit 请求）。
 */
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..', '..');
const html = readFileSync(resolve(root, 'app', 'static', 'index.html'), 'utf8');
const appJs = readFileSync(resolve(root, 'app', 'static', 'app.js'), 'utf8');

const rule = (name, max) => ({
  name, type: 'number_range', target: 'Inputs!B2:B4', min: 0, max,
});

const SET_V1 = {
  name: '预算规则', latest_version: 1, versions: 1, created_at: '2026-01-01T00:00:00Z',
};

function makeMocks() {
  const calls = [];
  const versions = [
    {
      id: 1, version_number: 1, parent_id: null, branch_name: 'main',
      message: '初始', audit: {}, created_at: '2026-01-01T00:00:00Z',
      effective_constraint_set: {
        constraint_set: '示例约束集', constraint_set_id: 1, constraint_set_version: 1,
        source: 'pinned', pinned_at: '2026-01-01T00:00:00Z', pin_revision: 0,
        rules: [rule('旧规则', 10)],
      },
    },
    {
      id: 2, version_number: 2, parent_id: 1, branch_name: 'main',
      message: '后续提交', audit: {}, created_at: '2026-01-02T00:00:00Z',
      effective_constraint_set: {
        constraint_set: '预算规则', constraint_set_id: 2, constraint_set_version: 1,
        source: 'commit', pinned_at: '2026-01-02T00:00:00Z', pin_revision: 0,
        rules: [rule('新规则', 100)],
      },
    },
  ];

  const bindings = [{
    id: 2, kind: 'branch', branch_name: 'main', binding_number: 2,
    constraint_set: '预算规则', constraint_set_id: 2, constraint_set_version: 1,
    workbook_version_number: 2, workbook_version_id: 2,
    created_at: '2026-01-02T00:00:00Z',
  }];
  // 钉版是与分支绑定独立的状态；初始为空。
  const pins = [];

  async function handler(url, options = {}) {
    const method = options.method || 'GET';
    calls.push({ method, url, body: options.body ? JSON.parse(options.body) : null });
    const json = (payload, status = 200) => ({
      status,
      ok: status >= 200 && status < 300,
      headers: { get: () => 'application/json' },
      text: async () => JSON.stringify(payload),
    });

    if (method === 'GET' && url === '/api/workbook') {
      return json({
        id: 1, name: '测试簿', columns: 2, rows: 3,
        sheets: [{ position: 0, name: 'Inputs' }],
      });
    }
    if (method === 'GET' && url === '/api/branches') {
      return json([{ name: 'main', head_version_id: 2, created_at: 'x' }]);
    }
    if (method === 'GET' && url.startsWith('/api/versions?')) {
      return json(versions);
    }
    const versionMatch = /^\/api\/versions\/(\d+)$/.exec(url);
    if (method === 'GET' && versionMatch) {
      const v = versions.find((x) => x.id === Number(versionMatch[1]));
      return json({ version: v, cells: [], edges: [] });
    }
    if (method === 'GET' && url === '/api/constraint-sets') {
      return json([SET_V1]);
    }
    if (method === 'GET' && url === '/api/constraint-sets/%E9%A2%84%E7%AE%97%E8%A7%84%E5%88%99') {
      return json({
        name: '预算规则',
        versions: [{
          id: 2, name: '预算规则', version_number: 1, message: '初版',
          created_at: '2026-01-02T00:00:00Z', rules: [rule('新规则', 100)],
        }],
      });
    }
    if (method === 'GET' && (url === '/api/constraint-bindings' || url.startsWith('/api/constraint-bindings?'))) {
      return json(bindings);
    }
    if (method === 'GET' && url.startsWith('/api/constraint-bindings/history')) {
      return json([
        {
          id: 1, kind: 'branch', branch_name: 'main', binding_number: 1,
          constraint_set: '示例约束集', constraint_set_id: 1, constraint_set_version: 1,
          workbook_version_number: 1, workbook_version_id: 1, created_at: 'x',
        },
        ...bindings,
      ]);
    }
    if (method === 'GET' && url.startsWith('/api/constraint-bindings/pins')) {
      return json(pins);
    }
    if (method === 'POST' && url === '/api/constraint-bindings') {
      const body = options.body ? JSON.parse(options.body) : {};
      if (body.version_id != null) {
        // 钉版：expected_version_number 是目标版本的独立钉版修订号。
        const target = versions.find((v) => v.id === body.version_id);
        const currentRevision = pins
          .filter((p) => p.workbook_version_id === body.version_id).length;
        if (body.expected_version_number !== currentRevision) {
          return json({ detail: { code: 'VERSION_CONFLICT', message: '冲突' } }, 409);
        }
        const newPin = {
          id: 100 + pins.length, kind: 'version_pin', branch_name: 'main',
          pin_number: currentRevision + 1,
          constraint_set: '预算规则', constraint_set_id: 2, constraint_set_version: 1,
          workbook_version_number: target.version_number,
          workbook_version_id: target.id,
          created_at: '2026-01-03T00:00:00Z',
        };
        pins.push(newPin);
        // 模拟后端只更新目标版本的关联为 pinned 并推进其钉版修订号；
        // 分支绑定（bindings）与分支头完全不变。
        target.effective_constraint_set = {
          ...target.effective_constraint_set,
          source: 'pinned',
          pin_revision: currentRevision + 1,
        };
        return json({ pin: newPin, binding: newPin });
      }
      if (body.expected_version_number !== bindings[bindings.length - 1].binding_number) {
        return json({ detail: { code: 'VERSION_CONFLICT', message: '冲突' } }, 409);
      }
      const newBinding = {
        id: 3, kind: 'branch', branch_name: 'main', binding_number: 3,
        constraint_set: '预算规则', constraint_set_id: 2, constraint_set_version: 1,
        workbook_version_number: 2, workbook_version_id: 2,
        created_at: '2026-01-03T00:00:00Z',
      };
      bindings.push(newBinding);
      return json({ binding: newBinding });
    }
    if (method === 'POST' && (url === '/api/validate' || url === '/api/commits')) {
      return json({ ok: false }, 422);
    }
    throw new Error(`未实现的 mock: ${method} ${url}`);
  }

  return { handler, calls };
}

async function boot() {
  const dom = new JSDOM(html, {
    url: 'http://localhost:8000/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const { window } = dom;
  const mocks = makeMocks();
  window.fetch = (url, options) => mocks.handler(String(url), options);
  window.eval(appJs);
  if (!window.__appReady) {
    await new Promise((resolveReady) => window.document.addEventListener('app:ready', resolveReady));
  }
  return { dom, window, mocks };
}

function waitFor(window, predicate, timeoutMs = 1000) {
  return new Promise((resolvePromise, rejectPromise) => {
    const start = Date.now();
    const tick = () => {
      if (predicate()) return resolvePromise();
      if (Date.now() - start > timeoutMs) return rejectPromise(new Error('等待超时'));
      setTimeout(tick, 10);
    };
    tick();
  });
}

let failures = 0;
function check(name, condition, detail = '') {
  if (condition) {
    console.log(`  ✓ ${name}`);
  } else {
    failures += 1;
    console.error(`  ✗ ${name} ${detail}`);
  }
}

async function main() {
  console.log('页面切换回归测试');

  const { window, mocks } = await boot();
  const doc = window.document;

  // 1) 初始：分支头 v2 显示其生效规则（commit，预算规则）。
  await waitFor(
    window,
    () => doc.getElementById('binding-info').textContent.includes('预算规则') &&
      doc.getElementById('constraint-pin-version').disabled === true,
  );
  check(
    '初始加载展示分支头版本生效规则',
    doc.getElementById('bound-rules').textContent.includes('新规则'),
    doc.getElementById('bound-rules').textContent,
  );
  check(
    '钉住复选框在分支头下禁用',
    doc.getElementById('constraint-pin-version').disabled === true,
  );

  // 2) 切到历史版本 v1。
  const select = doc.getElementById('version-select');
  select.value = '1';
  select.dispatchEvent(new window.Event('change', { bubbles: true }));
  await waitFor(window, () => doc.getElementById('bound-rules').textContent.includes('旧规则'));
  check(
    '历史版本显示该版本当时生效规则（不是分支头规则）',
    doc.getElementById('bound-rules').textContent.includes('旧规则') &&
      !doc.getElementById('bound-rules').textContent.includes('新规则'),
  );
  check(
    '历史版本信息包含"显式钉住"来源标记',
    doc.getElementById('binding-info').textContent.includes('显式钉住'),
  );
  check(
    '历史版本下钉住复选框可用',
    doc.getElementById('constraint-pin-version').disabled === false,
  );

  // 3) 勾选钉住，提交绑定：请求体只带 version_id。
  const pin = doc.getElementById('constraint-pin-version');
  pin.checked = true;
  pin.dispatchEvent(new window.Event('change', { bubbles: true }));
  check(
    '勾选后按钮文案变为钉到历史版本',
    doc.getElementById('constraint-bind-btn').textContent.includes('钉到当前历史版本'),
  );
  doc.getElementById('constraint-bind-btn').click();
  await waitFor(window, () => mocks.calls.some((c) => c.url === '/api/constraint-bindings'));
  const bindCall = mocks.calls.filter((c) => c.url === '/api/constraint-bindings').at(-1);
  check('绑定请求方法为 POST', bindCall.method === 'POST');
  check('钉住请求带 version_id=1', bindCall.body.version_id === 1, JSON.stringify(bindCall.body));
  check('钉住请求不携带 branch', !('branch' in bindCall.body));
  check(
    'expected_version_number 取目标版本自身钉版修订号 0（不是分支代数 2）',
    bindCall.body.expected_version_number === 0,
    JSON.stringify(bindCall.body),
  );
  // 绑定成功后版本详情重新拉取并显示 pinned 来源。
  await waitFor(window, () => doc.getElementById('status').textContent.includes('钉到'));
  check(
    '钉住成功后状态提示包含版本归属与钉版修订',
    doc.getElementById('status').textContent.includes('main') &&
      doc.getElementById('status').textContent.includes('钉版修订 1') &&
      doc.getElementById('status').textContent.includes('不影响分支门禁'),
    doc.getElementById('status').textContent,
  );
  // 4) 钉版不改变分支当前绑定卡片（仍是绑定代数 2、预算规则）：切回分支头确认。
  const versionSelect = doc.getElementById('version-select');
  versionSelect.value = '2';
  versionSelect.dispatchEvent(new window.Event('change', { bubbles: true }));
  await waitFor(
    window,
    () => doc.getElementById('binding-info').textContent.includes('绑定代数 2'),
  );
  check(
    '切回分支头后绑定卡片仍显示代数 2（钉版不追加分支绑定）',
    doc.getElementById('binding-info').textContent.includes('预算规则') &&
      doc.getElementById('binding-info').textContent.includes('绑定代数 2'),
    doc.getElementById('binding-info').textContent,
  );
  const branchBindingCalls = mocks.calls.filter(
    (c) => c.url === '/api/constraint-bindings' && c.method === 'POST',
  );
  check('仅发送了一次写请求', branchBindingCalls.length === 1);
  // 钉版记录出现在独立的"历史版本钉版"分区，且分支换绑分区仍只有原记录。
  check(
    '历史面板展示钉版修订与分支换绑两类记录',
    doc.getElementById('binding-history').textContent.includes('历史版本钉版') &&
      doc.getElementById('binding-history').textContent.includes('钉版修订 #1') &&
      doc.getElementById('binding-history').textContent.includes('分支换绑'),
    doc.getElementById('binding-history').textContent,
  );

  // 5) 切回历史版本 v1，编辑被阻止，不产生 validate/commit 请求。
  versionSelect.value = '1';
  versionSelect.dispatchEvent(new window.Event('change', { bubbles: true }));
  await waitFor(window, () => doc.getElementById('constraint-pin-version').disabled === false);
  const callsBefore = mocks.calls.length;
  const formula = doc.getElementById('formula-input');
  formula.value = '999';
  formula.dispatchEvent(new window.Event('change', { bubbles: true }));
  doc.getElementById('validate-btn').click();
  await new Promise((r) => setTimeout(r, 50));
  const writeCalls = mocks.calls.slice(callsBefore).filter(
    (c) => c.url === '/api/validate' || c.url === '/api/commits',
  );
  check(
    '历史版本下预演/提交不发送写请求',
    writeCalls.length === 0,
    JSON.stringify(writeCalls),
  );
  check(
    '页面提示历史版本只读',
    doc.getElementById('status').textContent.includes('历史版本'),
    doc.getElementById('status').textContent,
  );

  if (failures > 0) {
    console.error(`\n${failures} 个断言失败`);
    process.exitCode = 1;
  } else {
    console.log('\n全部通过');
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
