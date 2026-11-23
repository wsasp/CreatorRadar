import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  AudioLines, BookOpen, Bookmark, ChevronLeft, ChevronRight, Clock3, ExternalLink, Eye, FileText, Filter, Heart, ImageOff,
  MessageCircle, Play, Plus, Radar, RefreshCw, Search as SearchIcon, Share2, Sparkles, Trash2, Users,
} from 'lucide-react';
import { useState, type FormEvent, type ReactNode } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import {
  displayTime, platforms, refreshWorkspace, useApi, write,
  type Account, type Measure, type Page, type Platform as PlatformId,
  type Search, type SearchResults, type Settings, type Source, type Topic, type TopicDetail, type Work,
} from './api';
import { TopicProcessing } from './topic-processing';
import { Dialog, Empty, FilterSelect, LoadState, PageTitle, Platform, SearchField, Status } from './ui';

const platformIds = Object.keys(platforms) as PlatformId[];
const countFormat = new Intl.NumberFormat('zh-CN');
const metricFields = [
  ['plays', Eye, '播放'], ['likes', Heart, '点赞'], ['comments', MessageCircle, '评论'],
  ['favorites', Bookmark, '收藏'], ['shares', Share2, '转发'],
] as const;
const unknown = { missing: '平台未返回', not_applicable: '图文作品不适用', unparsed: '平台数据无法解析' };
const sourceNames = { scan: '情报流', search: '关键词搜索', quick: '快速采集' };
const feedPeriods = ['today', '3d', '7d', 'all', '24h'];

function publicationStart(period: string) {
  const minute = Math.floor(Date.now() / 60_000) * 60_000;
  if (period === 'today') return new Date(minute).setHours(0, 0, 0, 0);
  if (period === '3d' || period === '7d') return minute - Number(period[0]) * 86_400_000;
  return undefined;
}

function useWorkspaceMutation<T, V>(run: (value: V) => Promise<T>, success?: (result: T) => void) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: run,
    onSuccess: (result) => {
      void refreshWorkspace(client);
      success?.(result);
    },
  });
}

function useFilters() {
  const [params, setParams] = useSearchParams();
  function change(values: Record<string, string>, clearCursor = false) {
    setParams((current) => {
      const next = new URLSearchParams(current);
      if (clearCursor) next.delete('cursor');
      for (const [key, value] of Object.entries(values)) {
        if (value) next.set(key, value); else next.delete(key);
      }
      return next;
    }, { replace: true, preventScrollReset: true });
  }
  function apiParams(...keys: string[]) {
    return new URLSearchParams(keys.flatMap((key) =>
      params.has(key) ? [[key, params.get(key)!]] : []));
  }
  return { params, change, apiParams };
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="field"><span>{label}</span>{children}</label>;
}

function PlatformTabs({ value, change }: { value: string; change: (value: string) => void }) {
  return <div className="segmented" role="group" aria-label="平台筛选">
    <button type="button" aria-pressed={!value} className={!value ? 'active' : ''} onClick={() => change('')}>全部平台</button>
    {platformIds.map((id) => <button key={id} type="button" aria-pressed={id === value}
      className={id === value ? 'active' : ''} onClick={() => change(id)}><Platform id={id} /></button>)}
  </div>;
}

function Pager({ cursor, next, change }: { cursor: string; next: string | null; change: (cursor: string) => void }) {
  const [trail, setTrail] = useState<string[]>([]);
  if (!cursor && !next) return null;
  return <div className="modal-actions">
    {cursor && <button className="button" onClick={() => {
      change(trail.at(-1) || '');
      setTrail((items) => items.slice(0, -1));
    }}>上一页</button>}
    {next && <button className="button" onClick={() => {
      setTrail((items) => cursor ? [...items, cursor] : ['']);
      change(next);
    }}>下一页</button>}
  </div>;
}

function Metrics({ work, detail = false, compact = false }: { work: Work; detail?: boolean; compact?: boolean }) {
  return <div className={'metrics' + (detail ? ' metrics-detail' : compact ? ' metrics-compact' : '')}>{metricFields.map(([key, Icon, label]) => {
    const metric = work.metrics[key];
    const value = metric?.count;
    const description = value == null
      ? unknown[metric?.availability as keyof typeof unknown] || unknown.missing
      : countFormat.format(value) + ' · 采集于 ' + displayTime(metric.collected_at);
    return <span key={key} title={description} aria-label={label + ' ' + description}>
      <Icon size={15} aria-hidden="true" />
      <b>{value == null ? '—' : !detail && value >= 10000 ? (value / 10000).toFixed(1) + '万' : countFormat.format(value)}</b>
      <small>{label}</small>
    </span>;
  })}</div>;
}

function Cover({ work }: { work: Work }) {
  const [broken, setBroken] = useState(false);
  return <>
    {work.cover_url && !broken ? <img src={work.cover_url} onError={() => setBroken(true)} alt="" loading="lazy" referrerPolicy="no-referrer" />
      : <ImageOff aria-hidden="true" className="cover-placeholder" size={26} />}
    <Platform id={work.platform} />
    {work.kind === 'article' && <span className="duration"><FileText size={12} />图文</span>}
    {work.kind === 'video' && <span className="duration"><Play size={12} />
      {work.duration_seconds ? `${Math.floor(work.duration_seconds / 60)}:${String(work.duration_seconds % 60).padStart(2, '0')}` : '视频'}
    </span>}
  </>;
}

function ArticleGallery({ images }: { images: string[] }) {
  const [position, setPosition] = useState(0);
  const [brokenUrl, setBrokenUrl] = useState<string | null>(null);
  const index = Math.min(position, images.length - 1);
  const url = images[index];
  return <section className="article-gallery" aria-label="作品图片">
    <div className="article-gallery-frame">
      {brokenUrl === url ? <span className="article-gallery-error"><ImageOff size={24} />图片加载失败</span>
        : <a href={url} target="_blank" rel="noreferrer" aria-label={`查看第 ${index + 1} 张大图`}>
          <img key={url} src={url} alt={`第 ${index + 1} 张作品图片`} referrerPolicy="no-referrer"
            onError={() => setBrokenUrl(url)} />
        </a>}
    </div>
    {images.length > 1 && <div className="article-gallery-controls">
      <button type="button" className="icon-button" aria-label="上一张" disabled={index === 0}
        onClick={() => setPosition(index - 1)}><ChevronLeft size={18} /></button>
      <span aria-live="polite">{index + 1} / {images.length}</span>
      <button type="button" className="icon-button" aria-label="下一张" disabled={index === images.length - 1}
        onClick={() => setPosition(index + 1)}><ChevronRight size={18} /></button>
    </div>}
  </section>;
}

function Original({ work, detail = false }: { work: Work; detail?: boolean }) {
  const label = detail ? '打开原帖' : '原链接';
  const className = detail ? 'button' : 'text-button subdued';
  return work.original_url
    ? <a className={className} href={work.original_url} target="_blank" rel="noreferrer"><ExternalLink size={14} />{label}</a>
    : <button type="button" className={className} disabled title="该作品暂无原链接"><ExternalLink size={14} />{label}</button>;
}

function WorkCard({ work, open, label, transcribed, compactMetrics }: {
  work: Work; open: () => void; label?: string; transcribed?: boolean; compactMetrics?: boolean;
}) {
  return <article className="work-card">
    <button className="cover" type="button" onClick={open} aria-label={'查看作品 ' + work.title}><Cover key={work.cover_url} work={work} /></button>
    <div className="work-content">
      <div className="work-meta"><span>{work.author || '未知作者'}</span><i />
        <time>{displayTime(work.published_at)}</time>
        {(label || transcribed) && <span className="work-tags">
          {label && <span className="tag">{label}</span>}
          {transcribed && <span className="tag transcribed">已转写</span>}
        </span>}
      </div>
      <button className="work-title" onClick={open}>{work.title}</button>
      {work.body && <p>{work.body}</p>}
      <Metrics work={work} compact={compactMetrics} />
    </div>
    <div className="work-foot"><Original work={work} /><button className="text-button" onClick={open}>详情<ChevronRight size={14} /></button></div>
  </article>;
}

function WorkDrawer({ workId, topicId, close, sourceKind, sourceId }: {
  workId?: string; topicId?: string; close: () => void; sourceKind?: 'scan' | 'search'; sourceId?: string;
}) {
  const { params, change } = useFilters();
  const requestedTab = topicId && params.get('tab');
  const tab = requestedTab === 'asr' || requestedTab === 'analysis' ? requestedTab : 'overview';
  const navigate = useNavigate();
  const workQuery = useApi<Work>('works/' + workId, '', Boolean(workId));
  const topicQuery = useApi<TopicDetail>('topics/' + topicId, '', Boolean(topicId));
  const topic = topicQuery.data;
  const work = topic?.work || workQuery.data;
  const collectedTopicId = topic?.id ?? work?.topic_id;
  const query = topicId ? topicQuery : workQuery;
  const source = topic?.source || work?.sources?.find((item) => item.kind === sourceKind &&
    (!sourceId || item.source_id === sourceId));
  const adopt = useWorkspaceMutation(
    (selected: Source) => write<{ topic_id: string }>('topics', 'POST',
      { work_id: work?.id, kind: selected.kind, source_id: selected.source_id }),
    ({ topic_id }) => {
      if (sourceKind === 'search') navigate('/topics?topic_id=' + topic_id);
    },
  );
  const archive = useWorkspaceMutation((archived: boolean) => write<Topic>('topics/' + topicId, 'PATCH', { archived }));
  const withdraw = useWorkspaceMutation((id: string) => write<{ id: string }>('topics/' + id, 'DELETE'), () => {
    if (topicId) close();
  });
  const actionError = archive.error || adopt.error || withdraw.error;
  return <Dialog drawer fullWidthBody title={topicId ? '选题详情' : '作品详情'} description={source && sourceNames[source.kind]} close={close}>
    <LoadState pending={query.isPending} error={query.error} />
    {work && <>
      <div className="detail-top">
        {work.image_urls?.length ? <ArticleGallery key={work.id} images={work.image_urls} />
          : <div className="detail-cover"><Cover key={work.cover_url} work={work} /></div>}
        <h3>{work.title}</h3>
        <div className="work-meta"><span>{work.author || '未知作者'}</span><i /><time>{displayTime(work.published_at)}</time></div>
        <Metrics work={work} detail />
        {work.kind === 'article' && work.reads && <ReadCount metric={work.reads} />}
        <div className="inline-actions">
          <Original work={work} detail />
          {topic ? <button className="button" disabled={archive.isPending || withdraw.isPending}
            onClick={() => archive.mutate(!topic.archived)}>{topic.archived ? '恢复选题' : '归档选题'}</button>
            : work.topic_id ? <Link className="button primary" to={'/topics?topic_id=' + work.topic_id}>查看选题</Link>
            : source && <button className="button primary" disabled={adopt.isPending} onClick={() => adopt.mutate(source)}>
              <BookOpen size={15} />加入选题库</button>}
          {collectedTopicId && <button type="button" className="button" disabled={withdraw.isPending || archive.isPending}
            onClick={() => withdraw.mutate(collectedTopicId)}>取消收录</button>}
        </div>
      </div>
      {topic && <div className="detail-tabs" aria-label="选题内容">
        {([['overview', '作品概览', Radar], ['asr', '逐字稿', AudioLines],
          ['analysis', 'AI 拆解', Sparkles]] as const).map(([id, label, Icon]) =>
          <button key={id} type="button" aria-pressed={tab === id} className={tab === id ? 'active' : ''}
            onClick={() => change({ tab: id === 'overview' ? '' : id })}><Icon size={16} />{label}</button>)}
      </div>}
      <div className="detail-body">
        {topic && tab !== 'overview' ? <TopicProcessing key={topic.id + tab} topic={topic} kind={tab} /> : <>
        <h4>{work.kind === 'article' ? '平台正文' : '作品简介'}</h4>
        <p className="work-body">{work.body || '平台暂未返回内容。'}</p>
        {source && <p className="field-note">来源 · {sourceNames[source.kind]}
          {source.label !== sourceNames[source.kind] ? ' · ' + source.label : ''}
          {' · '}<Link to={'/jobs?job_id=' + source.job_id}>查看来源任务</Link></p>}
        {!topic && Boolean(work.sources?.length) && <ul className="source-list">{work.sources?.map((item) =>
          <li key={item.kind + item.source_id}>{sourceNames[item.kind]} · {item.label} · {displayTime(item.discovered_at)}</li>)}</ul>}
        <p className="field-note">最近采集 · {displayTime(work.collected_at)}</p>
        </>}
        {actionError && <p className="form-error" role="alert">{actionError.message}</p>}
      </div>
    </>}
  </Dialog>;
}

function ReadCount({ metric }: { metric: Measure }) {
  return <p className="field-note" title={metric.field || unknown.missing}>阅读量 · {
    metric.count == null ? '—' : countFormat.format(metric.count)
  }{metric.collected_at && ' · ' + displayTime(metric.collected_at)}</p>;
}

export function FeedPage() {
  const { params, change, apiParams } = useFilters();
  const navigate = useNavigate();
  const settings = useApi<Settings>('settings');
  const accounts = useApi<Page<Account>>('accounts', 'limit=100');
  const accountId = params.get('account_id') || '';
  const adopted = params.get('adopted') || '';
  const hasDates = params.has('since') || params.has('until');
  const requestedPeriod = params.get('recent') || '7d';
  const period = hasDates ? 'custom' : feedPeriods.includes(requestedPeriod) ? requestedPeriod : '7d';
  const hasAdvancedFilters = Boolean(accountId || hasDates);
  const [showFilters, setShowFilters] = useState(hasAdvancedFilters);
  const selectedAccount = useApi<Account>('accounts/' + accountId, '', Boolean(accountId));
  const filters = apiParams('platform', 'q', 'account_id', 'adopted', 'cursor');
  if (period === '24h') filters.set('recent', period);
  const start = publicationStart(period);
  if (start !== undefined) filters.set('since', String(start));
  for (const key of ['since', 'until']) {
    const date = params.get(key);
    if (date && /^\d{4}-\d{2}-\d{2}$/.test(date)) {
      const value = new Date(date + (key === 'until' ? 'T23:59:59.999' : 'T00:00:00')).getTime();
      if (Number.isFinite(value)) filters.set(key, String(value));
    }
  }
  const query = useApi<Page<Work> & { total: number }>(
    'feed', filters.toString(), true, filters.has('recent') || start !== undefined ? 60_000 : undefined,
  );
  const scan = useWorkspaceMutation(() => write<{ job_id: string; job_ids: string[] }>('account-scans', 'POST', {}),
    ({ job_id, job_ids }) => navigate(job_ids.length > 1 ? '/jobs?kind=account_scan' : '/jobs?job_id=' + job_id));
  const workId = params.get('work_id') || undefined;
  const accountItems = accounts.data?.items || [];
  function changePeriod(value: string) {
    if (value !== 'custom') change({ recent: value, since: '', until: '' }, true);
  }
  return <>
    <PageTitle title="情报流" description={query.data ? `${query.data.total} 条作品` : '浏览对标账号扫描发现的作品。'}
      action={<button className="button" disabled={scan.isPending || !settings.data?.enabled_platforms.length ||
        !accounts.data || !accounts.data.next_cursor && !accountItems.some((account) =>
          account.tracking === 'active' && settings.data?.enabled_platforms.includes(account.platform))}
        onClick={() => scan.mutate()}><RefreshCw size={15} />扫描对标账号</button>} />
    <div className="filter-bar simple">
      <PlatformTabs value={params.get('platform') || ''} change={(platform) => change({ platform }, true)} />
      <div className="segmented" role="group" aria-label="选题库收录状态">
        {([['', '全部作品'], ['true', '已收录'], ['false', '未收录']] as const).map(([value, label]) =>
          <button key={value} type="button" aria-pressed={adopted === value} className={adopted === value ? 'active' : ''}
            onClick={() => change({ adopted: value }, true)}>{label}</button>)}
      </div>
      <div className="filter-right">
        <SearchField label="检索作品" placeholder="搜索标题、作者…" value={params.get('q') || ''} onChange={(q) => change({ q }, true)} />
        <FilterSelect label="发布时间" value={period} onChange={changePeriod}>
          <option value="today">今天</option><option value="3d">最近 3 天</option><option value="7d">最近 7 天</option>
          <option value="all">全部时间</option>
          {period === '24h' && <option value="24h">近 24 小时</option>}
          {hasDates && <option value="custom" disabled>自定义日期</option>}
        </FilterSelect>
        {period === '24h' && <button className="text-button" type="button" onClick={() => changePeriod('')}>清除近 24 小时筛选</button>}
        <button className={'button filter-button' + (showFilters || hasAdvancedFilters ? ' selected' : '')}
          type="button" aria-expanded={showFilters} onClick={() => setShowFilters(!showFilters)}><Filter size={15} />筛选</button>
      </div>
    </div>
    {showFilters && <div className="extra-filters">
      <FilterSelect label="对标账号" value={accountId} onChange={(account_id) => change({ account_id }, true)}>
        <option value="">全部账号</option>
        {accountItems.map((account) => <option value={account.id} key={account.id}>{account.nickname}</option>)}
        {selectedAccount.data && !accountItems.some((account) => account.id === accountId) &&
          <option value={accountId}>{selectedAccount.data.nickname}</option>}
      </FilterSelect>
      <button className="text-button" type="button" onClick={() => change({ account_id: '', since: '', until: '' }, true)}>清除高级筛选</button>
      <details className="date-filters" open={hasDates || undefined}><summary>自定义日期</summary>
        <div className="extra-filters-content">
          <Field label="发布起始日期"><input type="date" value={params.get('since') || ''} onChange={(e) => change({ since: e.target.value }, true)} /></Field>
          <Field label="发布结束日期"><input type="date" value={params.get('until') || ''} onChange={(e) => change({ until: e.target.value }, true)} /></Field>
        </div>
      </details>
    </div>}
    {scan.error && <p className="form-error" role="alert">{scan.error.message}</p>}
    <LoadState pending={query.isPending} error={query.error} />
    {query.data && (query.data.items.length ? <div className="work-grid">{query.data.items.map((work) =>
      <WorkCard key={work.id} work={work} label={work.topic_id ? '已加入选题库' : undefined}
        open={() => change({ work_id: work.id })} />)}</div>
      : <Empty title={params.has('platform') || params.has('q') || adopted || hasAdvancedFilters ? '没有符合条件的作品' : '暂无情报作品'}
        icon={SearchIcon} message="试试更换关键词或放宽筛选范围。" />)}
    {query.data && <Pager cursor={params.get('cursor') || ''} next={query.data.next_cursor} change={(cursor) => change({ cursor })} />}
    {workId && <WorkDrawer workId={workId} close={() => change({ work_id: '' })} sourceKind="scan" sourceId={accountId} />}
  </>;
}

export function TopicsPage() {
  const { params, change, apiParams } = useFilters();
  const query = useApi<Page<Topic>>('topics', apiParams('platform', 'q', 'archived', 'cursor').toString());
  const topicId = params.get('topic_id') || undefined;
  return <>
    <PageTitle title="选题库" description="研读已收录的作品，完成转写与 AI 拆解。" />
    <div className="filter-bar"><PlatformTabs value={params.get('platform') || ''} change={(platform) => change({ platform }, true)} />
      <div className="filter-right">
        <SearchField label="检索选题" placeholder="搜索标题、作者…" value={params.get('q') || ''} onChange={(q) => change({ q }, true)} />
        <FilterSelect label="选题状态" value={params.get('archived') || ''} onChange={(archived) => change({ archived }, true)}>
          <option value="">已收录</option><option value="true">已归档</option>
        </FilterSelect>
      </div>
    </div>
    <LoadState pending={query.isPending} error={query.error} />
    {query.data && (query.data.items.length ? <div className="work-grid">{query.data.items.map((topic) =>
      <WorkCard key={topic.id} work={topic.work} label={sourceNames[topic.source.kind]}
        transcribed={topic.transcribed} open={() => change({ topic_id: topic.id })} />)}</div>
      : <Empty title="没有匹配的选题" icon={BookOpen} message="可从情报流、关键词搜索或快速采集收录作品。" />)}
    {query.data && <Pager cursor={params.get('cursor') || ''} next={query.data.next_cursor} change={(cursor) => change({ cursor })} />}
    {topicId && <WorkDrawer topicId={topicId} close={() => change({ topic_id: '', tab: '' })} />}
  </>;
}

export function SearchPage() {
  const { params, change, apiParams } = useFilters();
  const client = useQueryClient();
  const settings = useApi<Settings>('settings');
  const [keyword, setKeyword] = useState('');
  const [selected, setSelected] = useState<PlatformId[] | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Search | 'all' | null>(null);
  const enabled = settings.data?.enabled_platforms || [];
  const chosen = (selected || enabled).filter((name) => enabled.includes(name));
  const history = useApi<Page<Search>>('searches', apiParams('history_cursor').toString().replace('history_cursor=', 'cursor='));
  const historyItems = history.data?.items || [];
  const historyCursor = params.get('history_cursor') || '';
  const searchId = params.get('search_id') || history.data?.items[0]?.id;
  const results = useApi<SearchResults>('searches/' + searchId + '/results',
    apiParams('platform', 'sort', 'cursor').toString(), Boolean(searchId));
  const submit = useWorkspaceMutation(
    () => write<{ search_id: string }>('searches', 'POST', { keyword: keyword.trim(), platforms: chosen }),
    ({ search_id }) => change({ search_id, platform: '', work_id: '' }, true),
  );
  const retry = useWorkspaceMutation((job: { parent: string; child: string }) =>
    write('jobs/' + job.parent + '/retry', 'POST', { child_ids: [job.child] }));
  const removeHistory = useMutation({
    mutationFn: (target: Search | 'all') => write('searches' + (target === 'all' ? '' : '/' + target.id), 'DELETE'),
    onSuccess: async (_, target) => {
      await client.cancelQueries({ queryKey: ['searches'] });
      client.setQueriesData<Page<Search>>({ queryKey: ['searches'] }, (page) => {
        if (target === 'all') return { items: [], next_cursor: null };
        return page && { ...page, items: page.items.filter((record) => record.id !== target.id) };
      });
      const resetHistoryPage = target === 'all' || (historyCursor && historyItems.length <= 1);
      const resetResults = target === 'all' || target.id === searchId;
      if (resetHistoryPage || resetResults) change({
        ...(resetHistoryPage && { history_cursor: '' }),
        ...(resetResults && { search_id: '', platform: '', work_id: '' }),
      }, resetResults);
      setDeleteTarget(null);
      await refreshWorkspace(client, ['searches', 'works']);
    },
  });
  const workId = params.get('work_id') || undefined;
  function start(event: FormEvent) {
    event.preventDefault();
    if (keyword.trim() && chosen.length) submit.mutate();
  }
  return <>
    <PageTitle title="关键词搜索" description="从多个平台发现新作品，把值得研究的内容加入选题库。" />
    <form className="panel search-panel" onSubmit={start}>
      <div className="search-submit"><SearchField label="关键词" placeholder="输入关键词，例如 AI、选题、工作流" value={keyword} onChange={setKeyword} />
        <button className="button primary" disabled={submit.isPending || !keyword.trim() || !chosen.length}><SearchIcon size={16} />开始搜索</button></div>
      <div className="check-list"><span>搜索平台</span>{platformIds.map((name) => <label key={name}>
        <input type="checkbox" disabled={!enabled.includes(name)} checked={chosen.includes(name)}
          onChange={(e) => setSelected(e.target.checked ? [...chosen, name] : chosen.filter((p) => p !== name))} />
        <Platform id={name} />{!enabled.includes(name) && <small>已停用</small>}
      </label>)}</div>
      <LoadState pending={settings.isPending} error={settings.error} />
      {settings.data && !chosen.length && <p className="field-note">{enabled.length
        ? '请选择至少一个已启用平台。' : '请在系统设置启用至少一个平台。'}</p>}
      {submit.error && <p className="form-error" role="alert">{submit.error.message}</p>}
    </form>
    <div className="search-layout">
      <section>
        <div className="section-title"><div><h2>{searchId ? '搜索结果' : '作品发现'}</h2>{results.data && <span>{results.data.search.keyword}</span>}</div>
          {searchId && <FilterSelect label="结果排序" value={params.get('sort') || 'platform'} onChange={(sort) => change({ sort }, true)}>
            <option value="platform">平台顺序</option><option value="published">最近发布</option>
          </FilterSelect>}
        </div>
        {searchId && <LoadState pending={results.isPending} error={results.error} />}
        {results.error && params.has('search_id') && <button type="button" className="text-button"
          onClick={() => change({ search_id: '', platform: '', work_id: '', history_cursor: '' }, true)}>返回搜索历史</button>}
        {results.data && <>
          <div className="search-status"><PlatformTabs value={params.get('platform') || ''} change={(platform) => change({ platform }, true)} />
            <Link to={'/jobs?job_id=' + results.data.search.job_id}><Status value={results.data.search.status} /></Link></div>
          <ul className="search-platform-status">{results.data.children.map((job) => <li key={job.id}>
            {job.platform && <Platform id={job.platform} />}<Status value={job.status} /><span>{job.message}</span>
            {job.status === 'waiting_login' && <Link className="text-button" to={'/settings?platform=' + job.platform}>平台登录</Link>}
            {['failed', 'interrupted', 'waiting_login', 'partially_succeeded'].includes(job.status) &&
              <button className="text-button" disabled={retry.isPending}
                onClick={() => retry.mutate({ parent: results.data!.search.job_id, child: job.id })}>重试此平台</button>}
          </li>)}</ul>
          {retry.error && <p className="form-error" role="alert">{retry.error.message}</p>}
          {results.data.items.length ? <div className="work-grid search-grid">{results.data.items.map((work) =>
            <WorkCard key={work.id} work={work} compactMetrics open={() => change({ work_id: work.id })} />)}</div>
            : <Empty title={['queued', 'running'].includes(results.data.search.status) ? '正在搜索' : '暂无匹配作品'} icon={SearchIcon} />}
          <Pager cursor={params.get('cursor') || ''} next={results.data.next_cursor} change={(cursor) => change({ cursor })} />
        </>}
        {!searchId && <Empty title="开始一次关键词搜索" icon={SearchIcon} message="可搜索「AI」「选题」或「工作流」。" />}
      </section>
      <aside className="panel history">
        <div className="history-header"><h3><Clock3 size={17} />搜索历史</h3>
          {(historyItems.length > 0 || historyCursor) && <button type="button" className="text-button subdued"
            disabled={removeHistory.isPending} onClick={() => { removeHistory.reset(); setDeleteTarget('all'); }}>清空历史</button>}
        </div>
        <LoadState pending={history.isPending} error={history.error} />
        {historyItems.map((item) => <div className="history-item" key={item.id}>
          <button type="button" className={'history-open' + (item.id === searchId ? ' active' : '')}
            onClick={() => change({ search_id: item.id, platform: '', work_id: '' }, true)}>
            <span className="history-item-details">
              <strong title={item.keyword}>{item.keyword}</strong>
              <time dateTime={item.created_at}>{displayTime(item.created_at)}</time>
              <small>{item.platforms.map((p) => platforms[p]).join('、')}</small>
            </span>
            <ChevronRight size={15} />
          </button>
          <button type="button" className="icon-button history-delete" aria-label={`删除搜索记录 ${item.keyword}`} title="删除搜索记录"
            disabled={removeHistory.isPending} onClick={() => { removeHistory.reset(); setDeleteTarget(item); }}><Trash2 size={15} /></button>
        </div>)}
        {history.data?.items.length === 0 && <p>搜索完成后可从这里回看结果。</p>}
        {history.data && <Pager cursor={historyCursor} next={history.data.next_cursor}
          change={(history_cursor) => change({ history_cursor })} />}
      </aside>
    </div>
    {deleteTarget && <Dialog title={deleteTarget === 'all' ? '清空搜索历史' : '删除搜索记录'} close={() => {
      if (!removeHistory.isPending) setDeleteTarget(null);
    }}>
      <p className="history-confirmation">{deleteTarget === 'all' ? '将删除全部搜索历史和对应的搜索结果。'
        : `将删除「${deleteTarget.keyword}」及其搜索结果。`}已收录的选题仍可在选题库查看。</p>
      {removeHistory.error && <p className="form-error" role="alert">{removeHistory.error.message}</p>}
      <div className="modal-actions">
        <button type="button" className="button" disabled={removeHistory.isPending} onClick={() => setDeleteTarget(null)}>取消</button>
        <button type="button" className="button danger" disabled={removeHistory.isPending} onClick={() => removeHistory.mutate(deleteTarget)}>
          {deleteTarget === 'all' ? '确认清空' : '确认删除'}
        </button>
      </div>
    </Dialog>}
    {workId && searchId && <WorkDrawer workId={workId} close={() => change({ work_id: '' })} sourceKind="search" sourceId={searchId} />}
  </>;
}

export function AccountsPage() {
  const { params, change, apiParams } = useFilters();
  const navigate = useNavigate();
  const [adding, setAdding] = useState(false);
  const settings = useApi<Settings>('settings');
  const query = useApi<Page<Account>>('accounts', apiParams('platform', 'q', 'cursor').toString());
  const accountId = params.get('account_id') || undefined;
  const eligible = (account: Account) => account.tracking === 'active' && settings.data?.enabled_platforms.includes(account.platform);
  const scan = useWorkspaceMutation((id?: string) => write<{ job_id: string; job_ids: string[] }>('account-scans', 'POST', id
    ? { account_ids: [id] } : { platform: params.get('platform') || null, q: params.get('q') || '' }),
  ({ job_id, job_ids }) => navigate(job_ids.length > 1 ? '/jobs?kind=account_scan' : '/jobs?job_id=' + job_id));
  return <>
    <PageTitle title="对标账号" description="跟踪创作者更新，查看各账号的作品和采集结果。"
      action={<button className="button primary" onClick={() => setAdding(true)}><Plus size={17} />添加账号</button>} />
    <div className="filter-bar simple">
      <PlatformTabs value={params.get('platform') || ''} change={(platform) => change({ platform }, true)} />
      <div className="filter-right">
        <SearchField label="检索账号" placeholder="搜索账号…" value={params.get('q') || ''} onChange={(q) => change({ q }, true)} />
        <button className="button" disabled={scan.isPending || !query.data?.items.some(eligible) && !query.data?.next_cursor}
          onClick={() => scan.mutate(undefined)}><RefreshCw size={15} />扫描当前范围</button>
      </div>
    </div>
    <LoadState pending={query.isPending} error={query.error} />
    {scan.error && <p className="form-error" role="alert">{scan.error.message}</p>}
    {query.data && <div className="panel table-panel"><table>
      <thead><tr><th>账号</th><th>平台</th><th>跟踪状态</th><th>最近扫描</th><th>操作</th></tr></thead>
      <tbody>{query.data.items.map((account) => <tr key={account.id}>
        <td><div className="account-cell"><span className="account-avatar" aria-hidden="true">{Array.from(account.nickname)[0]}</span>
          <div><button className="account-name" onClick={() => change({ account_id: account.id })}>{account.nickname}</button>
            <small className="account-note">{account.note || '—'}</small></div></div></td>
        <td><Platform id={account.platform} /></td><td><span className={'status' + (account.tracking === 'active' ? ' green' : '')}>
          <i />{account.tracking === 'active' ? '跟踪中' : '已暂停'}</span></td>
        <td>{account.last_scan ? <Link to={'/jobs?job_id=' + account.last_scan.id} title={account.last_scan.message}>
          {account.last_scan.started_at ? displayTime(account.last_scan.finished_at || account.last_scan.started_at)
            : <Status value={account.last_scan.status} />}</Link> : '—'}</td>
        <td><div className="row-actions"><Link className="text-button" to={'/feed?account_id=' + account.id}>作品</Link>
          <button className="text-button" disabled={!eligible(account) || scan.isPending} onClick={() => scan.mutate(account.id)}>扫描</button>
          <button className="text-button subdued" onClick={() => change({ account_id: account.id })}>详情</button></div></td>
      </tr>)}</tbody>
    </table>{!query.data.items.length && <Empty title="没有匹配的账号" icon={Users} />}</div>}
    {query.data && <Pager cursor={params.get('cursor') || ''} next={query.data.next_cursor} change={(cursor) => change({ cursor })} />}
    {adding && <AccountForm close={() => setAdding(false)} />}
    {accountId && <AccountDrawer id={accountId} close={() => change({ account_id: '' })} />}
  </>;
}

function AccountForm({ close, account }: { close: () => void; account?: Account }) {
  const navigate = useNavigate();
  const [url, setUrl] = useState('');
  const [note, setNote] = useState(account?.note || '');
  const [days, setDays] = useState(account?.lookback_days?.toString() || '');
  const [limit, setLimit] = useState(account?.scan_limit?.toString() || '');
  const save = useWorkspaceMutation(() => {
    const body = { note, lookback_days: days ? Number(days) : null, scan_limit: limit ? Number(limit) : null };
    return account ? write<{ job_id?: string }>('accounts/' + account.id, 'PATCH', body)
      : write<{ job_id?: string }>('accounts', 'POST', { ...body, url });
  }, ({ job_id }) => {
    close();
    if (job_id) navigate('/jobs?job_id=' + job_id);
  });
  return <Dialog title={account ? '编辑对标账号' : '添加对标账号'} close={close}>
    <form className="modal-form" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
      {!account && <Field label="创作者主页链接"><input type="url" autoFocus required value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" /></Field>}
      <Field label="备注"><input value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} /></Field>
      <div className="form-grid">
        <Field label="回看天数"><input type="number" min={1} max={365} value={days} placeholder="使用工作区默认值" onChange={(e) => setDays(e.target.value)} /></Field>
        <Field label="单次采集上限"><input type="number" min={1} max={100} value={limit} placeholder="使用工作区默认值" onChange={(e) => setLimit(e.target.value)} /></Field>
      </div>
      {save.error && <p className="form-error" role="alert">{save.error.message}</p>}
      <div className="modal-actions"><button type="button" className="button" onClick={close}>取消</button>
        <button className="button primary" disabled={save.isPending}>{account ? '保存账号' : '解析并添加'}</button></div>
    </form>
  </Dialog>;
}

function AccountDrawer({ id, close }: { id: string; close: () => void }) {
  const query = useApi<Account>('accounts/' + id);
  const [confirm, setConfirm] = useState(false);
  const [edit, setEdit] = useState(false);
  const change = useWorkspaceMutation((action: 'active' | 'paused' | 'remove') =>
    action === 'remove' ? write('accounts/' + id, 'DELETE')
      : write('accounts/' + id, 'PATCH', { tracking: action }), () => { if (confirm) close(); });
  const account = query.data;
  if (account && edit) return <AccountForm account={account} close={() => setEdit(false)} />;
  return <Dialog drawer title="对标账号详情" description={account?.nickname} close={close}>
    <LoadState pending={query.isPending} error={query.error} />
    {account && <div className="detail-body">
      <div className="inline-actions"><Platform id={account.platform} />
        <a className="text-button" href={account.profile_url} target="_blank" rel="noreferrer">打开主页<ExternalLink size={14} /></a>
        <Link className="text-button" to={'/feed?account_id=' + id}>查看已入库作品（{account.work_count || 0}）</Link>
      </div>
      {account.note && <p>{account.note}</p>}
      <p className="field-note">回看天数 · {account.lookback_days ?? '默认'}　单次采集上限 · {account.scan_limit ?? '默认'}</p>
      <h4>最近扫描</h4>
      {account.last_scan ? <>
        <p><Status value={account.last_scan.status} /> {account.last_scan.message}</p>
        <p>开始 · {displayTime(account.last_scan.started_at)}　结束 · {displayTime(account.last_scan.finished_at)}</p>
        <p>新增 {String(account.last_scan.result?.added ?? '—')} 条　更新 {String(account.last_scan.result?.updated ?? '—')} 条</p>
        <Link className="text-button" to={'/jobs?job_id=' + account.last_scan.id}>查看任务</Link>
      </> : <p>暂无扫描记录</p>}
      {confirm && <p className="callout">移除后停止跟踪此账号，历史作品及选题仍可查看。</p>}
      {change.error && <p className="form-error" role="alert">{change.error.message}</p>}
      {account.tracking !== 'removed' && <div className="modal-actions">
        {confirm ? <><button className="button" onClick={() => setConfirm(false)}>返回</button>
          <button className="button danger" disabled={change.isPending} onClick={() => change.mutate('remove')}>确认移除</button></>
          : <><button className="button danger" onClick={() => setConfirm(true)}>移除账号</button>
            <button className="button" onClick={() => setEdit(true)}>编辑</button>
            <button className="button" disabled={change.isPending} onClick={() => change.mutate(account.tracking === 'active' ? 'paused' : 'active')}>
              {account.tracking === 'active' ? '暂停跟踪' : '恢复跟踪'}</button></>}
      </div>}
    </div>}
  </Dialog>;
}
