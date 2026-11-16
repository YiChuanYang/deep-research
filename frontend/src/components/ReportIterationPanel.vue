<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { ReportIterationDetail, ReportIterationProgress } from '../types';
import { getIterations } from '../api';

const props = defineProps<{ runId: string; progress: ReportIterationProgress }>();
const detail = ref<ReportIterationDetail | null>(null);
const error = ref('');
const opened = ref(false);
const labels: Record<string, string> = {
  researching: '初始研究', reviewing: '正在核对报告', improving: '正在补充与修订',
  accepted: '模型复核完成', limited: '有限交付', unreviewed: '语义复核未完成', blocked: '草稿未通过交付检查',
};
const title = computed(() => labels[props.progress.status] ?? props.progress.status);
let requestId = 0;
async function load(): Promise<void> {
  const id = ++requestId;
  try {
    const value = await getIterations(props.runId);
    if (id === requestId) { detail.value = value; error.value = ''; }
  } catch {
    if (id === requestId) error.value = '暂时无法读取迭代记录，请重试。';
  }
}
watch(() => [props.runId, props.progress.draft_count, props.progress.status, props.progress.revision].join(':'), () => {
  // 只有展开记录时才获取正文，常规状态轮询不反复传输整份报告。
  if (opened.value) void load();
});
function toggle(event: Event): void {
  opened.value = (event.target as HTMLDetailsElement).open;
  if (opened.value) void load();
}
</script>

<template>
  <section class="iteration-panel" aria-label="报告迭代">
    <div class="iteration-heading"><strong>{{ title }}</strong><span>第 {{ progress.revision + 1 }} 版 · 最多 {{ progress.max_revisions }} 次改进</span></div>
    <p>初始任务已处理 {{ progress.initial_completed }} / {{ progress.initial_total }} · 补充任务已处理 {{ progress.supplemental_completed }} / {{ progress.supplemental_total }}</p>
    <p v-if="progress.issue_count !== null && progress.issue_count > 0">当前评审提出 {{ progress.issue_count }} 项待处理问题</p>
    <p v-if="progress.stop_reason" class="iteration-reason">{{ progress.stop_reason }}</p>
    <details @toggle="toggle"><summary>查看草稿与补充记录</summary>
      <p v-if="error" role="status">{{ error }} <button class="text-button" @click="load">重试</button></p>
      <template v-if="detail?.iteration">
        <p v-if="!detail.iteration.drafts.length">尚未生成报告草稿。</p>
        <article v-for="draft in detail.iteration.drafts" :key="draft.revision" class="iteration-draft">
          <h3>第 {{ draft.revision + 1 }} 版草稿</h3>
          <p v-if="draft.review_reused_from != null">正文无实质变化，沿用第 {{ draft.review_reused_from + 1 }} 版反馈，未再次评审。</p>
          <p v-if="draft.review_error">未完成复核：{{ draft.review_error }}</p>
          <p v-else-if="draft.review && !draft.review.issues.length">本次模型评审未提出问题。</p>
          <ul v-if="draft.review?.issues.length"><li v-for="(issue, index) in draft.review.issues" :key="index">{{ issue.reason }}<span v-if="issue.question"> · 补充问题：{{ issue.question }}</span></li></ul>
          <details><summary>阅读本版正文</summary><pre>{{ draft.text }}</pre></details>
        </article>
        <article v-for="task in detail.iteration.supplements" :key="task.task_id" class="iteration-draft">
          <h3>补充任务 {{ task.task_id }} · 第 {{ task.revision + 1 }} 版</h3>
          <p>{{ task.question }}</p><p>查询：{{ task.query }}</p><p>取得 {{ task.novel_evidence_ids.length }} 条内容不同的新证据</p>
        </article>
      </template>
    </details>
  </section>
</template>

<style scoped>
.iteration-panel { margin: 18px 0; padding: 18px 22px; border: 1px solid #cbdff5; border-radius: 12px; background: #f6faff; color: #29496b; }
.iteration-heading { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.iteration-heading strong { color: #174d8a; }
.iteration-heading span, .iteration-panel p { font-size: 13px; line-height: 1.8; }
.iteration-panel summary { cursor: pointer; font-size: 13px; padding: 6px 0; }
.iteration-draft { border-top: 1px solid #dce8f4; margin-top: 12px; padding-top: 12px; overflow-wrap: anywhere; }
.iteration-draft h3 { font-size: 14px; }
.iteration-draft ul { font-size: 13px; line-height: 1.8; padding-left: 20px; }
.iteration-draft pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 420px; overflow-y: auto; font: inherit; font-size: 13px; line-height: 1.8; padding: 12px; background: white; }
.iteration-reason { font-weight: 600; }
</style>
