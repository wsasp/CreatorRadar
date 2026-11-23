import { useMutation, useQueryClient } from '@tanstack/react-query';
import { AudioLines, FileText, Sparkles } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  displayTime, phases, refreshWorkspace, write,
  type Analysis, type AnalysisPoint, type Job, type TopicDetail,
} from './api';
import { Empty, Status } from './ui';

export type ProcessingKind = 'asr' | 'analysis';

function timestamp(seconds: number) {
  const millis = Math.round(seconds * 1000);
  const hours = Math.floor(millis / 3_600_000);
  const minutes = Math.floor(millis / 60_000) % 60;
  const remainder = Math.floor(millis / 1000) % 60;
  const fraction = millis % 1000;
  const pair = (value: number) => String(value).padStart(2, '0');
  return (hours ? pair(hours) + ':' : '') + pair(minutes) + ':' + pair(remainder) +
    (fraction ? '.' + String(fraction).padStart(3, '0') : '');
}

function timeRange(start: number, end: number) {
  return timestamp(start) + '–' + timestamp(end);
}

function escapeMarkdown(text: string) {
  return text.replace(/[\\`*_[\]{}<>#|~&]/g, '\\$&')
    .replace(/^([ \t]*)([-+=])(?=[ \t=-]|$)/gm, '$1\\$2')
    .replace(/^([ \t]*\d{1,9})([.)])(?=[ \t]|$)/gm, '$1\\$2');
}

function ProcessingStatus({ job, retry, pending }: {
  job: Job; retry: () => void; pending: boolean;
}) {
  const retryable = ['failed', 'interrupted', 'waiting_login'].includes(job.status) &&
    job.error_code !== 'ai_settings_changed';
  const savedAsHistory = job.kind === 'analysis' && job.result?.current === false;
  return <div className="callout processing-status" role="status">
    <div><Status value={job.status} />
      <span>{job.cancel_requested ? '正在取消'
        : savedAsHistory ? '输入已更新，拆解已保存为历史版本。' : job.message}</span>
      {phases[job.phase] && <small>阶段 · {phases[job.phase]}</small>}
    </div>
    <div className="inline-actions">
      {retryable && <button className="text-button" disabled={pending} onClick={retry}>重试任务</button>}
      {(job.status === 'waiting_login' || job.error_code === 'platform_disabled') &&
        <Link className="text-button" to={'/settings?platform=' + job.platform}>平台设置</Link>}
      {job.error_code && ['asr_engine_unavailable', 'asr_model_unavailable', 'asr_model_disk_space', 'ai_key_required',
        'ai_auth', 'ai_settings_changed', 'ai_codex_unavailable', 'ai_codex_auth', 'ai_codex_failed']
        .includes(job.error_code) &&
        <Link className="text-button" to="/settings">系统设置</Link>}
      <Link className="text-button" to={'/jobs?job_id=' + job.id}>查看采集任务</Link>
      <Link className="text-button" to={'/logs?job_id=' + job.id}>查看日志</Link>
    </div>
  </div>;
}

function Evidence({ point, analysis }: { point: AnalysisPoint; analysis: Analysis }) {
  return <><p>{point.text}</p>
    {Array.from(new Set(point.refs)).map((ref) => {
      const source = analysis.input_snapshot.sources[ref - 1];
      if (!source) return null;
      return <details className="evidence" key={ref}>
        <summary>{source.start !== undefined && source.end !== undefined
          ? '逐字稿 ' + timeRange(source.start, source.end) + (source.estimated ? '（估算）' : '')
          : '平台正文第 ' + ref + ' 段'}</summary>
        <blockquote>{source.text}</blockquote>
      </details>;
    })}
  </>;
}

function AnalysisResult({ analysis }: { analysis: Analysis }) {
  const { summary, hook, structure, key_points, techniques, extensions } = analysis.content;
  return <div className="analysis-content">
    {([['内容摘要', [summary]], ['开篇钩子', [hook]], ['段落结构', structure],
      ['核心观点', key_points], ['可借鉴的表达方法', techniques], ['可延展选题', extensions]] as const)
      .map(([label, points]) => <section key={label}><h5>{label}</h5>
        {points.length === 1 ? <Evidence point={points[0]} analysis={analysis} />
          : <ol>{points.map((point, index) => <li key={index}><Evidence point={point} analysis={analysis} /></li>)}</ol>}
      </section>)}
  </div>;
}

export function TopicProcessing({ topic, kind }: { topic: TopicDetail; kind: ProcessingKind }) {
  const client = useQueryClient();
  const [copyStatus, setCopyStatus] = useState('');
  const { work, transcription, analysis } = topic;
  const estimatedTiming = transcription?.segments.some((segment) => segment.estimated);
  const job = topic.processing_jobs[kind];
  const command = useMutation({
    mutationFn: (jobId?: string) => write<{ job_id: string }>(
      jobId ? 'jobs/' + jobId + '/retry'
        : 'topics/' + topic.id + '/' + (kind === 'asr' ? 'transcriptions' : 'analyses'), 'POST',
    ),
    onSuccess: () => refreshWorkspace(client, ['topics', 'jobs', 'events', 'overview']),
  });
  const busy = command.isPending || job?.status === 'queued' || job?.status === 'running';
  const video = work.kind === 'video';
  const missing = video ? !transcription : work.kind !== 'article' || !work.body?.trim();
  const status = <>{job && <ProcessingStatus job={job} retry={() => command.mutate(job.id)} pending={busy} />}
    {command.error && <p className="form-error" role="alert">{command.error.message}</p>}</>;

  async function copyTranscript(withTimestamps: boolean) {
    if (!transcription) return;
    const segments = transcription.segments.map((segment) => withTimestamps
      ? timeRange(segment.start, segment.end) + ' ' + segment.text
      : segment.text).join('\n');
    const text = withTimestamps && estimatedTiming ? '时间戳按文本位置估算。\n' + segments : segments;
    try {
      await navigator.clipboard.writeText(text);
      setCopyStatus(withTimestamps ? '已复制逐字稿' : '已复制纯文本逐字稿');
    } catch {
      setCopyStatus('复制失败，请选择文字后手动复制。');
    }
  }

  function saveTranscript() {
    if (!transcription) return;
    const title = work.title.trim().replace(/\s+/g, ' ') || '未命名作品';
    const paragraphs = transcription.segments.map(({ text }) => escapeMarkdown(text));
    const markdown = [`# ${escapeMarkdown(title)}`, ...paragraphs].join('\n\n') + '\n';
    const filenameTitle = Array.from(title.replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_')).slice(0, 60).join('');
    const url = URL.createObjectURL(new Blob([markdown], { type: 'text/markdown;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `逐字稿-${filenameTitle}.md`;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  if (kind === 'asr') {
    if (!video) return <Empty title={work.kind === 'article' ? '图文作品' : '作品类型待确认'}
      message={work.kind === 'article' ? '可阅读平台正文，或直接生成 AI 拆解。' : '请检查原帖并重新采集。'} icon={FileText} />;
    return <>
      <div className="section-head"><div><h4>ASR 逐字稿</h4>
        {transcription && <small>{transcription.engine} · {transcription.model} · {displayTime(transcription.created_at)}
          {estimatedTiming && ' · 时间戳为估算'}</small>}
      </div><button className="button primary" disabled={busy} onClick={() => command.mutate(undefined)}>
        <AudioLines size={16} />{busy ? '转写任务处理中' : transcription ? '重新转写' : '开始转写'}
      </button></div>
      {status}
      {transcription ? <>
        <div className="inline-actions transcript-actions">
          <button type="button" className="button" onClick={() => copyTranscript(true)}>复制逐字稿</button>
          <button type="button" className="button" onClick={() => copyTranscript(false)}>复制纯文本</button>
          <button type="button" className="button" onClick={saveTranscript}>保存为 Markdown</button>
          {copyStatus && <span role="status">{copyStatus}</span>}
        </div>
        <ul className="transcript">{transcription.segments.map(({ start, end, text }) =>
          <li key={start}><span>{timeRange(start, end)}</span><p>{text}</p></li>)}</ul>
      </> : <Empty title="暂无逐字稿" message="转写完成后会显示带时间戳的文本。" icon={AudioLines} />}
    </>;
  }
  return <>
    <div className="section-head"><div><h4>AI 内容拆解</h4>
      {analysis && <small>{analysis.model} · {displayTime(analysis.created_at)}</small>}
    </div><button className="button primary" disabled={busy || missing} onClick={() => command.mutate(undefined)}>
      <Sparkles size={16} />{busy ? '拆解任务处理中' : analysis ? '重新生成' : '生成拆解'}
    </button></div>
    {status}
    {analysis?.stale && <p className="callout" role="status">
      {analysis.input_snapshot.kind === 'video' ? '逐字稿或作品简介已更新' : '平台正文已更新'}，可重新生成拆解。
    </p>}
    {analysis ? <AnalysisResult analysis={analysis} /> : <Empty title="暂无拆解结果"
      message={missing ? video ? '请先在「逐字稿」完成转写。' : '缺少平台正文，请重新采集作品。'
        : '生成后可查看拆解及其原文依据。'} icon={Sparkles} />}
  </>;
}
