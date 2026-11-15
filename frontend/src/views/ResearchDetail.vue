<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { ArrowDownToLine, ArrowLeft, ArrowRight, ArrowUpRight, BookOpen, Check, Copy, FileText, Link2, LoaderCircle, RefreshCw, RotateCcw, TriangleAlert, X } from '@lucide/vue';
import { useResearch } from '../composables/useResearch';
import { refreshHistory } from '../composables/workspace';
import { errorText, getReport, resumeResearch } from '../api';
import { hostname, progressLabel, shortId } from '../display';
import type { Citation } from '../types';
import ReportContent from '../components/ReportContent.vue';
import EvidenceDrawer from '../components/EvidenceDrawer.vue';
import ResearchProgress from '../components/ResearchProgress.vue';
import StreamingReport from '../components/StreamingReport.vue';
import ReportIterationPanel from '../components/ReportIterationPanel.vue';

const props = defineProps<{ runId: string }>();
const { progress, result, loading, error, notFound, lastUpdated, refresh, draft, phase, connection, currentNode } = useResearch(props.runId);
const tab = ref('progress');
const resuming = ref(false);
const downloading = ref(false);
const actionError = ref('');
const copied = ref('');
const selectedEvidence = ref<Citation | null>(null);
const confirmDialog = ref<HTMLDialogElement | null>(null);
const canResume = computed(() => Boolean(progress.value && !error.value && !progress.value.report_ready && progress.value.execution === 'idle' && progress.value.report_iteration?.status !== 'blocked' && progress.value.status !== 'completed'));
const phases = [{ name: 'progress', title: '研究进度', icon: BookOpen }, { name: 'report', title: '研究报告', icon: FileText }, { name: 'sources', title: '引用证据', icon: Link2 }];

watch(result, value => { if (value) { tab.value = 'report'; refreshHistory(); } });
watch(() => progress.value?.status, () => { refreshHistory(); });

function openEvidence(id: string): void {
  selectedEvidence.value = result.value?.citations.find(item => item.evidence_id === id) ?? null;
}
async function copy(value: string, name: string): Promise<void> {
  try { await navigator.clipboard.writeText(value); copied.value = name; }
  catch { actionError.value = '浏览器未允许访问剪贴板，请手动选择文本复制。'; }
}
async function requestResume(): Promise<void> {
  if (!canResume.value || resuming.value) return;
  if (progress.value?.requires_recovery_confirmation) { confirmDialog.value?.showModal(); return; }
  await resume(false);
}
async function resume(confirmed: boolean): Promise<void> {
  if (resuming.value) return;
  confirmDialog.value?.close(); resuming.value = true; actionError.value = '';
  try { await resumeResearch(props.runId, confirmed); refreshHistory(); await refresh(); }
  catch (caught) { actionError.value = `${errorText(caught)}。请先刷新进度确认状态，请求不会自动重发。`; }
  finally { resuming.value = false; }
}
async function download(): Promise<void> {
  if (downloading.value) return;
  downloading.value = true; actionError.value = '';
  try {
    const blob = await getReport(props.runId);
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a'); link.href = url; link.download = `research-${props.runId}.md`;
    document.body.append(link); link.click(); link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (caught) { actionError.value = errorText(caught); }
  finally { downloading.value = false; }
}
async function changeTab(name: string): Promise<void> {
  if (name !== 'progress' && !result.value) return;
  tab.value = name;
  await nextTick();
}
function tabKeys(event: KeyboardEvent): void {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const available = phases.filter(item => item.name === 'progress' || result.value);
  const index = available.findIndex(item => item.name === tab.value);
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? available.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + available.length) % available.length;
  const name = available[next]?.name;
  if (name) { void changeTab(name); document.getElementById(`tab-${name}`)?.focus(); }
}
</script>

<template>
  <div class="detail-page page-enter">
    <div class="detail-top"><RouterLink class="back-link" to="/"><ArrowLeft :size="15" />工作台</RouterLink><span class="eyebrow">{{ shortId(runId) }}</span></div>
    <div v-if="error" class="alert warning" role="status"><TriangleAlert :size="18" /><div><strong>{{ error }}</strong><p>{{ notFound ? '请从历史记录中选择一项有效研究。' : '保留上次读取的内容。连接异常不代表研究执行失败。' }}</p></div><button class="text-button" :disabled="loading" @click="refresh">重试</button></div>
    <div v-if="actionError" class="alert warning" role="alert"><TriangleAlert :size="18" /><p>{{ actionError }}</p><button class="icon-button small" aria-label="关闭提示" @click="actionError = ''"><X :size="16" /></button></div>
    <template v-if="progress">
      <header class="research-heading"><div class="research-kicker"><span :class="['status-badge', progress.report_ready ? 'completed' : progress.requires_recovery_confirmation ? 'failed' : progress.status]"><span class="status-dot"></span>{{ progressLabel(progress) }}</span></div><h1>{{ progress.topic }}</h1><div class="detail-meta"><span>已规划 {{ progress.planned_tasks }} 项 · 本轮执行 {{ progress.selected_tasks }} 项</span><button class="text-button quiet" @click="copy(runId, 'id')"><Check v-if="copied === 'id'" :size="13" /><Copy v-else :size="13" />{{ copied === 'id' ? '编号已复制' : '复制研究编号' }}</button></div></header>
      <div v-if="progress.execution === 'idle' && !progress.report_ready && (progress.status === 'failed' || progress.requires_recovery_confirmation || progress.status === 'pending')" class="recovery-banner"><RotateCcw :size="20" /><div><strong>{{ progress.requires_recovery_confirmation ? '这项研究等待恢复' : progress.status === 'pending' ? '这项研究尚未开始' : '研究暂时停在这里' }}</strong><p>{{ progress.error_message || '已保存的研究进度仍然保留，你可以从原来的位置继续。' }}</p></div><button class="button primary" :disabled="!canResume || resuming" @click="requestResume"><LoaderCircle v-if="resuming" class="spin" :size="15" />继续研究<ArrowRight :size="15" /></button></div>
      <ReportIterationPanel v-if="progress.report_iteration" :run-id="runId" :progress="progress.report_iteration" />
      <div class="research-tabs" role="tablist" aria-label="研究详情" @keydown="tabKeys"><button v-for="phase in phases" :id="`tab-${phase.name}`" :key="phase.name" role="tab" :aria-controls="`panel-${phase.name}`" :aria-selected="tab === phase.name" :tabindex="tab === phase.name ? 0 : -1" :disabled="phase.name !== 'progress' && !result" :class="{ active: tab === phase.name }" @click="changeTab(phase.name)"><component :is="phase.icon" :size="16" />{{ phase.title }}<span v-if="phase.name === 'sources' && result" class="tab-count">{{ result.citations.length }}</span></button><button class="refresh-detail" aria-label="刷新研究进度" :disabled="loading" @click="refresh"><RefreshCw :size="15" :class="{ spin: loading }" /></button></div>

      <section v-show="tab === 'progress'" id="panel-progress" role="tabpanel" aria-labelledby="tab-progress" class="progress-panel">
        <StreamingReport v-if="!result" :draft="draft" :phase="phase" :connection="connection" :current-node="currentNode" :interrupted="progress.execution === 'idle' && (progress.status === 'failed' || progress.requires_recovery_confirmation)" />
        <ResearchProgress :progress="progress" :loading="loading" :error="error" :last-updated="lastUpdated" />
      </section>

      <section v-if="tab === 'report' && result" id="panel-report" role="tabpanel" aria-labelledby="tab-report" class="report-panel">
        <div class="report-toolbar"><span>{{ result.citations.length }} 条引用证据</span><div class="actions"><button class="button subtle" @click="copy(result.report_markdown, 'report')"><Check v-if="copied === 'report'" :size="14" /><Copy v-else :size="14" />{{ copied === 'report' ? '已复制' : '复制报告' }}</button><button class="button primary" :disabled="downloading" @click="download"><LoaderCircle v-if="downloading" class="spin" :size="15" /><ArrowDownToLine v-else :size="15" />下载报告</button></div></div>
        <div class="report-paper"><ReportContent :markdown="result.report_markdown" :citations="result.citations" @select="openEvidence" /></div>
        <details v-if="result.tasks.length" class="task-appendix"><summary>查看各任务摘要 <span>{{ result.tasks.length }} 项</span></summary><div v-for="task in result.tasks" :key="task.id" class="appendix-item"><h3>{{ task.id }}. {{ task.title }}</h3><p>{{ task.summary || '本任务没有生成摘要。' }}</p></div></details>
      </section>

      <section v-if="tab === 'sources' && result" id="panel-sources" role="tabpanel" aria-labelledby="tab-sources" class="sources-panel"><div class="section-heading"><div><h2>引用证据</h2></div><span>报告实际引用的 {{ result.citations.length }} 条证据</span></div><div v-if="!result.citations.length" class="planning-empty"><Link2 :size="24" /><h3>本次报告没有引用证据</h3><p>资料不足的说明报告也可能没有引用，请结合报告正文判断。</p></div><button v-for="citation in result.citations" :key="citation.evidence_id" class="source-card" @click="openEvidence(citation.evidence_id)"><span class="evidence-code">{{ citation.evidence_id }}</span><div><h3>{{ citation.title }}</h3><p>{{ citation.text }}</p><span class="source-domain">{{ hostname(citation.url) }}</span></div><ArrowUpRight :size="18" /></button></section>
    </template>
    <div v-else-if="loading" class="detail-skeleton" aria-label="正在读取研究"><span></span><span></span><span></span></div>
    <div v-else class="planning-empty"><FileText :size="30" /><h2>暂时无法显示这项研究</h2><p>可以重新读取，或从左侧选择其他研究。</p><button class="button" @click="refresh">重新读取</button></div>
    <p v-if="copied" class="sr-only" role="status">已复制到剪贴板</p>
    <EvidenceDrawer :evidence="selectedEvidence" @close="selectedEvidence = null" />
    <dialog ref="confirmDialog" class="confirm-dialog" aria-labelledby="recovery-title"><div class="confirm-content"><TriangleAlert :size="25" /><h2 id="recovery-title">确认后继续研究</h2><p>上次执行未正常结束。请确认旧的执行进程已经停止，再从保存的进度继续。</p><div class="actions"><button class="button" @click="confirmDialog?.close()">暂不恢复</button><button class="button primary" :disabled="resuming" @click="resume(true)">确认已停止，继续</button></div></div></dialog>
  </div>
</template>
