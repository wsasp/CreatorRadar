import { useMutation, useQueryClient } from '@tanstack/react-query';
import { FileText, ListTodo, Play, Terminal, Trash2 } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  displayDuration, displayTime, levels, phases, platforms, refreshWorkspace, statuses, types, useApi, write,
  type Job, type JobDetail, type LogEvent, type Page,
} from './api';
import { Dialog, Empty, FilterSelect, LoadState, PageTitle, Platform, Status, Values } from './ui';
import { Collection } from './collection';

const retryable = ['failed', 'interrupted', 'waiting_login', 'partially_succeeded'];
const active = ['queued', 'running'];

function useFilters(names: readonly string[], pagination: string) {
  const [params, update] = useSearchParams();
  const api = new URLSearchParams();
  for (const name of [...names, pagination]) {
    const value = params.get(name);
    if (!value) continue;
    api.set(name, value);
  }
  function change(name: string, value: string, resetFilters = false) {
    const next = new URLSearchParams(resetFilters ? '' : params);
    if (value) next.set(name, value);
    else next.delete(name);
    if (names.includes(name)) next.delete(pagination);
    update(next);
  }
  return { params, change, query: api.toString() };
}

function Select({ label, all, value, options, change }: {
  label: string; all: string; value: string; options: Record<string, string>; change: (value: string) => void;
}) {
  return <FilterSelect label={label} value={value} onChange={change}>
      <option value="">{all}</option>
      {Object.entries(options).map(([id, name]) => <option value={id} key={id}>{name}</option>)}
  </FilterSelect>;
}

function DateFields({ params, change }: {
  params: URLSearchParams; change: (name: string, value: string) => void;
}) {
  return <div className="extra-filters-content">{(['since', 'until'] as const).map((key) => {
    const date = new Date(params.get(key) || '');
    const value = Number.isNaN(date.valueOf()) ? '' : new Date(
      date.valueOf() - date.getTimezoneOffset() * 60_000,
    ).toISOString().slice(0, 16);
    return <label className="filter-field" key={key}>
      <span>{key === 'since' ? '开始时间' : '结束时间'}</span>
      <input type="datetime-local" value={value} onChange={(event) => {
        const local = new Date(event.target.value);
        change(key, Number.isNaN(local.valueOf()) ? '' : local.toISOString());
      }} />
    </label>;
  })}</div>;
}

function Pager({ current, next, change }: {
  current: boolean; next: string | number | null; change: (value: string) => void;
}) {
  if (!current && next === null) return null;
  return <div className="pagination">
    {current && <button className="button" onClick={() => change('')}>返回第一页</button>}
    {next !== null && <button className="button" onClick={() => change(String(next))}>下一页</button>}
  </div>;
}

export function JobsPage() {
  const { params, change, query } = useFilters(['kind', 'status', 'platform', 'since', 'until'], 'cursor');
  const list = useApi<Page<Job>>('jobs', query);
  const selected = params.get('job_id');
  return <>
    <Collection />
    <div className="section-title"><h2>任务队列</h2><Link className="text-button" to="/logs">打开运行日志</Link></div>
    <div className="filter-bar simple">
      <Select label="任务类型" all="全部类型" options={types} value={params.get('kind') || ''} change={(value) => change('kind', value)} />
      <Select label="任务状态" all="全部状态" options={statuses} value={params.get('status') || ''} change={(value) => change('status', value)} />
      <Select label="任务平台" all="全部平台" options={platforms} value={params.get('platform') || ''} change={(value) => change('platform', value)} />
      <details className="date-filters" open={params.has('since') || params.has('until') || undefined}>
        <summary>时间范围</summary><DateFields params={params} change={change} />
      </details>
    </div>
    <LoadState pending={list.isPending} error={list.error} />
    {list.data && <>
      <div className="panel table-panel">
        <table><thead><tr><th>任务 / 对象</th><th>平台</th><th>状态</th>
          <th>提交时间</th><th>结果 / 操作</th></tr></thead>
          <tbody>{list.data.items.map((job) => <tr key={job.id}>
            <td><strong>{types[job.kind]} · {job.title}</strong><small>{job.id}</small></td>
            <td>{job.platform ? <Platform id={job.platform} /> : job.is_batch ? '多平台' : '—'}</td>
            <td><Status value={job.status} />{job.cancel_requested && <small>正在取消</small>}</td>
            <td>{displayTime(job.created_at)}</td>
            <td><small>{job.message}</small><div className="row-actions">
              <button className="text-button" onClick={() => change('job_id', job.id)}>详情</button>
              <Link className="text-button" to={`/logs?job_id=${job.id}`}>查看日志</Link>
            </div></td>
          </tr>)}</tbody>
        </table>{!list.data.items.length && <Empty title="没有匹配的任务" icon={ListTodo} />}
      </div>
      <Pager current={params.has('cursor')} next={list.data.next_cursor} change={(value) => change('cursor', value)} />
    </>}
    {selected && <JobDrawer key={selected} id={selected} close={() => change('job_id', '')}
      open={(id) => change('job_id', id)} />}
  </>;
}

type Command = { action: 'retry' | 'cancel'; child?: string };

function useJobCommand(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ action, child }: Command) => write(`jobs/${id}/${action}`, 'POST',
      child ? { child_ids: [child] } : undefined),
    onSuccess: () => refreshWorkspace(client, ['overview', 'jobs', 'events']),
  });
}

function JobActions({ job, command }: { job: JobDetail; command: ReturnType<typeof useJobCommand> }) {
  const canRetry = job.is_batch ? job.children.some((child) => retryable.includes(child.status))
    : retryable.includes(job.status) && job.error_code !== 'ai_settings_changed';
  return <>
    {command.error && <p className="form-error" role="alert">{command.error.message}</p>}
    {command.isSuccess && <p className="callout" role="status">操作已提交</p>}
    {(active.includes(job.status) || canRetry) && <div className="inline-actions">
      {active.includes(job.status) && <button className="button danger" disabled={command.isPending || job.cancel_requested}
        onClick={() => command.mutate({ action: 'cancel' })}>取消任务</button>}
      {canRetry && <button className="button primary" disabled={command.isPending || job.cancel_requested}
        onClick={() => command.mutate({ action: 'retry' })}>{job.is_batch ? '重试可恢复项' : '重试任务'}</button>}
    </div>}
  </>;
}

function JobDrawer({ id, close, open }: { id: string; close: () => void; open: (id: string) => void }) {
  const job = useApi<JobDetail>('jobs/' + encodeURIComponent(id));
  const command = useJobCommand(id);
  const item = job.data;
  return <Dialog title="任务详情" description={item ? types[item.kind] + ' · ' + item.title : undefined} close={close} drawer>
    <LoadState pending={job.isPending} error={job.error} />
    {item && <>
      <div className="job-summary"><Status value={item.status} />
        <span>{item.cancel_requested ? '正在取消' : item.message}</span>
        <Link className="text-button" to={`/logs?job_id=${id}`}>查看日志</Link>
      </div>
      <Values rows={[
        ['任务 ID', id], ...(phases[item.phase] ? [['阶段', phases[item.phase]] as const] : []),
        ['平台', item.platform ? platforms[item.platform] : item.is_batch ? '多平台' : '—'],
        ['提交时间', displayTime(item.created_at)], ['开始时间', displayTime(item.started_at)],
        ['结束时间', displayTime(item.finished_at)], ['耗时', displayDuration(item.duration_ms)],
        ['执行尝试', item.attempt_count],
        ...(item.context.trigger ? [['触发来源', item.context.trigger === 'scheduled' ? '定时扫描' : '手动提交'] as const] : []),
        ...(item.error_code ? [['错误码', item.error_code] as const] : []),
      ]} />
      <div className="inline-actions">
        {!item.context.topic_id && typeof item.result?.topic_id === 'string' && <Link className="button primary"
          to={'/topics?topic_id=' + item.result.topic_id}>查看选题</Link>}
        {item.context.topic_id && <Link className="button" to={`/topics?topic_id=${item.context.topic_id}${
          item.kind === 'asr' || item.kind === 'analysis' ? '&tab=' + item.kind : ''
        }`}>查看选题</Link>}
        {item.context.search_id && <Link className="button" to={'/search?search_id=' + item.context.search_id}>查看搜索结果</Link>}
        {typeof (item.context.account_id || item.result?.account_id) === 'string' && <Link className="button"
          to={'/accounts?account_id=' + (item.context.account_id || item.result?.account_id)}>查看账号</Link>}
        {(item.status === 'waiting_login' || item.kind === 'platform_login' || item.error_code === 'challenge') && item.platform &&
          <Link className="button" to={'/settings?platform=' + item.platform}>平台设置</Link>}
      </div>
      {item.parent_id && <button className="text-button" onClick={() => open(item.parent_id!)}>查看父任务</button>}
      {item.result && !item.is_batch && <section className="task-section"><h3>结果</h3>
        {typeof item.result.count === 'number' ? <p>获取 {item.result.count} 条作品
          {item.kind === 'account_scan' && ` · 新增 ${item.result.added} 条 · 更新 ${item.result.updated} 条`}</p>
          : typeof item.result.segment_count === 'number' ? <p>已生成 {item.result.segment_count} 段逐字稿</p>
          : item.result.current === false ? <p>输入已更新，拆解结果已保存为历史版本。</p>
          : item.kind === 'media_fetch' || item.kind === 'self_test' ?
            <pre className="result">{JSON.stringify(item.result, null, 2)}</pre> : <p>{item.message}</p>}</section>}
      {item.children.length > 0 && <section className="task-section"><h3>子任务</h3>
        <ul className="task-items">{item.children.map((child) => <li key={child.id}>
          <div><button className="text-button" onClick={() => open(child.id)}>
            {child.kind === 'account_scan' ? child.title : child.platform ? platforms[child.platform] : child.title}
          </button><p>{child.message}</p></div>
          <Status value={child.status} />
          {retryable.includes(child.status) && <button className="button" disabled={command.isPending || item.cancel_requested}
            onClick={() => command.mutate({ action: 'retry', child: child.id })}>重试此项</button>}
        </li>)}</ul>
      </section>}
      {item.attempts.length > 0 && <section className="task-section"><h3>执行记录</h3>
        <ul className="task-items">{item.attempts.map((attempt) => <li key={attempt.number}>
          <div><strong>第 {attempt.number} 次</strong><p>{displayTime(attempt.started_at)} · {displayDuration(attempt.duration_ms)}</p>
            {attempt.message && <p>{attempt.message}</p>}</div><Status value={attempt.status} />
        </li>)}</ul>
      </section>}
      <JobActions job={item} command={command} />
    </>}
  </Dialog>;
}

function LogJob({ id }: { id: string }) {
  const job = useApi<JobDetail>('jobs/' + encodeURIComponent(id));
  const command = useJobCommand(id);
  const item = job.data;
  if (!item) return <LoadState pending={job.isPending} error={job.error} />;
  const finished = item.children.filter((child) => !active.includes(child.status)).length;
  return <section className="panel log-job">
    <div className="section-title"><h2>{types[item.kind]} · {item.title}</h2>
      <Link className="text-button" to={'/jobs?job_id=' + id}>任务详情</Link>
    </div>
    <div className="job-summary"><Status value={item.status} />
      <span>{item.cancel_requested ? '正在取消' : item.message}</span>
      {phases[item.phase] && <span>{phases[item.phase]}</span>}
    </div>
    {item.children.length > 0 && <div className="log-progress overview-progress">
      <div className="overview-progress-row">
        <div><span>子任务进度</span><strong>{finished} / {item.children.length}</strong></div>
        <progress aria-label="子任务进度" value={finished} max={item.children.length} />
      </div>
    </div>}
    <JobActions job={item} command={command} />
  </section>;
}

type ClearScope = 'completed' | 'all';

export function LogsPage() {
  const { params, change, query } = useFilters(['level', 'platform', 'job_id', 'since', 'until'], 'before');
  const client = useQueryClient();
  const events = useApi<Page<LogEvent, number>>('events', query, true, 10_000);
  const cursor = params.get('task_cursor');
  const jobQuery = new URLSearchParams({ limit: '15', ...(cursor ? { cursor } : {}) });
  const jobs = useApi<Page<Job>>('jobs', jobQuery.toString(), true, 10_000);
  const [copyStatus, setCopyStatus] = useState('');
  const [clearScope, setClearScope] = useState<ClearScope | null>(null);
  const items = events.data?.items || [];
  const jobId = params.get('job_id') || '';
  const selfTest = useMutation({
    mutationFn: () => write<{ job_id: string }>('system/self-test', 'POST'),
    onSuccess: ({ job_id }) => {
      change('job_id', job_id, true);
      return refreshWorkspace(client, ['overview', 'jobs', 'events']);
    },
  });
  const clear = useMutation({
    mutationFn: (scope: ClearScope) => write<{ deleted_events: number }>('events/clear', 'POST', { scope }),
    onSuccess: () => {
      setClearScope(null);
      setCopyStatus('');
      if (params.has('before')) change('before', '');
      return client.invalidateQueries({ queryKey: ['events'] });
    },
  });

  async function copy() {
    try {
      await navigator.clipboard.writeText(items.map((event) => [
        displayTime(event.created_at), levels[event.level], types[event.kind],
        event.job_id, event.code, event.message,
      ].join(' · ')).join('\n'));
      setCopyStatus('已复制当前日志。');
    } catch {
      setCopyStatus('复制失败，请选择日志文字后手动复制。');
    }
  }

  let content: ReactNode = null;
  if (events.data) content = <><table><thead><tr><th>时间</th><th>级别</th><th>任务 / 模块</th><th>日志内容</th></tr></thead>
    <tbody>{items.map((event) => <tr key={event.id}>
      <td>{displayTime(event.created_at)}</td>
      <td><span className={`log-level event-${event.level}`}>{levels[event.level]}</span></td>
      <td><strong>{types[event.kind]}</strong>
        <Link className="text-button event-job-link" to={`/jobs?job_id=${event.job_id}`}>{event.job_id}</Link>
        {event.platform && <Platform id={event.platform} />}</td>
      <td className="log-message">{event.message}<small>{event.code}{event.attempt ? ` · 第 ${event.attempt} 次执行` : ''}</small></td>
    </tr>)}</tbody></table>{!items.length && <Empty title="没有匹配的日志" icon={Terminal} />}</>;

  return <>
    <PageTitle title="运行日志" description="按任务查看实时事件、执行状态和历史日志。" action={
      <div className="log-title-actions">
        <button className="button" onClick={() => { clear.reset(); setClearScope('completed'); }}>
          <Trash2 size={15} />清空已完成任务日志</button>
        <button className="button" onClick={() => { clear.reset(); setClearScope('all'); }}>清空全部任务日志</button>
        <button className="button primary" disabled={selfTest.isPending} onClick={() => selfTest.mutate()}>
          <Play size={15} />运行队列自检</button>
      </div>} />
    {selfTest.error && <p className="callout form-error" role="alert">{selfTest.error.message}</p>}
    {clear.isSuccess && <p className="callout" role="status">已清空 {clear.data.deleted_events} 条日志。</p>}
    <div className="log-layout">
      <aside className="panel log-task-list" aria-label="日志任务列表">
        <div className="section-title"><h2>任务队列</h2></div>
        <button className="log-task" aria-pressed={!jobId} onClick={() => change('job_id', '')}>全部日志</button>
        <LoadState pending={jobs.isPending} error={jobs.error} />
        {jobs.data?.items.map((job) => <button key={job.id} className="log-task" aria-pressed={jobId === job.id}
          onClick={() => change('job_id', job.id)}>
          <span><strong>{types[job.kind]} · {job.title}</strong><small>{displayTime(job.created_at)}</small></span>
          <Status value={job.status} />
        </button>)}
        {jobs.data && <Pager current={!!cursor} next={jobs.data.next_cursor} change={(value) => change('task_cursor', value)} />}
      </aside>
      <div className="log-main">
        {jobId && <LogJob key={jobId} id={jobId} />}
        <div className="filter-bar simple">
          <Select label="日志级别" all="全部级别" options={levels} value={params.get('level') || ''}
            change={(value) => change('level', value)} />
          <Select label="日志平台" all="全部平台" options={platforms} value={params.get('platform') || ''}
            change={(value) => change('platform', value)} />
          <details className="date-filters" open={params.has('since') || params.has('until') || undefined}>
            <summary>时间范围</summary><DateFields params={params} change={change} />
          </details>
          <button className="button" disabled={!items.length} onClick={copy}>
            <FileText size={16} />复制当前日志
          </button>
        </div>
        {copyStatus && <p className="callout" role="status">{copyStatus}</p>}
        <LoadState pending={events.isPending} error={events.error} />
        {content && <div className="panel table-panel">{content}</div>}
        {events.data && <Pager current={params.has('before')} next={events.data.next_cursor}
          change={(value) => change('before', value)} />}
      </div>
    </div>
    {clearScope && <Dialog title={clearScope === 'completed' ? '清空已完成任务日志？' : '清空全部任务日志？'}
      description={clearScope === 'completed' ? '将删除已结束任务的事件日志。' : '将删除历史和排队任务的事件日志，保留运行中任务及其批次的日志。'}
      close={() => { if (!clear.isPending) setClearScope(null); }}>
      <p>任务记录和采集结果会保留，删除的日志无法恢复。</p>
      {clear.error && <p className="form-error" role="alert">{clear.error.message}</p>}
      <div className="modal-actions">
        <button className="button" disabled={clear.isPending} onClick={() => setClearScope(null)}>返回</button>
        <button className="button danger" disabled={clear.isPending} onClick={() => clear.mutate(clearScope)}>确认清空</button>
      </div>
    </Dialog>}
  </>;
}
