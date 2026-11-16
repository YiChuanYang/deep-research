<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { Check, Circle, Clock3, FileText, Layers3, Link2, LoaderCircle, Minus, Radio } from '@lucide/vue';
import { nodeLabel, progressLabel, taskLabels } from '../display';
import type { RunProgress } from '../types';

const props = defineProps<{ progress: RunProgress; loading: boolean; error: string; lastUpdated: number | null }>();
interface Activity { id: number; time: string; text: string }
const activities = ref<Activity[]>([]);
const now = ref(Date.now());
const hidden = ref(document.hidden);
let sequence = 0;
let timer: number | undefined;

const processed = computed(() => props.progress.completed_tasks + props.progress.skipped_tasks);
const percentage = computed(() => props.progress.selected_tasks ? Math.min(100, processed.value / props.progress.selected_tasks * 100) : 0);
const executing = computed(() => !props.error && !hidden.value && !props.progress.report_ready && props.progress.execution === 'executing');
const activeTask = computed(() => props.progress.tasks.find(task => task.id === props.progress.active_task_id));
const evidenceCount = computed(() => props.progress.tasks.reduce((sum, task) => sum + task.evidence_count, 0));
const citationCount = computed(() => props.progress.tasks.reduce((sum, task) => sum + task.citation_count, 0));
const steps = ['任务规划', '检索与摘要', '报告汇总', '报告就绪'];
// next_nodes 是检查点待执行节点，不是服务端实时正在执行的节点。
const stepIndex = computed(() => {
  const value = props.progress;
  if (value.report_ready) return 3;
  if (value.next_nodes.some(node => ['report', 'save_report', 'reflect_report', 'prepare_report_revision', 'finalize_report', 'block_report'].includes(node))) return 2;
  return value.tasks.length ? 1 : 0;
});
const checkpoint = computed(() => props.progress.next_nodes.map(nodeLabel).join(' / '));
const syncText = computed(() => {
  if (props.error) return '同步中断';
  if (hidden.value) return '页面隐藏 · 暂停同步';
  if (props.loading) return '同步中';
  if (props.lastUpdated === null) return '等待同步';
  const seconds = Math.max(0, Math.floor((now.value - props.lastUpdated) / 1000));
  return seconds < 2 ? '刚刚同步' : seconds < 60 ? `${seconds} 秒前同步` : `${Math.floor(seconds / 60)} 分钟前同步`;
});

function record(text: string): void {
  activities.value = [{
    id: ++sequence,
    time: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
    text,
  }, ...activities.value].slice(0, 12);
}

watch(() => props.progress, (value, previous) => {
  if (!previous) {
    record(`已读取进度：${progressLabel(value)}，${value.completed_tasks} 项完成`);
    return;
  }
  if (value.report_ready && !previous.report_ready) record('报告已就绪');
  if (value.execution !== previous.execution || value.status !== previous.status) {
    record(progressLabel(value));
  }
  if (value.tasks.length !== previous.tasks.length) record(`研究计划已更新：${value.planned_tasks} 项，本轮 ${value.selected_tasks} 项`);
  for (const task of value.tasks) {
    const before = previous.tasks.find(item => item.id === task.id);
    if (!before) continue;
    if (before.status !== task.status) record(`任务 ${task.id}「${task.title}」：${taskLabels[task.status]}`);
    if (before.evidence_count !== task.evidence_count) record(`任务 ${task.id}：已保存 ${task.evidence_count} 条证据`);
    if (before.citation_count !== task.citation_count) record(`任务 ${task.id}：摘要引用 ${task.citation_count} 条证据`);
    if (before.retrieval?.stage !== task.retrieval?.stage && task.retrieval?.stage) {
      const r = task.retrieval;
      record(`任务 ${task.id}：正文 ${r.full_pages ?? 0} 页 · 文本块 ${r.chunks ?? 0} · 选中 ${r.selected ?? 0} 条证据`);
    }
  }
  if (value.next_nodes.join(',') !== previous.next_nodes.join(',') && value.next_nodes.length) {
    record(`检查点更新 · 待执行：${value.next_nodes.map(nodeLabel).join(' / ')}`);
  }
}, { immediate: true });

function visibility(): void { hidden.value = document.hidden; now.value = Date.now(); }
onMounted(() => {
  timer = window.setInterval(() => { if (!document.hidden) now.value = Date.now(); }, 1000);
  document.addEventListener('visibilitychange', visibility);
});
onBeforeUnmount(() => { window.clearInterval(timer); document.removeEventListener('visibilitychange', visibility); });
</script>

<template>
  <div class="research-progress">
    <div class="live-panel" :class="{ 'is-live': executing, 'is-stale': Boolean(error) }">
      <div class="live-heading">
        <div class="live-status"><Radio :size="17" /><strong>{{ error ? '进度待同步' : progressLabel(progress) }}</strong></div>
        <span class="sync-state"><Clock3 :size="12" />{{ syncText }}</span>
      </div>
      <p v-if="progress.report_ready" class="live-title">报告已生成</p>
      <p v-else-if="progress.execution === 'queued'" class="live-title">等待后台执行</p>
      <p v-else-if="activeTask && stepIndex === 1" class="live-title">任务 {{ activeTask.id }} · {{ activeTask.title }}</p>
      <p v-else class="live-title">{{ steps[stepIndex] }}</p>
      <p v-if="checkpoint && !progress.report_ready" class="checkpoint-label">检查点待执行：{{ checkpoint }}</p>
      <div v-if="executing" class="activity-track" aria-hidden="true"><span></span></div>
    </div>

    <ol class="phase-track" aria-label="研究阶段">
      <li v-for="(step, index) in steps" :key="step" :class="{ reached: index < stepIndex || progress.report_ready, current: index === stepIndex, live: index === stepIndex && executing }" :aria-current="index === stepIndex ? 'step' : undefined">
        <span class="phase-icon"><Check v-if="index < stepIndex || progress.report_ready" :size="14" /><span v-else>{{ index + 1 }}</span></span><span>{{ step }}</span>
      </li>
    </ol>

    <div class="metric-grid">
      <div><Layers3 :size="17" /><span>任务已处理</span><strong>{{ processed }}<small> / {{ progress.selected_tasks }}</small></strong></div>
      <div><FileText :size="17" /><span>已保存证据</span><strong :key="evidenceCount" class="metric-value">{{ evidenceCount }}</strong></div>
      <div><Link2 :size="17" /><span>任务摘要引用</span><strong :key="citationCount" class="metric-value">{{ citationCount }}</strong></div>
    </div>
    <div v-if="!progress.report_iteration" class="progress-track" role="progressbar" aria-label="任务处理进度" :aria-valuenow="Math.round(percentage)" :aria-valuemin="0" :aria-valuemax="100"><span :style="{ width: `${percentage}%` }"></span></div>
    <div v-if="!progress.report_iteration" class="progress-caption"><span>完成 {{ progress.completed_tasks }} · 跳过 {{ progress.skipped_tasks }}</span><span>任务处理 {{ Math.round(percentage) }}% · 不含报告生成</span></div>

    <div v-if="!progress.tasks.length" class="planning-empty"><LoaderCircle v-if="executing" class="spin" :size="24" /><Circle v-else :size="24" /><h3>等待研究计划</h3></div>
    <ol v-else class="task-list">
      <li v-for="task in progress.tasks" :key="task.id" :class="{ muted: !task.selected, active: executing && task.id === progress.active_task_id && task.status === 'running' }">
        <span class="task-symbol"><Check v-if="task.status === 'completed'" :size="17" /><Minus v-else-if="task.status === 'skipped' || !task.selected" :size="16" /><LoaderCircle v-else-if="task.status === 'running' && executing" :size="17" class="spin" /><Circle v-else :size="15" /></span>
        <div class="task-content"><span class="task-index">任务 {{ task.id.toString().padStart(2, '0') }}</span><h3>{{ task.title }}</h3><div class="task-metrics"><span>{{ task.evidence_count }} 条证据</span><span>{{ task.citation_count }} 条引用</span></div>
          <div v-if="task.retrieval?.stage" class="task-metrics"><span>{{ task.retrieval.discovered ?? 0 }} 个候选网页</span><span>{{ task.retrieval.full_pages ?? 0 }} 页正文</span><span>{{ task.retrieval.chunks ?? 0 }} 个文本块</span><span v-if="task.retrieval.local_sufficient">复用已有资料</span></div>
          <details v-if="task.notices?.length" class="retrieval-notices"><summary>资料限制 · {{ task.notices.length }}</summary><p v-for="notice in task.notices" :key="notice">{{ notice }}</p></details>
        </div>
        <span :class="['task-state', task.status]">{{ task.selected ? taskLabels[task.status] : '本轮未执行' }}</span>
      </li>
    </ol>

    <details class="activity-log" open>
      <summary>进度更新 <span>本页观察记录</span></summary>
      <ol aria-live="polite" aria-relevant="additions" aria-label="进度更新记录"><li v-for="item in activities" :key="item.id"><time>{{ item.time }}</time><span>{{ item.text }}</span></li></ol>
      <p>每 2.5 秒核对已保存进度；实时消息与报告草稿显示在上方。</p>
    </details>
  </div>
</template>

<style scoped>
.retrieval-notices { margin-top: 8px; color: #64748b; font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.retrieval-notices summary { cursor: pointer; }
</style>
