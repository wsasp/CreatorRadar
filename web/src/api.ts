import { useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

export const platforms = { douyin: '抖音', xiaohongshu: '小红书', bilibili: 'B站' } as const;
export const types = {
  quick_capture: '快速采集', account_scan: '账号扫描', search: '关键词搜索',
  account_resolve: '添加账号', platform_login: '平台登录', media_fetch: '媒体获取',
  asr: 'ASR 转写', analysis: 'AI 拆解', self_test: '队列验收',
} as const;
export const statuses = {
  queued: '排队中', running: '运行中', waiting_login: '待登录', succeeded: '成功',
  partially_succeeded: '部分成功', failed: '失败', cancelled: '已取消', interrupted: '中断',
} as const;
export const levels = { info: '信息', warning: '警告', error: '错误' } as const;
export const phases: Record<string, string> = {
  starting: '正在认领', executing: '执行中', account: '解析主页', scan: '扫描作品',
  search: '搜索作品', detail: '获取作品详情', login: '等待平台登录验证',
  media: '获取媒体', audio_extract: '提取音轨', transcribing: '转写', analyzing: '生成拆解',
};

export type Platform = keyof typeof platforms;
type Status = keyof typeof statuses;
type Kind = keyof typeof types;
export type Job = {
  id: string; parent_id: string | null; is_batch: boolean; kind: Kind;
  platform: Platform | null; title: string; status: Status; phase: string;
  created_at: string; started_at: string | null; finished_at: string | null;
  duration_ms: number | null; attempt_count: number; cancel_requested: boolean;
  error_code: string | null; message: string; result: Record<string, unknown> | null;
  context: { trigger?: string; account_id?: string; search_id?: string; work_id?: string; topic_id?: string };
};
type Attempt = Pick<Job, 'status' | 'started_at' | 'finished_at' | 'duration_ms' | 'error_code'> & {
  number: number; message: string | null;
};
export type JobDetail = Job & { children: Job[]; attempts: Attempt[] };
export type LogEvent = {
  id: number; job_id: string; parent_id: string | null; attempt: number | null;
  created_at: string; kind: Kind; platform: Platform | null;
  level: keyof typeof levels; code: string; message: string;
};
export type Page<T, C = string> = { items: T[]; next_cursor: C | null };
type Progress = { completed: number; total: number };
export type Overview = {
  running_jobs: number; works_24h: number; tracked_accounts: number; active_topics: number;
  recent_jobs: Job[]; platforms: { platform: Platform; accounts: number; works: number }[];
  transcription: Progress; analysis: Progress;
};
export type AiConfig = {
  provider: 'api' | 'codex_cli'; base_url: string; model: string;
  codex_model: string; codex_reasoning_effort: string;
};
export type CodexModel = { id: string; name: string; reasoning_efforts: string[] };
export type ScanPlan = { enabled: boolean; time: string; lookback_days: number; limit: number; platforms?: Platform[] };
export type Settings = {
  enabled_platforms: Platform[]; workspace: 'production' | 'acceptance'; database_path: string; media_path: string;
  scan: ScanPlan;
  asr: {
    engine: 'FunASR' | 'faster-whisper'; model: string; language: 'zh' | 'auto';
    base_url: string; key_configured: boolean;
  };
  ai: AiConfig & { key_configured: boolean };
  retention: { media_days: number; search_days: number; log_days: number };
  integrations: { crawler: { state: 'ready' | 'unavailable' | 'fixture'; revision: string; python: string };
    asr: 'installed' | 'unavailable'; ai: 'configured' | 'unconfigured' | 'unavailable' };
  sessions: Record<Platform, { state: string; checked_at: string | null; job_id: string | null }>;
};
export const sessionLabels: Record<string, string> = {
  ready: '会话可用', unknown: '待登录', login_required: '待登录',
  challenge: '需要平台验证', rate_limited: '平台限流',
};
export type Measure = { count: number | null; field: string | null; availability: string; collected_at: string | null };
export type Source = { kind: 'scan' | 'search' | 'quick'; label: string; source_id: string; job_id: string; discovered_at: string };
export type Work = {
  id: string; platform: Platform; kind: 'video' | 'article' | 'unknown'; title: string;
  author: string | null; body: string | null; original_url: string | null; cover_url: string | null;
  image_urls?: string[];
  published_at: string | null; collected_at: string; duration_seconds: number | null;
  metrics: Record<string, Measure>; reads: Measure | null; topic_id: string | null; sources?: Source[];
};
export type Topic = { id: string; work: Work; source: Source; archived: boolean; transcribed: boolean };
export type Segment = { start: number; end: number; text: string; estimated?: boolean };
export type Transcription = {
  id: string; engine: string; model: string; model_revision: string | null; language: string;
  created_at: string; segments: Segment[];
};
export type AnalysisPoint = { text: string; refs: number[] };
export type Analysis = {
  id: string; model: string; created_at: string; stale: boolean;
  input_snapshot: {
    kind: 'video' | 'article'; sources: { id: number; text: string; start?: number; end?: number; estimated?: boolean }[];
  };
  content: {
    summary: AnalysisPoint; hook: AnalysisPoint; structure: AnalysisPoint[];
    key_points: AnalysisPoint[]; techniques: AnalysisPoint[]; extensions: AnalysisPoint[];
  };
};
export type TopicDetail = Topic & {
  transcription: Transcription | null; analysis: Analysis | null;
  processing_jobs: Record<'asr' | 'analysis', Job | null>;
};
export type Account = {
  id: string; platform: Platform; nickname: string; profile_url: string; note: string;
  tracking: 'active' | 'paused' | 'removed'; lookback_days: number | null; scan_limit: number | null;
  work_count?: number;
  last_scan: Pick<Job, 'id' | 'status' | 'started_at' | 'finished_at' | 'message' | 'result'> | null;
};
export type Search = { id: string; keyword: string; platforms: Platform[]; job_id: string;
  status: Status; created_at: string };
export type SearchResults = Page<Work> & { search: Search; children: Job[] };
export type Health = { database: 'ready' | 'unavailable'; worker: 'online' | 'offline' };

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch('/api/v1/' + path, init);
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.message || '工作区暂不可用，请稍后重试。');
  }
  return response.json();
}

export function write<T>(path: string, method: 'POST' | 'PATCH' | 'PUT' | 'DELETE', body?: unknown) {
  return request<T>(path, { method, ...(body === undefined ? {} : {
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }) });
}

export function refreshWorkspace(client: ReturnType<typeof useQueryClient>, resources?: string[]) {
  return client.invalidateQueries({
    predicate: ({ queryKey }) => {
      const resource = String(queryKey[0]);
      const root = resource.split('/')[0];
      return (!resources || resources.includes(root)) &&
        !(resource === 'events' && new URLSearchParams(String(queryKey[1])).has('before'));
    },
  });
}

export function useApi<T>(resource: string, params = '', enabled = true, refetchInterval?: number) {
  return useQuery({
    queryKey: [resource, params],
    queryFn: ({ signal }) => request<T>(resource + (params ? '?' + params : ''), { signal }),
    enabled,
    refetchInterval,
  });
}

export function useLiveJobs() {
  const client = useQueryClient();
  useEffect(() => {
    const stream = new EventSource('/api/v1/events/stream');
    const refresh = () => { void refreshWorkspace(client); };
    stream.addEventListener('open', refresh);
    stream.addEventListener('job-event', refresh);
    return () => stream.close();
  }, [client]);
}

const dateFormat = new Intl.DateTimeFormat('zh-CN', {
  dateStyle: 'short', timeStyle: 'medium', hour12: false,
});
export function displayTime(value: string | null) {
  return value ? dateFormat.format(new Date(value)) : '—';
}

export function displayDuration(ms: number | null) {
  if (ms === null) return '—';
  if (ms < 1000) return `${ms} 毫秒`;
  return `${(ms / 1000).toFixed(1)} 秒`;
}
