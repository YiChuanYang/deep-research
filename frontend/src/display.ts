import type { RunProgress, RunStatus, TaskStatus } from './types';

export const runLabels: Record<RunStatus, string> = { pending: '待执行', running: '进行中', failed: '执行失败', completed: '已完成' };
export const taskLabels: Record<TaskStatus, string> = { pending: '待处理', running: '处理中', completed: '已完成', skipped: '已跳过' };
const nodeLabels: Record<string, string> = { plan: '规划研究', select_task: '选择任务', rewrite_query: '改写搜索词', search: '检索资料', summarize: '生成摘要', save_note: '保存任务结果', report: '汇总报告', save_report: '保存报告' };
export const nodeLabel = (name: string) => nodeLabels[name] ?? name;
export const shortId = (id: string) => id.slice(0, 8).toUpperCase();
export function progressLabel(value: RunProgress): string {
  if (value.report_ready) return '报告已就绪';
  if (value.execution === 'queued') return '等待执行';
  if (value.execution === 'executing') return '研究进行中';
  if (value.requires_recovery_confirmation) return '执行已中断';
  return runLabels[value.status];
}
export function formatDate(value: string, full = false): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '时间未知';
  return date.toLocaleString('zh-CN', full
    ? { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }
    : { month: '2-digit', day: '2-digit' });
}
export function safeUrl(value: string): string | null {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : null; }
  catch { return null; }
}
export function hostname(value: string): string {
  try { return new URL(value).hostname.replace(/^www\./, ''); } catch { return '来源'; }
}
