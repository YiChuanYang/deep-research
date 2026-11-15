export type RunStatus = 'pending' | 'running' | 'failed' | 'completed';
export type TaskStatus = 'pending' | 'running' | 'completed' | 'skipped';
export interface Accepted { run_id: string; accepted: boolean; status_url: string; result_url: string }
export interface RunItem {
  run_id: string; topic: string; status: RunStatus; updated_at: string; max_tasks: number | null;
}
export interface RunList { items: RunItem[]; limit: number; offset: number; has_more: boolean }
export interface TaskProgress {
  id: number; title: string; status: TaskStatus; selected: boolean; evidence_count: number; citation_count: number;
}
export interface RunProgress extends RunItem {
  execution: 'idle' | 'queued' | 'executing'; next_nodes: string[]; active_task_id: number | null;
  planned_tasks: number; selected_tasks: number; completed_tasks: number; skipped_tasks: number;
  tasks: TaskProgress[]; report_ready: boolean; requires_recovery_confirmation: boolean; error_message: string | null;
  report_iteration?: ReportIterationProgress | null;
}
export interface ReportIterationProgress {
  revision: number; max_revisions: number; status: string; stop_reason: string; draft_count: number;
  initial_total: number; initial_completed: number; supplemental_total: number; supplemental_completed: number;
  issue_count: number | null;
}
export interface ReportIssue {
  kind: string; reason: string; paragraph_id: string; evidence_ids: string[]; question: string; query: string;
}
export interface ReportIterationDetail {
  run_id: string;
  iteration: null | {
    drafts: { revision: number; text: string; citation_ids: string[]; review: { issues: ReportIssue[] } | null; review_error: string | null }[];
    supplements: { task_id: number; revision: number; question: string; query: string; novel_evidence_ids: string[] }[];
  };
}
export interface Citation { evidence_id: string; title: string; url: string; text: string }
export interface TaskResult { id: number; title: string; status: TaskStatus; summary: string | null; citation_ids: string[] }
export interface ResearchResult { run_id: string; report_markdown: string; tasks: TaskResult[]; citations: Citation[] }
