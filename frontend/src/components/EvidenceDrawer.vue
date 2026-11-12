<script setup lang="ts">
import { nextTick, ref, watch } from 'vue';
import { ArrowUpRight, Quote, X } from '@lucide/vue';
import type { Citation } from '../types';
import { hostname, safeUrl } from '../display';

const props = defineProps<{ evidence: Citation | null }>();
const emit = defineEmits<{ close: [] }>();
const dialog = ref<HTMLDialogElement | null>(null);
watch(() => props.evidence, async value => {
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
    </div>
  </dialog>
</template>
