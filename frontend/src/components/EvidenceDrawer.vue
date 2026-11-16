<script setup lang="ts">
import { nextTick, ref, watch } from 'vue';
import { ArrowUpRight, Quote, X } from '@lucide/vue';
import type { Citation } from '../types';
import { hostname, safeUrl } from '../display';
import { errorText, getEvidenceSnapshot } from '../api';
import type { PageSnapshot } from '../types';

const props = defineProps<{ evidence: Citation | null; runId?: string }>();
const emit = defineEmits<{ close: [] }>();
const dialog = ref<HTMLDialogElement | null>(null);
const snapshot = ref<PageSnapshot | null>(null);
const snapshotError = ref('');
const snapshotLoading = ref(false);
let requestId = 0;
async function loadSnapshot(): Promise<void> {
  if (!props.runId || !props.evidence) return;
  const id = ++requestId;
  snapshotLoading.value = true;
  snapshotError.value = '';
  try {
    const value = await getEvidenceSnapshot(props.runId, props.evidence.evidence_id);
    if (id === requestId) snapshot.value = value;
  } catch (error) { if (id === requestId) snapshotError.value = errorText(error); }
  finally { if (id === requestId) snapshotLoading.value = false; }
}
watch(() => props.evidence, async value => {
  requestId++;
  snapshot.value = null;
  snapshotError.value = '';
  snapshotLoading.value = false;
  await nextTick();
  if (value && !dialog.value?.open) dialog.value?.showModal();
  if (!value && dialog.value?.open) dialog.value.close();
});
function backdrop(event: MouseEvent): void {
  if (event.target === dialog.value) emit('close');
}
</script>

<template>
  <dialog ref="dialog" class="evidence-drawer" aria-labelledby="evidence-title" @cancel.prevent="emit('close')" @close="emit('close')" @click="backdrop">
    <div v-if="evidence" class="drawer-inner">
      <div class="drawer-head"><span class="eyebrow">引用证据</span><button class="icon-button" aria-label="关闭证据" @click="emit('close')"><X :size="20" /></button></div>
      <span class="evidence-code">{{ evidence.evidence_id }}</span>
      <h2 id="evidence-title">{{ evidence.title }}</h2>
      <a v-if="safeUrl(evidence.url)" class="source-link" :href="safeUrl(evidence.url) ?? undefined" target="_blank" rel="noopener noreferrer">{{ hostname(evidence.url) }}<ArrowUpRight :size="16" /></a>
      <div class="drawer-divider"></div>
      <div class="section-caption"><Quote :size="16" /><span>研究中使用的原始片段</span></div>
      <blockquote class="evidence-body">{{ evidence.text }}</blockquote>
      <p v-if="evidence.section" class="snapshot-meta">{{ evidence.section }}</p>
      <p v-if="evidence.retrieved_at" class="snapshot-meta">取得时间：{{ new Date(evidence.retrieved_at).toLocaleString('zh-CN') }} · {{ evidence.kind === 'page_excerpt' ? '网页正文片段' : '搜索片段' }}<span v-if="evidence.truncated"> · 来源已截断</span></p>
      <button v-if="evidence.snapshot_id && runId && !snapshot" class="text-button" :disabled="snapshotLoading" @click="loadSnapshot">{{ snapshotLoading ? '读取快照…' : '查看当时的网页正文' }}</button>
      <p v-if="snapshotError" role="alert">{{ snapshotError }}</p>
      <details v-if="snapshot" open class="snapshot-content"><summary>保存的正文 · 引用位置已标记</summary><pre>{{ snapshot.text.slice(0, snapshot.start ?? 0) }}<mark>{{ snapshot.text.slice(snapshot.start ?? 0, snapshot.end ?? 0) }}</mark>{{ snapshot.text.slice(snapshot.end ?? 0) }}</pre></details>
    </div>
  </dialog>
</template>

<style scoped>
.snapshot-meta { color: #64748b; font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.snapshot-content { margin-top: 20px; font-size: 13px; }
.snapshot-content pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 55vh; overflow: auto; font: inherit; line-height: 1.8; padding: 12px; background: #f8fafc; }
.snapshot-content mark { background: #dbeafe; color: #174d8a; }
</style>
