import { useMutation, useQueryClient } from '@tanstack/react-query';
import { CalendarDays, RefreshCw } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { displayTime, platforms, refreshWorkspace, sessionLabels, useApi, write,
  type Health, type Job, type Overview, type Platform as PlatformId, type ScanPlan, type Settings } from './api';
import { LoadState, PageTitle, Platform, Status } from './ui';

type PlanStatus = { eligible_accounts: number; next_run_at: string | null; last_job: Job | null };
type ScanResponse = { job_id: string; job_ids: string[] };
const platformIds = Object.keys(platforms) as PlatformId[];

export function Collection() {
  const client = useQueryClient();
  const settings = useApi<Settings>('settings');
  const overview = useApi<Overview>('overview');
  const health = useApi<Health>('health', '', true, 5_000);
  const plan = useApi<PlanStatus>('scan-plan', '', true, 30_000);
  const data = settings.data;
  const counts = overview.data?.platforms;
  const canScan = data && data.integrations.crawler.state !== 'unavailable';
  const hasAccounts = counts?.some(({ platform, accounts }) => accounts > 0 && data?.enabled_platforms.includes(platform));
  const scan = useMutation({
    mutationFn: (platform?: PlatformId) => write<ScanResponse>('account-scans', 'POST', platform ? { platform } : {}),
    onSuccess: () => refreshWorkspace(client),
  });
  return <>
    <PageTitle title="采集任务" description="扫描对标账号，管理每日计划，查看采集与处理任务。"
      action={<button className="button primary" disabled={!canScan || !hasAccounts || scan.isPending}
        onClick={() => scan.mutate(undefined)}><RefreshCw size={16} />全平台采集</button>} />
    <LoadState pending={settings.isPending || overview.isPending} error={settings.error || overview.error} />
    {data?.integrations.crawler.state === 'unavailable' && <div className="callout">
      采集环境未就绪。<Link className="text-button" to="/settings">查看平台设置</Link>
    </div>}
    {health.data?.worker === 'offline' && <p className="callout">Worker 离线，已提交的任务会等待服务恢复。</p>}
    {scan.error && <p className="callout form-error" role="alert">{scan.error.message}</p>}
    {scan.data && <div className="callout" role="status">
      已提交 {scan.data.job_ids.length} 个账号的扫描。
      {scan.data.job_ids.length === 1
        ? <Link className="text-button" to={'/logs?job_id=' + scan.data.job_id}>查看本次日志</Link>
        : <Link className="text-button" to="/jobs?kind=account_scan">查看扫描任务</Link>}
    </div>}
    {data && <section className="platform-collection" aria-label="分平台采集">
      {platformIds.map((id) => {
        const enabled = data.enabled_platforms.includes(id);
        const session = data.sessions[id];
        const count = counts?.find(({ platform }) => platform === id)?.accounts;
        return <article className="panel collection-platform" key={id}>
          <div><Platform id={id} /><small>{enabled
            ? session.job_id ? '正在登录' : sessionLabels[session.state] || '待登录'
            : '已停用'}</small></div>
          <Link className="text-button" to={'/accounts?platform=' + id}>{count ?? '—'} 个跟踪账号</Link>
          <button className="button" disabled={!canScan || !enabled || !count || scan.isPending}
            onClick={() => scan.mutate(id)}>采集{platforms[id]}</button>
        </article>;
      })}
    </section>}
    {data && <ScanSchedule saved={data.scan} enabledPlatforms={data.enabled_platforms}
      status={plan.data} statusError={plan.error} />}
  </>;
}

function ScanSchedule({ saved, enabledPlatforms, status, statusError }: {
  saved: ScanPlan; enabledPlatforms: PlatformId[]; status?: PlanStatus; statusError: Error | null;
}) {
  const [draft, setDraft] = useState<ScanPlan | null>(null);
  const scan = draft || saved;
  const client = useQueryClient();
  const save = useMutation({
    mutationFn: () => write<Settings>('settings', 'PATCH', { scan: { ...scan, platforms: scan.platforms || [] } }),
    onSuccess: (data) => {
      client.setQueryData(['settings', ''], data);
      setDraft(null);
      return refreshWorkspace(client, ['scan-plan']);
    },
  });
  const scope = scan.platforms || [];
  function change(fields: Partial<ScanPlan>) {
    setDraft({ ...scan, ...fields });
    save.reset();
  }
  const lastJob = status?.last_job;
  return <form className="panel settings-panel collection-schedule" onSubmit={(event) => {
    event.preventDefault();
    if (draft && !save.isPending) save.mutate();
  }}>
    <div className="section-title"><h2><CalendarDays size={19} />定时扫描</h2></div>
    <fieldset disabled={save.isPending}>
      <label className="check-label"><input type="checkbox" checked={scan.enabled}
        onChange={(event) => change({ enabled: event.target.checked })} />启用每日扫描</label>
      <div className="form-grid">
        <label className="field"><span>执行时间</span><input type="time" required value={scan.time}
          onChange={(event) => change({ time: event.target.value })} /></label>
        <label className="field"><span>默认回看天数</span><input type="number" min={1} max={365} required value={scan.lookback_days || ''}
          onChange={(event) => change({ lookback_days: Number(event.target.value) })} /></label>
        <label className="field"><span>单次采集上限</span><input type="number" min={1} max={100} required value={scan.limit || ''}
          onChange={(event) => change({ limit: Number(event.target.value) })} /></label>
      </div>
      <fieldset className="schedule-platforms"><legend>采集范围</legend>
        {platformIds.map((id) => <label key={id}>
          <input type="checkbox" checked={scope.includes(id)}
            onChange={(event) => change({ platforms: event.target.checked
              ? [...scope, id] : scope.filter((value) => value !== id) })} />
          {platforms[id]}{enabledPlatforms.includes(id) ? '' : '（已停用）'}
        </label>)}
        <small>未勾选时扫描全部已启用平台。</small>
      </fieldset>
    </fieldset>
    <p className="field-note">执行时间按服务机器的本地时间计算。</p>
    {status && <div className="schedule-status">
      <div><small>下次执行</small><strong>{!saved.enabled ? '计划已暂停'
        : status.next_run_at?.slice(0, 16).replace('T', ' ') || '暂无可扫描账号'}</strong></div>
      <div><small>计划覆盖</small><strong>{status.eligible_accounts} 个账号</strong></div>
      <div><small>最近一次定时任务</small>{lastJob
        ? <div className="inline-actions"><Status value={lastJob.status} />
          <Link to={'/logs?job_id=' + lastJob.id} className="text-button">{lastJob.title}</Link>
          <span>{displayTime(lastJob.created_at)}</span>
          {typeof lastJob.result?.added === 'number' && <span>
            新增 {lastJob.result.added} 条 · 更新 {String(lastJob.result.updated ?? 0)} 条</span>}
        </div> : <strong>尚未执行</strong>}</div>
    </div>}
    <LoadState pending={false} error={save.error || statusError} />
    <div className="inline-actions">
      <button className="button primary" disabled={!draft || save.isPending}>保存扫描计划</button>
      {save.isSuccess && <span role="status">扫描计划已保存</span>}
    </div>
  </form>;
}
