import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, Database, Sparkles } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { refreshWorkspace, request, write, type AiConfig, type CodexModel, type Health, type Settings } from './api';
import { Values } from './ui';

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="field"><span>{label}</span>{children}</label>;
}

function Feedback({ error, success }: { error: Error | null; success?: string }) {
  if (error) return <p className="form-error" role="alert">{error.message}</p>;
  if (success) return <p className="field-note" role="status">{success}</p>;
  return null;
}

type AsrCheck = Record<'ffmpeg' | 'ffprobe' | 'engine' | 'model', 'ready' | 'unavailable' | 'unknown'>;
const stateLabels = { ready: '可用', unavailable: '不可用', unknown: '未检测' };
const asrModels: Record<Settings['asr']['engine'], string[]> = {
  'faster-whisper': ['tiny', 'base', 'small', 'medium', 'large-v3-turbo', 'large-v3'],
  FunASR: ['paraformer', 'fun-asr-nano', 'sensevoice'],
};

export function AsrPanel({ data }: { data: Settings }) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<Settings['asr'] | null>(null);
  const [key, setKey] = useState('');
  const value = draft ?? data.asr;
  const models = asrModels[value.engine];
  const useFunAsr = value.engine === 'FunASR';
  const hasKey = value.base_url === data.asr.base_url && data.asr.key_configured;
  const pendingKey = useFunAsr && Boolean(key.trim());
  const changed = JSON.stringify(value) !== JSON.stringify(data.asr);
  const check = useMutation({ mutationFn: () => write<AsrCheck>('settings/asr/check', 'POST') });
  const save = useMutation({
    mutationFn: (action: 'save' | 'clear') => write<Settings>('settings', 'PATCH', { asr: {
      engine: value.engine, model: value.model, language: value.language,
      ...(useFunAsr && { base_url: value.base_url }),
      ...(action === 'clear' ? { api_key: null } : pendingKey && { api_key: key }),
    } }),
    onSuccess: (saved) => {
      client.setQueryData(['settings', ''], saved);
      setDraft(null);
      setKey('');
      check.reset();
    },
  });
  const update = (next: Settings['asr']) => {
    setDraft(next);
    save.reset();
    check.reset();
  };
  const disabled = save.isPending || check.isPending;
  return <section className="panel settings-panel">
    <h3><Activity size={18} />ASR 转写</h3>
    {!useFunAsr && data.integrations.asr === 'unavailable' && <p>本机 ASR 环境未就绪。重启工作台会自动准备默认模型，失败原因见启动日志。</p>}
    <form onSubmit={(event) => { event.preventDefault(); save.mutate('save'); }}>
      <div className="form-grid">
        <Field label="ASR 引擎"><select value={value.engine} disabled={disabled}
          onChange={(event) => {
            const engine = event.target.value as Settings['asr']['engine'];
            setKey('');
            update(engine === data.asr.engine ? data.asr : {
              engine, model: engine === 'FunASR' ? 'paraformer' : 'small', language: 'zh',
              base_url: '', key_configured: false,
            });
          }}><option>FunASR</option><option>faster-whisper</option></select></Field>
        <Field label="ASR 模型"><select required value={value.model}
          disabled={disabled}
          onChange={(event) => update({ ...value, model: event.target.value })}>
          {!models.includes(value.model) && <option value={value.model}>{value.model}</option>}
          {models.map((model) => <option key={model} value={model}>{model}</option>)}
        </select></Field>
        <Field label="转写语言"><select value={value.language}
          disabled={useFunAsr || disabled}
          onChange={(event) => update({ ...value, language: event.target.value as Settings['asr']['language'] })}>
          <option value="zh">中文</option>
          {!useFunAsr && <option value="auto">自动识别</option>}
        </select></Field>
        {useFunAsr && <>
          <Field label="FunASR API 地址"><input type="url" required maxLength={2048} disabled={disabled}
            placeholder="https://example.com/v1" value={value.base_url ?? ''}
            onChange={(event) => update({ ...value, base_url: event.target.value })} /></Field>
          <Field label="FunASR API Key（可选）"><input type="password" autoComplete="new-password"
            maxLength={1024} disabled={disabled} value={key}
            placeholder={hasKey ? '已配置，输入可替换' : '无需认证时可留空'}
            onChange={(event) => { setKey(event.target.value); save.reset(); check.reset(); }} /></Field>
        </>}
      </div>
      {useFunAsr && <p className="field-note">转写时会将作品音轨发送到所填的 OpenAI 兼容服务。</p>}
      <div className="inline-actions">
        <button className="button primary" disabled={disabled || (!changed && !pendingKey)}>保存 ASR 设置</button>
        <button type="button" className="button" disabled={disabled || changed || pendingKey}
          onClick={() => check.mutate()}>{check.isPending ? '正在检查' : '检查 ASR 环境'}</button>
        {useFunAsr && hasKey && !changed && <button type="button" className="button" disabled={disabled || pendingKey}
          onClick={() => save.mutate('clear')}>清除 FunASR Key</button>}
      </div>
      {check.data && <Values rows={([
        ['FFmpeg', 'ffmpeg'], ['FFprobe', 'ffprobe'], ['引擎', 'engine'], ['模型', 'model'],
      ] as const).map(([label, key]) => [label, stateLabels[check.data[key]]])} />}
      <Feedback error={save.error || check.error} success={save.isSuccess ? 'ASR 设置已保存' : undefined} />
    </form>
  </section>;
}

const effortLabels: Record<string, string> = {
  none: '无', minimal: '最低', low: '低', medium: '中', high: '高',
  xhigh: '很高', max: '最高', ultra: 'Ultra',
};

function CodexFields({ value, disabled, update }: {
  value: AiConfig; disabled: boolean; update: (value: AiConfig) => void;
}) {
  const catalog = useQuery({
    queryKey: ['settings/ai/codex-models'],
    queryFn: ({ signal }) => request<{ models: CodexModel[] }>('settings/ai/codex-models', { signal }),
    retry: false,
    staleTime: 'static',
  });
  const models = catalog.data?.models ?? [];
  const selected = models.find((model) => model.id === value.codex_model);
  const efforts = selected?.reasoning_efforts ?? Object.keys(effortLabels);
  const selectModel = (id: string) => {
    const supported = models.find((model) => model.id === id)?.reasoning_efforts;
    const effort = value.codex_reasoning_effort;
    update({ ...value, codex_model: id, codex_reasoning_effort: supported?.includes(effort) ? effort : '' });
  };
  return <>
    <p className="field-note">使用运行工作台的本机 Codex CLI。首次使用请在终端运行 <code>codex login</code>。</p>
    <div className="form-grid">
      <Field label="Codex 模型">
        {models.length || catalog.isPending ? <select value={value.codex_model} disabled={disabled || catalog.isPending}
          onChange={(event) => selectModel(event.target.value)}>
          <option value="">{catalog.isPending ? '正在读取模型…' : '跟随 CLI 配置'}</option>
          {value.codex_model && !selected && <option value={value.codex_model}>{value.codex_model}</option>}
          {models.map(({ id, name }) => <option key={id} value={id}>{name}</option>)}
        </select> : <input value={value.codex_model} maxLength={200} disabled={disabled}
          placeholder="留空时跟随 CLI 配置" onChange={(event) => selectModel(event.target.value)} />}
      </Field>
      <Field label="思考等级"><select value={value.codex_reasoning_effort} disabled={disabled || catalog.isPending}
        onChange={(event) => update({ ...value, codex_reasoning_effort: event.target.value })}>
        <option value="">跟随 CLI 配置</option>
        {value.codex_reasoning_effort && !efforts.includes(value.codex_reasoning_effort) &&
          <option value={value.codex_reasoning_effort}>{value.codex_reasoning_effort}</option>}
        {efforts.map((effort) => <option key={effort} value={effort}>{effortLabels[effort] ?? effort}</option>)}
      </select></Field>
    </div>
    <Feedback error={catalog.error} />
    <button type="button" className="text-button" disabled={disabled || catalog.isFetching}
      onClick={() => { void catalog.refetch(); }}>刷新模型列表</button>
  </>;
}

function aiSelection(value: AiConfig) {
  return value.provider === 'codex_cli'
    ? { provider: value.provider, codex_model: value.codex_model, codex_reasoning_effort: value.codex_reasoning_effort }
    : { provider: value.provider, base_url: value.base_url, model: value.model };
}

export function AiPanel({ data }: { data: Settings }) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<AiConfig | null>(null);
  const [key, setKey] = useState('');
  const value = draft ?? data.ai;
  const useCodex = value.provider === 'codex_cli';
  const selection = aiSelection(value);
  const changed = JSON.stringify(selection) !== JSON.stringify(aiSelection(data.ai));
  const pendingKey = !useCodex && Boolean(key.trim());
  const command = useMutation({
    mutationFn: async (action: 'save' | 'clear' | 'test') => {
      if (action === 'test') {
        await write('settings/ai/test', 'POST');
        return '连接成功，服务返回了可用的拆解结果';
      }
      if (action === 'clear') {
        const saved = await write<Settings>('settings/ai-key?base_url=' +
          encodeURIComponent(data.ai.base_url), 'DELETE');
        client.setQueryData(['settings', ''], saved);
        setKey('');
        return '已清除 API Key';
      }
      let baseUrl = data.ai.base_url;
      if (changed) {
        const saved = await write<Settings>('settings', 'PATCH', {
          ai: selection,
        });
        client.setQueryData(['settings', ''], saved);
        baseUrl = saved.ai.base_url;
        setDraft(null);
      }
      if (pendingKey) {
        const saved = await write<Settings>('settings/ai-key', 'PUT', { api_key: key, base_url: baseUrl });
        client.setQueryData(['settings', ''], saved);
      }
      setKey('');
      return 'AI 设置已保存';
    },
  });
  const disabled = command.isPending;
  const update = (next: AiConfig) => { setDraft(next); command.reset(); };
  return <section className="panel settings-panel">
    <h3><Sparkles size={18} />AI 拆解</h3>
    <p>拆解时会向配置的服务发送视频逐字稿、标题和简介，或图文作品的正文。连接测试使用合成文本。</p>
    {!useCodex && data.ai.provider === 'api' && data.integrations.ai === 'unavailable' && <p>系统凭据存储暂不可用。</p>}
    <form className="ai-settings-form" onSubmit={(event) => { event.preventDefault(); command.mutate('save'); }}>
      <Field label="拆解方式"><select value={value.provider} disabled={disabled}
        onChange={(event) => update({ ...value, provider: event.target.value as AiConfig['provider'] })}>
        <option value="api">OpenAI 兼容 API</option>
        <option value="codex_cli">Codex CLI</option>
      </select></Field>
      {useCodex ? <CodexFields value={value} disabled={disabled} update={update} /> : <>
        <div className="form-grid">
          <Field label="AI 服务地址"><input type="url" placeholder="https://…" maxLength={2048}
            value={value.base_url} disabled={disabled}
            onChange={(event) => update({ ...value, base_url: event.target.value })} /></Field>
          <Field label="AI 模型"><input maxLength={200} value={value.model} disabled={disabled}
            onChange={(event) => update({ ...value, model: event.target.value })} /></Field>
        </div>
        <div className="key-field"><Field label="API Key">
          <input type="password" autoComplete="new-password" maxLength={1024} value={key}
            disabled={disabled} placeholder={value.base_url === data.ai.base_url && data.ai.key_configured
              ? '已配置，输入可替换' : '输入 API Key'}
            onChange={(event) => { setKey(event.target.value); command.reset(); }} />
        </Field></div>
      </>}
      <div className="inline-actions">
        <button className="button primary" disabled={disabled || (!changed && !pendingKey)}>保存 AI 设置</button>
        <button type="button" className="button" disabled={disabled || changed || pendingKey ||
          (!useCodex && (!data.ai.model || !data.ai.key_configured))}
          onClick={() => command.mutate('test')}>测试 AI 连接</button>
        {!useCodex && data.ai.key_configured && !changed && <button type="button" className="button" disabled={disabled}
          onClick={() => command.mutate('clear')}>清除 Key</button>}
      </div>
      <Feedback error={command.error} success={command.data} />
    </form>
  </section>;
}

export function StoragePanel({ data, worker }: { data: Settings; worker?: Health['worker'] }) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<{ media_days: string; log_days: string } | null>(null);
  const value = draft ?? {
    media_days: String(data.retention.media_days), log_days: String(data.retention.log_days),
  };
  const save = useMutation({
    mutationFn: () => write<Settings>('settings', 'PATCH', {
      retention: { media_days: Number(value.media_days), log_days: Number(value.log_days) },
    }),
    onSuccess: async () => { await refreshWorkspace(client, ['settings']); setDraft(null); },
  });
  return <section className="panel settings-panel"><h3><Database size={18} />存储与日志</h3>
    <Values rows={[
      ['数据库', data.database_path],
      ['临时媒体', data.media_path], ['Worker', worker === 'online' ? '运行中' : worker === 'offline' ? '离线' : '未知'],
      ['搜索历史保留天数', data.retention.search_days],
    ]} />
    <form onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
      <div className="form-grid">
        {([['media_days', '临时媒体保留天数'], ['log_days', '运行日志保留天数']] as const)
          .map(([field, label]) => <Field key={field} label={label}>
            <input required type="number" min={1} max={365} value={value[field]} disabled={save.isPending}
              onChange={(event) => { setDraft({ ...value, [field]: event.target.value }); save.reset(); }} />
          </Field>)}
      </div>
      <div className="inline-actions"><button className="button primary" disabled={!draft || save.isPending}>
        保存保留设置
      </button></div>
      <Feedback error={save.error} success={save.isSuccess ? '保留设置已保存' : undefined} />
    </form>
  </section>;
}
