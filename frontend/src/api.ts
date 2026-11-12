import type { Accepted, ResearchResult, RunList, RunProgress } from './types';

interface ErrorBody { error?: { code?: string; message?: string; run_id?: string | null; error_id?: string | null } }

export class ApiError extends Error {
  status: number;
  code: string;
  runId: string | null;
  errorId: string | null;

  constructor(status: number, code: string, message: string, runId: string | null = null, errorId: string | null = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.runId = runId;
    this.errorId = errorId;
  }
}

export function errorText(error: unknown): string {
  const text = error instanceof Error ? error.message : '暂时无法完成请求';
  return error instanceof ApiError && error.errorId ? `${text}（错误编号 ${error.errorId}）` : text;
}

/** Cancelling a browser request never cancels the background research. */
async function request<T>(path: string, init: RequestInit = {}, signal?: AbortSignal, blob = false): Promise<T> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  if (signal?.aborted) cancel();
  else signal?.addEventListener('abort', cancel, { once: true });
  let timedOut = false;
  const timer = window.setTimeout(() => { timedOut = true; controller.abort(); }, 20_000);
  try {
    const headers = new Headers(init.headers);
    if (init.body) headers.set('Content-Type', 'application/json');
    const response = await fetch(`/api${path}`, { ...init, headers, signal: controller.signal });
    if (!response.ok) {
      let body: ErrorBody = {};
      try { body = await response.json() as ErrorBody; } catch { /* Proxy errors may contain HTML. */ }
      throw new ApiError(response.status, body.error?.code ?? 'http_error', body.error?.message ?? `请求失败（HTTP ${response.status}）`, body.error?.run_id, body.error?.error_id);
    }
    if (response.status === 204) return undefined as T;
    return (blob ? await response.blob() : await response.json()) as T;
  } catch (error) {
    if (signal?.aborted) throw new DOMException('请求已取消', 'AbortError');
    if (error instanceof ApiError) throw error;
    throw new ApiError(0, timedOut ? 'timeout' : 'connection', timedOut ? '请求超时，服务器处理结果尚未确认' : '连接暂时中断，请确认后端服务已启动');
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener('abort', cancel);
  }
}

const runPath = (id: string) => `/research/${encodeURIComponent(id)}`;
export const getHealth = (signal?: AbortSignal) => request<{ status: string }>('/health', {}, signal);
export const listRuns = (offset: number, signal?: AbortSignal) => request<RunList>(`/research?limit=10&offset=${offset}`, {}, signal);
export const getProgress = (id: string, signal?: AbortSignal) => request<RunProgress>(runPath(id), {}, signal);
export const getResult = (id: string, signal?: AbortSignal) => request<ResearchResult>(`${runPath(id)}/result`, {}, signal);
export const getReport = (id: string) => request<Blob>(`${runPath(id)}/report`, {}, undefined, true);
export const deleteResearch = (id: string): Promise<void> => request<void>(runPath(id), { method: 'DELETE' });

export async function createResearch(topic: string, maxTasks: number): Promise<Accepted> {
  const result = await request<Accepted>('/research', { method: 'POST', body: JSON.stringify({ topic, max_tasks: maxTasks }) });
  if (!/^[0-9a-f]{32}$/.test(result.run_id)) throw new ApiError(0, 'invalid_response', '没有收到有效研究编号，请先检查历史记录');
  return result;
}

export const resumeResearch = (id: string, confirmed: boolean) => request<Accepted>(`${runPath(id)}/resume`, {
  method: 'POST', body: JSON.stringify({ recover_interrupted: confirmed }),
});
