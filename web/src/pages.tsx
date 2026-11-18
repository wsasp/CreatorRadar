import { useState, type FormEvent } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import {
  BookOpen, ChevronRight, Link2, ListTodo, Palette,
  Radio, Users, Zap,
} from 'lucide-react';
import { displayTime, platforms, refreshWorkspace, sessionLabels, types, useApi, write,
  type Health, type Overview, type Platform as PlatformId, type Settings } from './api';
import { Dialog, Empty, LoadState, PageTitle, Platform, Status } from './ui';
import { AiPanel, AsrPanel, StoragePanel } from './processing-settings';
import type { ThemePreference } from './theme';

export function OverviewPage() {
  const query = useApi<Overview>('overview', '', true, 60_000);
  const data = query.data;
  const stats = [
    ['/feed?recent=24h', Radio, '近 24 小时作品', data?.works_24h],
    ['/accounts', Users, '跟踪中账号', data?.tracked_accounts],
    ['/topics', BookOpen, '已收录选题', data?.active_topics],
    ['/jobs?status=running', ListTodo, '运行中任务', data?.running_jobs],
  ] as const;
  return <>
    <PageTitle title="总览" description="查看账号跟踪、选题处理和采集任务状态。" />
    <LoadState pending={query.isPending} error={query.error} />
    {data && <>
      <div className="overview-stats">{stats.map(([path, Icon, label, value]) =>
        <Link key={path} to={path} className="panel overview-stat">
          <span className="overview-stat-icon"><Icon size={19} /></span>
          <span><small>{label}</small><strong>{value}</strong></span>
          <ChevronRight size={17} className="overview-stat-arrow" />
        </Link>)}</div>
      <div className="overview-panels">
        <section className="panel overview-panel">
          <div className="section-title"><h2>平台分布</h2><Link className="text-button" to="/accounts">对标账号 <ChevronRight size={14} /></Link></div>
          <div className="overview-platforms">
            <div className="overview-platform-head"><span>平台</span><span>跟踪账号</span><span>情报作品</span></div>
            {data.platforms.map(({ platform, accounts, works }) => <div key={platform}>
              <Platform id={platform} /><strong>{accounts}</strong><strong>{works}</strong>
            </div>)}
          </div>
        </section>
        <section className="panel overview-panel">
          <div className="section-title"><h2>选题处理进度</h2><Link className="text-button" to="/topics">选题库 <ChevronRight size={14} /></Link></div>
          <div className="overview-progress">{([
            ['视频转写', data.transcription], ['AI 拆解', data.analysis],
          ] as const).map(([label, { completed, total }]) => <div className="overview-progress-row" key={label}>
            <div><span>{label}</span><strong>{completed} / {total}</strong></div>
            <progress aria-label={label} max={total || 1} value={completed} />
          </div>)}</div>
        </section>
      </div>
      <section className="panel overview-panel">
        <div className="section-title"><h2>最近任务</h2><Link className="text-button" to="/jobs">采集任务 <ChevronRight size={14} /></Link></div>
        {data.recent_jobs.length ? <ul className="overview-jobs">{data.recent_jobs.map((job) => <li key={job.id}>
          <div><strong><Link to={`/jobs?job_id=${job.id}`}>{types[job.kind]}</Link></strong><p>{job.title}</p></div>
          <span className="overview-job-platform">{job.platform ? <Platform id={job.platform} /> : job.is_batch ? '多平台' : '—'}</span>
          <time dateTime={job.created_at}>{displayTime(job.created_at)}</time><Status value={job.status} />
        </li>)}</ul> : <Empty title="暂无任务" icon={ListTodo} />}
      </section>
    </>}
  </>;
}

export function SettingsPage({ theme, changeTheme }: {
  theme: ThemePreference; changeTheme: (theme: ThemePreference) => void;
}) {
  const query = useApi<Settings>('settings');
  const health = useApi<Health>('health', '', true, 5_000);
  const data = query.data;
  return <>
    <PageTitle title="系统设置" description="管理外观、平台登录、转写、AI 服务和本地存储。" />
    <section className="panel settings-panel appearance-panel">
      <h3><Palette size={18} />外观</h3>
      <label className="field">
        <span>主题</span>
        <select value={theme} onChange={(event) => changeTheme(event.target.value as ThemePreference)}>
          <option value="system">跟随系统</option>
          <option value="light">浅色</option>
          <option value="dark">深色</option>
        </select>
      </label>
    </section>
    <LoadState pending={query.isPending} error={query.error} />
    {data && <div className="settings-grid">
      <PlatformsPanel data={data} />
      <AsrPanel data={data} />
      <AiPanel data={data} />
      <StoragePanel data={data} worker={health.isError ? undefined : health.data?.worker} />
    </div>}
  </>;
}

export function QuickCapture({ close }: { close: () => void }) {
  const [link, setLink] = useState('');
  const navigate = useNavigate();
  const client = useQueryClient();
  const capture = useMutation({
    mutationFn: () => write<{ job_id: string }>('quick-captures', 'POST', { url: link.trim() }),
    onSuccess: ({ job_id }) => {
      void refreshWorkspace(client);
      close();
      navigate('/jobs?job_id=' + job_id);
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    if (link.trim() && !capture.isPending) capture.mutate();
  }
  return <Dialog title="快速采集" description="粘贴一条作品链接，采集成功后自动加入选题库。" close={close}>
    <form className="modal-form" onSubmit={submit}>
      <label className="field"><span>作品链接</span><span className="input-icon"><Link2 size={17} />
        <input autoFocus required maxLength={4096} value={link} placeholder="https://…" onChange={(event) => {
          setLink(event.target.value);
          capture.reset();
        }} />
      </span></label>
      <small className="field-note">支持抖音、小红书和 B 站的作品链接及分享短链。</small>
      {capture.error && <p className="form-error" role="alert">{capture.error.message}</p>}
      <div className="modal-actions">
        <button type="button" className="button" onClick={close}>取消</button>
        <button className="button primary" disabled={!link.trim() || capture.isPending}><Zap size={16} />提交采集</button>
      </div>
    </form>
  </Dialog>;
}

function PlatformsPanel({ data }: { data: Settings }) {
  const client = useQueryClient();
  const [params] = useSearchParams();
  const crawler = data.integrations.crawler;
  const enable = useMutation({
    mutationFn: (enabled_platforms: PlatformId[]) => write<Settings>('settings', 'PATCH', { enabled_platforms }),
    onSuccess: (settings) => {
      client.setQueryData(['settings', ''], settings);
      void refreshWorkspace(client);
    },
  });
  const login = useMutation({
    mutationFn: (id: PlatformId) => write<{ job_id: string }>('platforms/' + id + '/login', 'POST'),
    onSuccess: () => refreshWorkspace(client),
  });
  return <section className="panel settings-panel"><h3><Radio size={18} />平台与 MediaCrawler</h3>
    <p>{crawler.state === 'unavailable' ? '采集环境未就绪。重启工作台会自动检查并安装，失败原因见启动日志。'
      : crawler.state === 'fixture' ? '隔离验收采集环境' : '采集环境已配置'} · {crawler.revision.slice(0, 7)} · Python {crawler.python}</p>
    {(Object.keys(platforms) as PlatformId[]).map((id) => {
      const enabled = data.enabled_platforms.includes(id);
      const session = data.sessions[id];
      return <div className={'session-row' + (params.get('platform') === id ? ' selected-platform' : '')} key={id}>
        <label><input type="checkbox" checked={enabled} disabled={enable.isPending} aria-label={`启用${platforms[id]}`}
          onChange={(e) => enable.mutate(e.target.checked
            ? [...data.enabled_platforms, id] : data.enabled_platforms.filter((name) => name !== id))} /><Platform id={id} /></label>
        <span title={session.checked_at ? displayTime(session.checked_at) : undefined}>{enabled
          ? session.job_id ? '请在本机窗口登录' : sessionLabels[session.state] : '已停用'}</span>
        {session.job_id ? <Link className="button" to={'/jobs?job_id=' + session.job_id}>查看登录任务</Link>
          : <button className="button" disabled={!enabled || crawler.state === 'unavailable' || login.isPending}
            onClick={() => login.mutate(id)}>{session.state === 'ready' ? '重新登录' : '登录平台'}</button>}
      </div>;
    })}
    {(login.error || enable.error) && <p className="form-error" role="alert">{(login.error || enable.error)?.message}</p>}
  </section>;
}

