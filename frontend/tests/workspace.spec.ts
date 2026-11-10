import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const completeId = 'a'.repeat(32);
const activeId = 'b'.repeat(32);
const failedId = 'c'.repeat(32);
const topic = '高光谱图像分类的研究现状与发展趋势';
const evidence = '光谱与空间特征的联合建模，是高光谱图像分类中的重要研究方向。自监督预训练可减少对标注样本的依赖，但跨传感器与跨场景的泛化仍需要进一步验证。';
const markdown = `# 高光谱图像分类：研究现状与发展趋势\n\n## 摘要\n\n高光谱图像分类正从依赖人工特征，转向光谱与空间信息的联合学习。研究重点逐步延伸到少样本学习和跨场景泛化。[E0001]\n\n## 01 · 主要技术路线\n\n现有方法可以从特征表达和训练方式两个维度理解。**方法的适用条件与验证数据同样重要。**\n\n| 技术路线 | 主要特点 | 适用条件 |\n| --- | --- | --- |\n| 传统机器学习 | 特征可解释、计算开销较低 | 标注样本有限 |\n| 深度学习 | 联合建模光谱与空间结构 | 有足够训练数据 |\n\n## 02 · 当前挑战\n\n- 标注稀缺与类别不平衡。\n- 传感器差异带来的领域偏移。\n- 跨场景评测与实际部署之间的差距。[E0001]\n\n> 阅读结论时，应同时核对实验条件与证据范围。\n\n## 03 · 后续方向\n\n在统一的评测条件下比较模型，往往比孤立的最高准确率更有参考价值。\n\n\`[E0001]\` 是代码中的编号，不生成按钮。未知编号 [E9999] 不生成按钮。\n\n<img src="x" onerror="window.bad=true">\n\n[不安全链接](javascript:alert(1))`;

interface Scenario { completed: boolean; failed: boolean; interrupted: boolean; offline: boolean; queueFull: boolean; uncertain: boolean; postCount: number; resumeBodies: unknown[]; offsets: number[] }

async function mockApi(page: Page, overrides: Partial<Scenario> = {}): Promise<Scenario> {
  const scenario: Scenario = { completed: false, failed: false, interrupted: false, offline: false, queueFull: false, uncertain: false, postCount: 0, resumeBodies: [], offsets: [], ...overrides };
  const items = [
    { run_id: completeId, topic, status: 'completed', max_tasks: 3, updated_at: '2026-11-10T13:20:00+00:00' },
    { run_id: activeId, topic: '检索增强生成的技术演进与应用边界', status: 'running', max_tasks: 3, updated_at: '2026-11-10T12:00:00+00:00' },
    { run_id: failedId, topic: '多智能体协作的架构与工程实践', status: 'failed', max_tasks: 3, updated_at: '2026-11-09T12:00:00+00:00' },
  ];
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === '/api/health') return route.fulfill({ json: { status: 'ok' } });
    if (path === '/api/research' && request.method() === 'POST') {
      scenario.postCount++;
      if (scenario.uncertain) return route.abort('failed');
      if (scenario.queueFull) return route.fulfill({ status: 503, json: { error: { code: 'queue_full', message: '研究队列已满，请稍后提交' } } });
      return route.fulfill({ status: 202, json: { run_id: activeId, accepted: true, status_url: `/api/research/${activeId}`, result_url: `/api/research/${activeId}/result` } });
    }
    if (path === '/api/research') {
      const offset = Number(url.searchParams.get('offset') ?? 0); scenario.offsets.push(offset);
      return route.fulfill({ json: { items: offset ? [] : items, offset, limit: 10, has_more: offset === 0 } });
    }
    const id = path.split('/')[3];
    if (![activeId, completeId, failedId].includes(id ?? '')) return route.fulfill({ status: 404, json: { error: { code: 'run_not_found', message: '研究不存在' } } });
    if (path.endsWith('/resume')) {
      scenario.resumeBodies.push(request.postDataJSON()); scenario.failed = false; scenario.interrupted = false;
      return route.fulfill({ status: 202, json: { run_id: id, accepted: true } });
    }
    if (path.endsWith('/report')) return route.fulfill({ contentType: 'text/markdown; charset=utf-8', body: markdown });
    if (path.endsWith('/result')) return route.fulfill({ json: { run_id: id, report_markdown: markdown, citations: [{ evidence_id: 'E0001', title: '光谱与空间特征联合建模：研究综述', url: 'https://example.com/research/spectral-spatial', text: evidence }], tasks: [{ id: 1, title: '主要研究方法与技术演进', status: 'completed', summary: '任务摘要。[E0001]', citation_ids: ['E0001'] }] } });
    if (scenario.offline) return route.fulfill({ status: 503, json: { error: { code: 'unavailable', message: '查询暂时不可用' } } });
    const done = id === completeId || scenario.completed;
    const failed = scenario.failed || (id === failedId && !scenario.resumeBodies.length);
    const interrupted = scenario.interrupted;
    return route.fulfill({ json: {
      run_id: id, topic: done ? topic : id === activeId ? '检索增强生成的技术演进与应用边界' : '多智能体协作的架构与工程实践', updated_at: '2026-11-10T13:20:00+00:00', max_tasks: 3,
      status: done ? 'completed' : failed ? 'failed' : 'running', execution: done || failed || interrupted ? 'idle' : 'executing',
      next_nodes: done ? [] : ['summarize'], active_task_id: done ? null : 2, planned_tasks: 4, selected_tasks: 3,
      completed_tasks: done ? 2 : 1, skipped_tasks: done ? 1 : 0, report_ready: done, requires_recovery_confirmation: interrupted, error_message: failed ? '上次执行失败，可以恢复研究。' : null,
      tasks: [
        { id: 1, title: '主要研究方法与技术演进', status: 'completed', selected: true, evidence_count: 5, citation_count: 3 },
        { id: 2, title: '关键技术、评测方法与公开数据集', status: done ? 'completed' : 'running', selected: true, evidence_count: 4, citation_count: done ? 2 : 0 },
        { id: 3, title: '当前挑战与值得关注的发展方向', status: done ? 'skipped' : 'pending', selected: true, evidence_count: 0, citation_count: 0 },
        { id: 4, title: '工业部署的实践案例', status: 'pending', selected: false, evidence_count: 0, citation_count: 0 },
      ],
    } });
  });
  return scenario;
}

test('research workspace, suggestions and history pagination', async ({ page }, testInfo) => {
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  const scenario = await mockApi(page);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /好的研究，\s*始于一个好问题。/ })).toBeVisible();
  await expect(page.getByText('API 已连接')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('workspace-desktop.png'), fullPage: true });
  await page.getByRole('button', { name: /高光谱图像分类的研究现状/ }).click();
  await expect(page.getByLabel('你想研究什么？')).toHaveValue(/高光谱/);
  await page.getByRole('button', { name: '下一页研究' }).click();
  await expect(page.getByText('创建的研究会保留在这里')).toBeVisible();
  expect(scenario.offsets).toContain(10);
  expect(errors).toEqual([]);
});

test('create, poll progress, receive report, click citation and download', async ({ page }, testInfo) => {
  const scenario = await mockApi(page);
  await page.goto('/');
  await page.getByLabel('你想研究什么？').fill('检索增强生成的技术演进');
  await page.getByRole('button', { name: '开始研究', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(activeId));
  await expect(page.getByText('关键技术、评测方法与公开数据集')).toBeVisible();
  await expect(page.getByText('本轮未执行', { exact: true })).toBeVisible();
  expect(scenario.postCount).toBe(1);
  await page.screenshot({ path: testInfo.outputPath('progress-desktop.png'), fullPage: true });
  scenario.completed = true;
  await expect(page.getByRole('tab', { name: '研究报告' })).toHaveAttribute('aria-selected', 'true');
  await page.screenshot({ path: testInfo.outputPath('report-desktop.png'), fullPage: true });
  await page.getByRole('button', { name: '查看证据 E0001' }).first().click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByText(evidence, { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('evidence-desktop.png') });
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).not.toBeVisible();
  const downloading = page.waitForEvent('download');
  await page.getByRole('button', { name: '下载报告', exact: true }).click();
  const file = await downloading;
  expect(file.suggestedFilename()).toBe(`research-${activeId}.md`);
  const stream = await file.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
  expect(Buffer.concat(chunks).toString('utf-8')).toBe(markdown);
});

test('completed deep link survives reload and Markdown stays inert', async ({ page }) => {
  await mockApi(page);
  await page.goto(`/#/research/${completeId}`);
  await expect(page.locator('.prose')).toBeVisible();
  await expect(page.locator('.prose img, .prose iframe, .prose a[href^="javascript:"]')).toHaveCount(0);
  await expect(page.locator('.prose pre button, .prose code button')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '查看证据 E9999' })).toHaveCount(0);
  expect(await page.evaluate(() => 'bad' in window)).toBe(false);
  await page.reload();
  await expect(page.locator('.prose')).toBeVisible();
});

test('ordinary failure resumes without interrupted confirmation', async ({ page }) => {
  const scenario = await mockApi(page, { failed: true });
  await page.goto(`/#/research/${failedId}`);
  await page.getByRole('button', { name: '继续研究', exact: true }).click();
  await expect.poll(() => scenario.resumeBodies.length).toBe(1);
  expect(scenario.resumeBodies[0]).toEqual({ recover_interrupted: false });
  await expect(page.getByText('研究进行中', { exact: true })).toBeVisible();
});

test('interrupted recovery requires explicit confirmation', async ({ page }) => {
  const scenario = await mockApi(page, { interrupted: true });
  await page.goto(`/#/research/${activeId}`);
  await page.getByRole('button', { name: '继续研究', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button', { name: '暂不恢复' }).click();
  expect(scenario.resumeBodies).toHaveLength(0);
  await page.getByRole('button', { name: '继续研究', exact: true }).click();
  await page.getByRole('button', { name: '确认已停止，继续' }).click();
  await expect.poll(() => scenario.resumeBodies.length).toBe(1);
  expect(scenario.resumeBodies[0]).toEqual({ recover_interrupted: true });
});

test('query failure preserves previous progress and never resubmits research', async ({ page }) => {
  const scenario = await mockApi(page);
  await page.goto(`/#/research/${activeId}`);
  await expect(page.getByText('关键技术、评测方法与公开数据集')).toBeVisible();
  scenario.offline = true;
  await page.getByRole('button', { name: '刷新研究进度' }).click();
  await expect(page.getByText('查询暂时不可用')).toBeVisible();
  await expect(page.getByText('关键技术、评测方法与公开数据集')).toBeVisible();
  expect(scenario.postCount).toBe(0);
  expect(scenario.resumeBodies).toHaveLength(0);
  scenario.offline = false;
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await expect(page.getByText('查询暂时不可用')).not.toBeVisible();
});

test('uncertain POST requires history check before another submit', async ({ page }) => {
  const scenario = await mockApi(page, { uncertain: true });
  await page.goto('/');
  await page.getByLabel('你想研究什么？').fill('模拟网络失败的研究');
  await page.getByRole('button', { name: '开始研究', exact: true }).click();
  await expect(page.getByText('后端可能已受理，请先刷新左侧历史，确认是否已经创建研究。')).toBeVisible();
  await expect(page.getByRole('button', { name: '开始研究', exact: true })).toBeDisabled();
  expect(scenario.postCount).toBe(1);
});

test('queue full keeps the research question editable', async ({ page }) => {
  await mockApi(page, { queueFull: true });
  await page.goto('/');
  await page.getByLabel('你想研究什么？').fill('需要保留的研究问题');
  await page.getByRole('button', { name: '开始研究', exact: true }).click();
  await expect(page.getByText('研究队列已满，请稍后提交')).toBeVisible();
  await expect(page.getByLabel('你想研究什么？')).toHaveValue('需要保留的研究问题');
  await expect(page.getByRole('button', { name: '开始研究', exact: true })).toBeEnabled();
});

test('switching research discards report and open evidence from previous run', async ({ page }) => {
  await mockApi(page);
  await page.goto(`/#/research/${completeId}`);
  await page.getByRole('button', { name: '查看证据 E0001' }).first().click();
  await page.getByRole('button', { name: '关闭证据' }).click();
  await page.locator('.history-item').filter({ hasText: '检索增强生成的技术演进与应用边界' }).click();
  await expect(page).toHaveURL(new RegExp(activeId));
  await expect(page.getByRole('tab', { name: '研究报告' })).toBeDisabled();
  await expect(page.locator('.prose')).toHaveCount(0);
});

test('mobile layout has no horizontal overflow and drawer remains usable', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /好的研究，\s*始于一个好问题。/ })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('workspace-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole('button', { name: '切换研究历史' }).click();
  await page.locator('.history-item').filter({ hasText: topic }).click();
  await expect(page.locator('.prose')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('report-mobile.png'), fullPage: true });
  await page.getByRole('button', { name: '查看证据 E0001' }).first().click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button', { name: '关闭证据' }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
});

test('unknown research remains a readable error without a recovery action', async ({ page }) => {
  await mockApi(page);
  await page.goto(`/#/research/${'d'.repeat(32)}`);
  await expect(page.getByText('研究不存在', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '继续研究', exact: true })).toHaveCount(0);
});

test('320px viewport keeps form controls and report within screen', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await mockApi(page);
  await page.goto('/');
  await expect(page.getByRole('button', { name: '开始研究', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('workspace-narrow.png'), fullPage: true });
  await page.goto(`/#/research/${completeId}`);
  await expect(page.locator('.prose')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
