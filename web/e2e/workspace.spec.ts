import { execFileSync } from 'node:child_process';
import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';
import { chromium, expect, test } from '@playwright/test';

const pages = [
  ['/', '总览'], ['/feed', '情报流'], ['/topics', '选题库'], ['/accounts', '对标账号'],
  ['/search', '关键词搜索'], ['/jobs', '采集任务'], ['/logs', '运行日志'], ['/settings', '系统设置'],
];

test('all pages open directly and preserve browser navigation', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  for (const [path, title] of pages) {
    await page.goto(path);
    await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
    await expect(page.getByRole('navigation').getByRole('link', { name: title })).toHaveAttribute('aria-current', 'page');
    await page.getByRole('button', { name: '快速采集' }).click();
    await expect(page.getByRole('dialog', { name: '快速采集' })).toBeVisible();
    await page.keyboard.press('Escape');
  }
  await page.getByRole('link', { name: 'CreatorRadar 创作者工作台' }).click();
  await expect(page.getByRole('heading', { name: '总览' })).toBeVisible();
  await page.getByRole('navigation').getByRole('link', { name: '采集任务' }).click();
  await page.getByLabel('任务状态').selectOption('queued');
  await expect(page).toHaveURL(/status=queued/);
  await page.reload();
  await expect(page.getByLabel('任务状态')).toHaveValue('queued');
  await page.goBack();
  await expect(page.getByLabel('任务状态')).toHaveValue('');
  await page.goBack();
  await expect(page.getByRole('heading', { name: '总览' })).toBeVisible();
  await page.goForward();
  await expect(page.getByRole('heading', { name: '采集任务' })).toBeVisible();
  await expect(page.getByText('v0.2.0')).toBeVisible();
  expect(errors).toEqual([]);
});

test('workspace theme follows the system and remembers explicit choices', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/settings');
  const theme = page.getByLabel('主题');
  const root = page.locator('html');
  await expect(theme).toHaveValue('system');
  await expect(root).toHaveCSS('color-scheme', 'light');
  const lightSurface = await page.locator('.topbar').evaluate((el) => getComputedStyle(el).backgroundColor);

  await page.emulateMedia({ colorScheme: 'dark' });
  await expect(root).toHaveCSS('color-scheme', 'dark');
  await expect(page.locator('.topbar')).not.toHaveCSS('background-color', lightSurface);
  const darkSurface = await page.locator('.topbar').evaluate((el) => getComputedStyle(el).backgroundColor);

  await theme.selectOption('light');
  await expect(root).toHaveCSS('color-scheme', 'light');
  await page.reload();
  await expect(theme).toHaveValue('light');
  await expect(root).toHaveCSS('color-scheme', 'light');

  await theme.selectOption('dark');
  await page.emulateMedia({ colorScheme: 'light' });
  await page.reload();
  await expect(theme).toHaveValue('dark');
  await expect(root).toHaveCSS('color-scheme', 'dark');
  await page.getByRole('link', { name: '总览' }).click();
  await page.getByRole('button', { name: '快速采集' }).click();
  const dialog = page.getByRole('dialog', { name: '快速采集' });
  await expect(dialog).toHaveCSS('color-scheme', 'dark');
  await expect(dialog).toHaveCSS('background-color', darkSurface);
  await page.keyboard.press('Escape');

  await page.getByRole('link', { name: '系统设置' }).click();
  await theme.selectOption('system');
  await expect(root).toHaveCSS('color-scheme', 'light');
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect(root).toHaveCSS('color-scheme', 'dark');
});

test('empty workspace and quick capture validation', async ({ page, request }) => {
  const jobs = await request.get('/api/v1/jobs');
  expect(await jobs.json()).toEqual({ items: [], next_cursor: null });
  await page.goto('/');
  await expect(page.getByText('暂无任务')).toBeVisible();
  await page.getByRole('button', { name: '快速采集' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('作品链接').fill('https://space.bilibili.com/123');
  await dialog.getByRole('button', { name: '提交采集' }).click();
  await expect(dialog.getByRole('alert')).toHaveText('这是创作者主页，请输入单条作品链接或分享短链。');
});

function enqueue(scenario: string, seconds: number): string {
  const cwd = fileURLToPath(new URL('../../backend/', import.meta.url));
  return JSON.parse(execFileSync(fileURLToPath(new URL(
    '../../backend/.venv/Scripts/python.exe', import.meta.url,
  )), [
    '-X', 'utf8', '-m', 'creator_radar', 'enqueue-test', '--scenario', scenario, '--seconds', String(seconds),
  ], { cwd, encoding: 'utf8' })).job_id;
}

test('close the browser during a job, then retry through the task and log pages', async ({ browser, request, baseURL }) => {
  test.setTimeout(45_000);
  expect(process.env.CREATOR_RADAR_WORKSPACE).toBe('acceptance');
  const isolatedBrowser = await chromium.launch({ channel: process.env.CREATOR_RADAR_TEST_BROWSER });
  const id = enqueue('fail_once', 5);
  try {
    const first = await isolatedBrowser.newPage({ baseURL });
    await first.goto('/jobs?job_id=' + id);
    const drawer = first.getByRole('dialog', { name: '任务详情' });
    await expect(drawer.getByText('正在执行任务')).toBeVisible();
  } finally {
    await isolatedBrowser.close();
  }
  await expect.poll(async () => (await (await request.get('/api/v1/jobs/' + id)).json()).status)
    .toBe('failed');

  const context = await browser.newContext({ baseURL, permissions: ['clipboard-read', 'clipboard-write'] });
  const page = await context.newPage();
  await page.goto('/jobs?job_id=' + id);
  const detail = page.getByRole('dialog', { name: '任务详情' });
  await detail.getByRole('button', { name: '重试任务' }).click();
  await expect(detail.getByText('验收任务执行完成').first()).toBeVisible({ timeout: 10_000 });
  await expect(detail.getByText('第 2 次')).toBeVisible();
  await detail.getByRole('link', { name: '查看日志' }).click();
  await expect(page).toHaveURL('/logs?job_id=' + id);
  await page.getByLabel('日志级别').selectOption('error');
  await expect(page.getByText('验收任务首次执行失败')).toBeVisible();
  await page.getByRole('button', { name: '复制当前日志' }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain('验收任务首次执行失败');
  await page.getByRole('link', { name: id }).click();
  await expect(page.getByRole('dialog', { name: '任务详情' })).toBeVisible();
  await context.close();
});

test('cancel an executing job in the browser', async ({ page }) => {
  const id = enqueue('success', 30);
  await page.goto('/logs?job_id=' + id);
  await expect(page.getByRole('table').getByText('正在执行任务')).toBeVisible();
  await page.getByRole('button', { name: '取消任务' }).click();
  await expect(page.getByRole('table').getByText('任务已取消')).toBeVisible();
});

test('retry one platform from a partial batch', async ({ page }) => {
  const id = enqueue('batch', 0.5);
  await page.goto('/jobs?job_id=' + id);
  const detail = page.getByRole('dialog', { name: '任务详情' });
  await expect(detail.getByText('部分成功', { exact: true })).toBeVisible();
  const xiaohongshu = detail.getByRole('listitem').filter({ hasText: '小红书' });
  await xiaohongshu.getByRole('button', { name: '重试此项' }).click();
  await expect(xiaohongshu.getByText('成功', { exact: true })).toBeVisible();
  await expect(detail.getByRole('listitem').filter({ hasText: 'B站' }).getByText('待登录')).toBeVisible();
});

test('discover, adopt, scan and revisit one topic through quick capture', async ({ page, request }) => {
  const settings = await request.get('/api/v1/settings');
  test.skip((await settings.json()).integrations.crawler.state !== 'fixture', '需要隔离采集样例');

  await page.goto('/search');
  await page.getByLabel('关键词', { exact: true }).fill('分平台验收');
  await page.getByRole('button', { name: '开始搜索' }).click();
  await expect(page.getByText('平台限制了请求，请稍后重试。')).toBeVisible();
  const bili = page.locator('.work-card').filter({ hasText: 'B站' });
  await expect(bili.getByLabel(/播放 0/)).toBeVisible();
  await page.getByRole('button', { name: '重试此平台' }).click();
  await expect(page.locator('.work-card').filter({ hasText: '小红书' })).toBeVisible();
  const searchUrl = page.url();
  await bili.getByRole('button', { name: '详情' }).click();
  await page.getByRole('button', { name: '加入选题库' }).click();
  await expect(page.getByRole('dialog', { name: '选题详情' })).toBeVisible();
  const topicUrl = page.url();

  await page.goto('/feed');
  await expect(page.getByText('暂无情报作品')).toBeVisible();
  await page.goto('/accounts');
  await page.getByRole('button', { name: '添加账号' }).click();
  await page.getByLabel('创作者主页链接').fill('https://space.bilibili.com/434377496');
  await page.getByRole('button', { name: '解析并添加' }).click();
  await page.getByRole('link', { name: '查看账号' }).click();
  await page.getByRole('button', { name: '关闭' }).click();
  await page.getByRole('button', { name: '扫描当前范围' }).click();
  await expect(page.getByText('获取 1 条作品 · 新增 1 条 · 更新 0 条')).toBeVisible();
  await page.goto('/');
  const recentWorks = page.getByRole('link', { name: /近 24 小时作品/ });
  await expect(recentWorks).toHaveText(/近 24 小时作品\s*1/);
  await recentWorks.click();
  await expect(page).toHaveURL('/feed?recent=24h');
  await expect(page.getByText('1 条作品', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '清除近 24 小时筛选' }).click();
  await expect(page).toHaveURL('/feed');
  await page.setViewportSize({ width: 1280, height: 440 });
  await page.goto('/feed');
  await page.getByLabel('检索作品').fill('收纳');
  await expect(page.locator('.work-card')).toHaveCount(1);
  await expect(page.getByRole('link', { name: '原链接' })).toHaveAttribute('target', '_blank');
  const detail = page.getByRole('button', { name: '详情' });
  await detail.scrollIntoViewIfNeeded();
  const scroll = await page.evaluate(() => window.scrollY);
  expect(scroll).toBeGreaterThan(0);
  await detail.click();
  await expect(page.getByRole('link', { name: '查看选题' })).toBeVisible();
  await page.getByRole('button', { name: '关闭' }).click();
  await expect(page.getByLabel('检索作品')).toHaveValue('收纳');
  expect(await page.evaluate(() => window.scrollY)).toBe(scroll);

  await page.getByRole('button', { name: '快速采集' }).click();
  await page.getByLabel('作品链接').fill('https://www.bilibili.com/video/BV1Fixture01');
  await page.getByRole('button', { name: '提交采集' }).click();
  await page.getByRole('link', { name: '查看选题' }).click();
  await expect(page).toHaveURL(topicUrl);
  await page.getByRole('button', { name: '归档选题' }).click();
  await expect(page.getByRole('button', { name: '恢复选题' })).toBeVisible();
  await page.getByRole('button', { name: '关闭' }).click();
  await page.getByLabel('选题状态').selectOption('true');
  await expect(page.locator('.work-card')).toHaveCount(1);
  await page.goto(searchUrl);
  await expect(page.locator('.work-card').filter({ hasText: 'B站' })).toBeVisible();
});

test('platform login and daily scan settings are connected to the workspace', async ({ page, request }) => {
  const settings = await request.get('/api/v1/settings');
  test.skip((await settings.json()).integrations.crawler.state !== 'fixture', '需要隔离采集样例');
  await page.goto('/settings?platform=bilibili');
  const row = page.locator('.session-row').filter({ hasText: 'B站' });
  await row.getByRole('button', { name: /登录/ }).click();
  await expect(row.getByText('会话可用')).toBeVisible();
  await expect.poll(async () => {
    const login = await request.get('/api/v1/jobs?kind=platform_login&platform=bilibili');
    return (await login.json()).items[0]?.status;
  }).toBe('succeeded');
  const account = await request.post('/api/v1/accounts', {
    data: { url: 'https://space.bilibili.com/434377496' },
  });
  const { job_id } = await account.json();
  await expect.poll(async () => (await (await request.get('/api/v1/jobs/' + job_id)).json()).status)
    .toBe('succeeded');
  await expect(page.getByRole('heading', { name: '定时扫描' })).toHaveCount(0);
  await page.getByRole('link', { name: '采集任务' }).click();
  await expect(page.getByRole('heading', { name: '定时扫描' })).toBeVisible();
  await page.getByLabel('执行时间').fill('08:35');
  await page.getByRole('group', { name: '采集范围' }).getByLabel('B站').check();
  await page.getByRole('button', { name: '保存扫描计划' }).click();
  await expect(page.getByText('扫描计划已保存')).toBeVisible();
  await expect(page.getByRole('button', { name: '保存扫描计划' })).toBeDisabled();
  await page.reload();
  await expect(page.getByLabel('执行时间')).toHaveValue('08:35');
  await expect(page.getByRole('group', { name: '采集范围' }).getByLabel('B站')).toBeChecked();
  await page.getByRole('button', { name: '采集B站' }).click();
  await page.getByRole('link', { name: '查看本次日志' }).click();
  await expect(page).toHaveURL(/\/logs\?job_id=/);
  await expect(page.getByRole('heading', { name: /账号扫描/ })).toBeVisible();
});

test('transcribe and cite a topic, retaining the result after a failed rerun', async ({ page, request, context }) => {
  test.skip(!process.env.CREATOR_RADAR_CRAWLER_FIXTURE_MEDIA, '需要隔离有声视频样例');
  test.setTimeout(120_000);
  let key = 'e2e-local-key';
  let invalidReference = false;
  const service = createServer((req, res) => {
    if (req.url !== '/v1/chat/completions' || req.headers.authorization !== `Bearer ${key}`) {
      res.writeHead(401).end();
      return;
    }
    req.resume();
    req.on('end', () => {
      const point = { text: '整理桌面与创作材料', refs: [invalidReference ? 9999 : 1] };
      const content = {
        summary: point, hook: point, structure: [point], key_points: [point],
        techniques: [point], extensions: [point],
      };
      res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({
        choices: [{ finish_reason: 'stop', message: { content: JSON.stringify(content) } }],
      }));
    });
  });
  await new Promise<void>((resolve) => service.listen(0, '127.0.0.1', resolve));
  const address = service.address();
  if (!address || typeof address === 'string') throw new Error('Local AI test server could not bind');
  try {
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.goto('/settings');
    await page.getByLabel('ASR 引擎').selectOption('faster-whisper');
    await page.getByLabel('转写语言').selectOption('auto');
    await page.getByRole('button', { name: '保存 ASR 设置' }).click();
    await expect(page.getByText('ASR 设置已保存')).toBeVisible();
    await page.getByLabel('AI 服务地址').fill(`http://127.0.0.1:${address.port}/v1`);
    await page.getByLabel('AI 模型').fill('fixture-model');
    await page.getByLabel('API Key', { exact: true }).fill(key);
    await page.getByRole('button', { name: '保存 AI 设置' }).click();
    await expect(page.getByText('AI 设置已保存')).toBeVisible();
    await page.getByRole('button', { name: '测试 AI 连接' }).click();
    await expect(page.getByText('连接成功，服务返回了可用的拆解结果')).toBeVisible();
    await page.reload();
    await expect(page.getByLabel('ASR 引擎')).toHaveValue('faster-whisper');
    await expect(page.getByLabel('转写语言')).toHaveValue('auto');
    await expect(page.getByLabel('API Key', { exact: true })).toHaveValue('');
    key = 'e2e-replacement-key';
    await page.getByLabel('API Key', { exact: true }).fill(key);
    await page.getByRole('button', { name: '保存 AI 设置' }).click();
    await expect(page.getByText('AI 设置已保存')).toBeVisible();

    await page.getByRole('button', { name: '快速采集' }).click();
    const videoId = `BV1E2E${Date.now()}`;
    await page.getByLabel('作品链接').fill(`https://www.bilibili.com/video/${videoId}`);
    await page.getByRole('button', { name: '提交采集' }).click();
    await page.getByRole('link', { name: '查看选题' }).click();
    const drawer = page.getByRole('dialog', { name: '选题详情' });
    const asrTab = drawer.getByRole('button', { name: '逐字稿', exact: true });
    const aiTab = drawer.getByRole('button', { name: 'AI 拆解', exact: true });
    await aiTab.click();
    await expect(drawer.getByRole('button', { name: '生成拆解' })).toBeDisabled();
    await asrTab.click();
    await drawer.getByRole('button', { name: '开始转写' }).click();
    await expect(drawer.locator('.transcript li').first()).toBeVisible({ timeout: 45_000 });
    await drawer.getByRole('button', { name: '复制逐字稿' }).click();
    await expect(drawer.getByText('已复制逐字稿')).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toContain('桌面');
    await aiTab.click();
    await drawer.getByRole('button', { name: '生成拆解' }).click();
    await expect(drawer.getByRole('heading', { name: '内容摘要' })).toBeVisible();
    const evidence = drawer.locator('details.evidence').first();
    await evidence.locator('summary').click();
    await expect(evidence.locator('blockquote')).toBeVisible();

    await asrTab.click();
    await drawer.getByRole('button', { name: '重新转写' }).click();
    await expect(drawer.getByText('转写任务处理中')).toBeVisible();
    await expect(drawer.getByRole('button', { name: '重新转写' })).toBeEnabled({ timeout: 45_000 });
    await aiTab.click();
    await expect(drawer.getByText(/逐字稿或作品简介已更新/)).toBeVisible();
    invalidReference = true;
    await drawer.getByRole('button', { name: '重新生成' }).click();
    await expect(drawer.getByText('AI 返回的依据超出输入范围，可重试。')).toBeVisible();
    await expect(drawer.getByRole('heading', { name: '内容摘要' })).toBeVisible();
    await drawer.getByRole('link', { name: '查看日志' }).click();
    await expect(page.getByRole('heading', { name: '运行日志' })).toBeVisible();
    await expect(page.getByRole('table').getByText('AI 返回的依据超出输入范围，可重试。')).toBeVisible();

    invalidReference = false;
    await page.getByRole('button', { name: '快速采集', exact: true }).click();
    const articleId = Date.now().toString(16).padStart(24, '0');
    await page.getByLabel('作品链接').fill(`https://www.xiaohongshu.com/explore/${articleId}`);
    await page.getByRole('button', { name: '提交采集' }).click();
    await page.getByRole('link', { name: '查看选题' }).click();
    await aiTab.click();
    await drawer.getByRole('button', { name: '生成拆解' }).click();
    await expect(drawer.getByRole('heading', { name: '内容摘要' })).toBeVisible();
    await drawer.locator('details.evidence').first().locator('summary').click();
    await expect(drawer.locator('details.evidence').first().locator('blockquote')).toContainText('记录桌面收纳');

    await page.goto('/settings');
    await page.getByRole('button', { name: '清除 Key' }).click();
    await expect(page.getByText('已清除 API Key')).toBeVisible();
  } finally {
    service.close();
    await request.delete('/api/v1/settings/ai-key', { timeout: 5_000 }).catch(() => undefined);
  }
});

test('run a queue self-test and clear its event history', async ({ page, request }) => {
  expect((await (await request.get('/api/v1/settings')).json()).workspace).toBe('acceptance');
  await page.goto('/logs?level=error');
  await page.getByRole('button', { name: '运行队列自检' }).click();
  await expect(page).toHaveURL(/\/logs\?job_id=[a-f0-9]{32}$/);
  await expect(page.getByRole('table').getByText('队列自检通过')).toBeVisible();
  await page.getByRole('button', { name: '清空已完成任务日志' }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认清空' }).click();
  await expect(page.getByText('没有匹配的日志')).toBeVisible();
  await expect(page.getByRole('heading', { name: /队列自检/ })).toBeVisible();
  const id = new URL(page.url()).searchParams.get('job_id');
  expect((await (await request.get('/api/v1/jobs/' + id)).json()).status).toEqual('succeeded');
});
