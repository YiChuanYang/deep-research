import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const completeId = 'a'.repeat(32);
const activeId = 'b'.repeat(32);
const failedId = 'c'.repeat(32);
const topic = '高光谱图像分类的研究现状与发展趋势';
const evidence = '光谱与空间特征的联合建模，是高光谱图像分类中的重要研究方向。自监督预训练可减少对标注样本的依赖，但跨传感器与跨场景的泛化仍需要进一步验证。';
const markdown = `# 高光谱图像分类：研究现状与发展趋势\n\n## 摘要\n\n高光谱图像分类正从依赖人工特征，转向光谱与空间信息的联合学习。研究重点逐步延伸到少样本学习和跨场景泛化。[E0001]\n\n## 01 · 主要技术路线\n\n现有方法可以从特征表达和训练方式两个维度理解。**方法的适用条件与验证数据同样重要。**\n\n| 技术路线 | 主要特点 | 适用条件 |\n| --- | --- | --- |\n| 传统机器学习 | 特征可解释、计算开销较低 | 标注样本有限 |\n| 深度学习 | 联合建模光谱与空间结构 | 有足够训练数据 |\n\n## 02 · 当前挑战\n\n- 标注稀缺与类别不平衡。\n- 传感器差异带来的领域偏移。\n- 跨场景评测与实际部署之间的差距。[E0001]\n\n> 阅读结论时，应同时核对实验条件与证据范围。\n\n## 03 · 后续方向\n\n在统一的评测条件下比较模型，往往比孤立的最高准确率更有参考价值。\n\n\`[E0001]\` 是代码中的编号，不生成按钮。未知编号 [E9999] 不生成按钮。\n\n<img src="x" onerror="window.bad=true">\n\n[不安全链接](javascript:alert(1))`;

interface Scenario { reportCycle?: 'improving' | 'blocked'; completed: boolean; failed: boolean; interrupted: boolean; offline: boolean; queueFull: boolean; uncertain: boolean; postCount: number; resumeBodies: unknown[]; offsets: number[]; deletedIds: string[]; deleteRequests: string[]; deleteMode: 'ok' | 'busy' | 'uncertain'; checkpoint: 'summarize' | 'report' | 'save_report'; evidenceCount: number; citationCount: number }

async function mockApi(page: Page, overrides: Partial<Scenario> = {}): Promise<Scenario> {
  const scenario: Scenario = { completed: false, failed: false, interrupted: false, offline: false, queueFull: false, uncertain: false, postCount: 0, resumeBodies: [], offsets: [], deletedIds: [], deleteRequests: [], deleteMode: 'ok', checkpoint: 'summarize', evidenceCount: 4, citationCount: 0, ...overrides };
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
      const visible = items.filter(item => !scenario.deletedIds.includes(item.run_id));
      return route.fulfill({ json: { items: offset ? visible.filter(item => item.run_id === failedId) : visible, offset, limit: 10, has_more: offset === 0 && !scenario.deletedIds.includes(failedId) } });
    }
    if (path.endsWith('/iterations')) return route.fulfill({ json: { run_id: activeId, iteration: {
      drafts: [{ revision: 0, text: 'Original draft [E0001]', citation_ids: ['E0001'], review: { issues: [{ kind: 'missing_evidence', reason: 'Missing comparison', paragraph_id: '', evidence_ids: [], question: 'Compare methods', query: 'comparison query' }] }, review_error: null }],
      supplements: [{ task_id: 5, revision: 1, question: 'Compare methods', query: 'comparison query', novel_evidence_ids: ['E0002'] }],
    } } });
    if (path.endsWith('/events')) return route.fulfill({ contentType: 'text/event-stream', body: ': test heartbeat\n\n' });
    const id = path.split('/')[3];
    if (request.method() === 'DELETE' && id) {
      scenario.deleteRequests.push(id);
      if (scenario.deleteMode === 'busy') return route.fulfill({ status: 409, json: { error: { code: 'research_busy', message: '研究已排队或正在执行，请结束后再删除' } } });
      if (!scenario.deletedIds.includes(id)) scenario.deletedIds.push(id);
      if (scenario.deleteMode === 'uncertain') return route.abort('failed');
      return route.fulfill({ status: 204 });
    }
    if (scenario.deletedIds.includes(id ?? '') || ![activeId, completeId, failedId].includes(id ?? '')) return route.fulfill({ status: 404, json: { error: { code: 'run_not_found', message: '研究不存在' } } });
    if (path.endsWith('/resume')) {
      scenario.resumeBodies.push(request.postDataJSON()); scenario.failed = false; scenario.interrupted = false;
      return route.fulfill({ status: 202, json: { run_id: id, accepted: true } });
    }
    if (path.endsWith('/report')) return route.fulfill({ contentType: 'text/markdown; charset=utf-8', body: markdown });
    if (path.endsWith('/result')) return route.fulfill({ json: { run_id: id, report_markdown: markdown, citations: [{ evidence_id: 'E0001', title: '光谱与空间特征联合建模：研究综述', url: 'https://example.com/research/spectral-spatial', text: evidence }], tasks: [{ id: 1, title: '主要研究方法与技术演进', status: 'completed', summary: '任务摘要。[E0001]', citation_ids: ['E0001'] }] } });
    if (scenario.offline) return route.fulfill({ status: 503, json: { error: { code: 'unavailable', message: '查询暂时不可用' } } });
    const done = id === completeId || scenario.completed;
    const tasksDone = done || scenario.checkpoint !== 'summarize';
    const failed = scenario.failed || (id === failedId && !scenario.resumeBodies.length);
    const interrupted = scenario.interrupted;
    return route.fulfill({ json: {
      run_id: id, topic: done ? topic : id === activeId ? '检索增强生成的技术演进与应用边界' : '多智能体协作的架构与工程实践', updated_at: '2026-11-10T13:20:00+00:00', max_tasks: 3,
      status: done ? 'completed' : failed ? 'failed' : 'running', execution: done || failed || interrupted ? 'idle' : 'executing',
      next_nodes: done ? [] : [scenario.checkpoint], active_task_id: tasksDone ? null : 2, planned_tasks: 4, selected_tasks: 3,
      completed_tasks: tasksDone ? 2 : 1, skipped_tasks: tasksDone ? 1 : 0, report_ready: done, requires_recovery_confirmation: interrupted, error_message: failed ? '上次执行失败，可以恢复研究。' : null,
      report_iteration: scenario.reportCycle ? {
        revision: 1, max_revisions: 2, status: scenario.reportCycle, stop_reason: scenario.reportCycle === 'blocked' ? 'Unresolved evidence problem' : '',
        draft_count: 1, initial_total: 3, initial_completed: 3, supplemental_total: 1, supplemental_completed: 0, issue_count: 1,
      } : null,
      tasks: [
        { id: 1, title: '主要研究方法与技术演进', status: 'completed', selected: true, evidence_count: 5, citation_count: 3 },
        { id: 2, title: '关键技术、评测方法与公开数据集', status: tasksDone ? 'completed' : 'running', selected: true, evidence_count: scenario.evidenceCount, citation_count: tasksDone ? 2 : scenario.citationCount },
        { id: 3, title: '当前挑战与值得关注的发展方向', status: tasksDone ? 'skipped' : 'pending', selected: true, evidence_count: 0, citation_count: 0 },
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
  await expect(page.getByRole('heading', { name: '开始一项研究' })).toBeVisible();
  await expect(page.getByText('API 已连接')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('workspace-desktop.png'), fullPage: true });
  await page.getByRole('button', { name: /高光谱图像分类的研究现状/ }).click();
  await expect(page.getByLabel('你想研究什么？')).toHaveValue(/高光谱/);
  await page.getByRole('button', { name: '下一页研究' }).click();
  await expect(page.locator('.history-item')).toHaveCount(1);
  await expect(page.getByRole('button', { name: '上一页研究' })).toBeEnabled();
  expect(scenario.offsets).toContain(10);
  expect(errors).toEqual([]);
});

test('create, poll progress, receive report, click citation and download', async ({ page }, testInfo) => {
  const scenario = await mockApi(page);
  await page.goto('/');
  await page.getByLabel('你想研究什么？').fill('检索增强生成的技术演进');
  await page.getByRole('button', { name: '开始研究', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(activeId));
  await expect(page.getByRole('heading', { name: '关键技术、评测方法与公开数据集' })).toBeVisible();
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
  await expect(page.locator('.status-badge').filter({ hasText: '研究进行中' })).toBeVisible();
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
  await expect(page.getByRole('heading', { name: '关键技术、评测方法与公开数据集' })).toBeVisible();
  scenario.offline = true;
  await page.getByRole('button', { name: '刷新研究进度' }).click();
  await expect(page.getByText('查询暂时不可用')).toBeVisible();
  await expect(page.getByRole('heading', { name: '关键技术、评测方法与公开数据集' })).toBeVisible();
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
  await expect(page.getByRole('heading', { name: '开始一项研究' })).toBeVisible();
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

test('selected sidebar research can cancel deletion then delete with a 204 response', async ({ page }) => {
  const scenario = await mockApi(page);
  await page.goto(`/#/research/${completeId}`);
  await expect(page.locator('.prose')).toBeVisible();
  await expect(page.getByRole('button', { name: /^删除研究：/ })).toHaveCount(1);
  await page.getByRole('button', { name: /^删除研究：/ }).click();
  await page.getByRole('button', { name: '取消', exact: true }).click();
  expect(scenario.deleteRequests).toHaveLength(0);
  await page.getByRole('button', { name: /^删除研究：/ }).click();
  await page.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(page.getByRole('heading', { name: '开始一项研究' })).toBeVisible();
  await expect(page.getByText('研究已删除', { exact: true })).toBeVisible();
  await expect(page.locator('.history-item').filter({ hasText: topic })).toHaveCount(0);
  expect(scenario.deleteRequests).toEqual([completeId]);
  await page.reload();
  await expect(page.locator('.history-item')).toHaveCount(2);
});

test('busy deletion keeps research and reports backend conflict', async ({ page }) => {
  const scenario = await mockApi(page, { deleteMode: 'busy' });
  await page.goto(`/#/research/${activeId}`);
  await page.getByRole('button', { name: /^删除研究：/ }).click();
  await page.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('研究已排队或正在执行');
  await expect(page.getByRole('dialog')).toBeVisible();
  expect(scenario.deletedIds).toHaveLength(0);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await expect(page).toHaveURL(new RegExp(activeId));
});

test('uncertain delete remains retryable even if the server already removed the record', async ({ page }) => {
  const scenario = await mockApi(page, { deleteMode: 'uncertain' });
  await page.goto(`/#/research/${completeId}`);
  await page.getByRole('button', { name: /^删除研究：/ }).click();
  await page.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('请求不会自动重发');
  expect(scenario.deleteRequests).toEqual([completeId]);
  scenario.deleteMode = 'ok';
  await page.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(page.getByRole('heading', { name: '开始一项研究' })).toBeVisible();
  expect(scenario.deleteRequests).toEqual([completeId, completeId]);
});

test('deleting the last entry on a page returns history to previous page', async ({ page }) => {
  const scenario = await mockApi(page);
  await page.goto('/');
  await page.getByRole('button', { name: '下一页研究' }).click();
  await expect(page.locator('.history-item')).toHaveCount(1);
  await page.locator('.history-item').click();
  await page.getByRole('button', { name: /^删除研究：/ }).click();
  await page.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(page.getByRole('button', { name: '上一页研究' })).toBeDisabled();
  await expect(page.locator('.history-item')).toHaveCount(2);
  expect(scenario.deleteRequests).toEqual([failedId]);
});

test('sidebar collapses, persists after reload, and remains available on mobile', async ({ page }, testInfo) => {
  await mockApi(page);
  await page.goto('/');
  await page.getByRole('button', { name: '折叠侧栏', exact: true }).click();
  await expect(page.locator('#workspace-sidebar')).not.toBeVisible();
  await page.reload();
  await expect(page.locator('#workspace-sidebar')).not.toBeVisible();
  await expect(page.getByRole('button', { name: '展开侧栏' })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('workspace-collapsed.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: '切换研究历史' }).click();
  await expect(page.locator('#workspace-sidebar')).toBeVisible();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole('button', { name: '展开侧栏' }).click();
  await expect(page.locator('#workspace-sidebar')).toBeVisible();
});

test('report hierarchy and outline navigation preserve the research route', async ({ page }) => {
  await mockApi(page);
  await page.goto(`/#/research/${completeId}`);
  const outline = page.getByRole('navigation', { name: '报告目录' });
  await expect(outline).toBeVisible();
  await outline.getByRole('button', { name: '02 · 当前挑战' }).click();
  await expect(page).toHaveURL(new RegExp(`${completeId}$`));
  await expect(page.locator('.prose h2').filter({ hasText: '02 · 当前挑战' })).toBeFocused();
  const sizes = await page.locator('.prose').evaluate(element => ({
    title: parseFloat(getComputedStyle(element.querySelector('h1')!).fontSize),
    section: parseFloat(getComputedStyle(element.querySelector('h2')!).fontSize),
    body: parseFloat(getComputedStyle(element).fontSize),
  }));
  expect(sizes.title).toBeGreaterThan(sizes.section);
  expect(sizes.section).toBeGreaterThan(sizes.body);
});

test('progress updates reflect snapshots, keep an observed timeline and stop activity on errors', async ({ page }, testInfo) => {
  const scenario = await mockApi(page);
  await page.goto(`/#/research/${activeId}`);
  await expect(page.locator('.live-panel')).toHaveClass(/is-live/);
  await expect(page.locator('.checkpoint-label')).toHaveText('检查点待执行：生成摘要');
  await expect(page.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '33');
  const log = page.getByRole('list', { name: '进度更新记录' });
  await expect(log.locator('li')).toHaveCount(1);
  await page.getByRole('button', { name: '刷新研究进度' }).click();
  await expect(page.getByRole('button', { name: '刷新研究进度' })).toBeEnabled();
  await expect(log.locator('li')).toHaveCount(1);
  scenario.evidenceCount = 6;
  scenario.citationCount = 2;
  await page.getByRole('button', { name: '刷新研究进度' }).click();
  await expect(log).toContainText('任务 2：已保存 6 条证据');
  await expect(log).toContainText('任务 2：摘要引用 2 条证据');
  await page.screenshot({ path: testInfo.outputPath('live-progress.png'), fullPage: true });
  scenario.checkpoint = 'report';
  await page.getByRole('button', { name: '刷新研究进度' }).click();
  await expect(page.locator('.phase-track .current')).toContainText('报告汇总');
  await expect(page.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100');
  await expect(page.getByText('任务处理 100% · 不含报告生成')).toBeVisible();
  await expect(page.getByRole('tab', { name: '研究报告' })).toBeDisabled();
  scenario.offline = true;
  await page.getByRole('button', { name: '刷新研究进度' }).click();
  await expect(page.locator('.sync-state')).toHaveText('同步中断');
  await expect(page.locator('.activity-track')).toHaveCount(0);
  scenario.offline = false;
  scenario.completed = true;
  await page.getByRole('button', { name: '刷新研究进度' }).click();
  await expect(page.getByRole('tab', { name: '研究报告' })).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('tab', { name: '研究进度' }).click();
  await expect(log).toContainText('任务 2：已保存 6 条证据');
  await expect(page.locator('.phase-track .current')).toContainText('报告就绪');
});

test('mobile progress fits screen and sidebar deletion remains accessible', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await mockApi(page, { deleteMode: 'busy' });
  await page.goto(`/#/research/${activeId}`);
  await expect(page.locator('.live-panel')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('progress-mobile.png'), fullPage: true });
  await page.getByRole('button', { name: '切换研究历史' }).click();
  await page.getByRole('button', { name: /^删除研究：/ }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await expect(page.locator('#workspace-sidebar')).toBeVisible();
});


function streamFrame(id: number, type: string, data: Record<string, unknown>, execution = 'first'): string {
  return 'id: ' + id + '\nevent: research.event\ndata: ' + JSON.stringify({
    event_id: id, run_id: activeId, execution_id: execution, type, data,
  }) + '\n\n';
}

test('streamed draft is deduplicated on reconnect and replaced by final report', async ({ page }, testInfo) => {
  const scenario = await mockApi(page);
  let connections = 0;
  const cursors: string[] = [];
  await page.route('**/api/research/*/events?*', async route => {
    connections++;
    cursors.push(new URL(route.request().url()).searchParams.get('after') ?? '');
    const body = connections === 1
      ? 'retry: 100\n\n' + streamFrame(1, 'report.started', { generation_id: 'g1' })
        + streamFrame(2, 'report.delta', { generation_id: 'g1', text: '# Live draft\n\n' })
      : streamFrame(2, 'report.delta', { generation_id: 'g1', text: '# Live draft\n\n' })
        + streamFrame(3, 'report.delta', { generation_id: 'g1', text: '<img src=x onerror=alert(1)>Safe preview' });
    return route.fulfill({ contentType: 'text/event-stream', body });
  });
  await page.goto('/#/research/' + activeId);
  await expect(page.locator('.stream-body')).toHaveText('# Live draft\n\n<img src=x onerror=alert(1)>Safe preview');
  await expect(page.locator('.stream-body img')).toHaveCount(0);
  expect(cursors).toContain('2');
  await page.screenshot({ path: testInfo.outputPath('streaming-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('streaming-mobile.png'), fullPage: true });
  scenario.completed = true;
  await page.getByRole('button', { name: '刷新研究进度' }).click();
  await expect(page.locator('.prose')).toBeVisible();
  await expect(page.locator('.stream-draft')).toHaveCount(0);
});

test('failed execution replay does not stop the newer report generation', async ({ page }) => {
  await mockApi(page);
  await page.route('**/api/research/*/events?*', route => route.fulfill({
    contentType: 'text/event-stream',
    body: streamFrame(1, 'report.started', { generation_id: 'old' })
      + streamFrame(2, 'report.delta', { generation_id: 'old', text: 'Old failed draft' })
      + streamFrame(3, 'research.failed', {})
      + streamFrame(4, 'research.started', {}, 'second')
      + streamFrame(5, 'report.started', { generation_id: 'new' }, 'second')
      + streamFrame(6, 'report.delta', { generation_id: 'new', text: 'New report draft' }, 'second'),
  }));
  await page.goto('/#/research/' + activeId);
  await expect(page.locator('.stream-body')).toHaveText('New report draft');
  await page.reload();
  await expect(page.locator('.stream-body')).toHaveText('New report draft');
});

test('failed stream retains draft without enabling final report', async ({ page }) => {
  await mockApi(page, { failed: true });
  await page.route('**/api/research/*/events?*', route => route.fulfill({
    contentType: 'text/event-stream',
    body: streamFrame(1, 'report.started', { generation_id: 'old' })
      + streamFrame(2, 'report.delta', { generation_id: 'old', text: 'Unvalidated draft' })
      + streamFrame(3, 'research.failed', {})
      + 'event: research.sync\ndata: {"terminal":true}\n\n',
  }));
  await page.goto('/#/research/' + activeId);
  await expect(page.locator('.stream-body')).toHaveText('Unvalidated draft');
  await expect(page.getByText('生成已暂停 · 草稿未定稿')).toBeVisible();
  await expect(page.getByRole('tab', { name: '研究报告' })).toBeDisabled();
  await expect(page.getByRole('button', { name: /继续研究/ }).first()).toBeEnabled();
});


test('report iteration shows history and supplement without publishing draft', async ({ page }, testInfo) => {
  await mockApi(page, { reportCycle: 'improving' });
  await page.goto('/#/research/' + activeId);
  const panel = page.getByRole('region', { name: '报告迭代' });
  await expect(panel).toContainText('初始任务已处理 3 / 3');
  await panel.locator('summary').first().click();
  await expect(panel).toContainText('Missing comparison');
  await panel.getByText('阅读本版正文', { exact: true }).click();
  await expect(panel.locator('pre')).toHaveText('Original draft [E0001]');
  await expect(panel).toContainText('comparison query');
  await expect(page.getByRole('tab', { name: '研究报告' })).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath('report-iteration.png'), fullPage: true });
});

test('blocked report keeps draft readable and prevents useless resume', async ({ page }) => {
  await mockApi(page, { reportCycle: 'blocked', failed: true });
  await page.goto('/#/research/' + activeId);
  await expect(page.getByRole('button', { name: '继续研究', exact: true })).toBeDisabled();
  await expect(page.getByRole('tab', { name: '研究报告' })).toBeDisabled();
  const panel = page.getByRole('region', { name: '报告迭代' });
  await expect(panel).toContainText('Unresolved evidence problem');
  await panel.locator('summary').first().click();
  await expect(panel).toContainText('Missing comparison');
});
