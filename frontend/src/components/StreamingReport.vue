<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { Radio } from '@lucide/vue';
import { nodeLabel } from '../display';

const props = defineProps<{
  draft: string;
  phase: string;
  connection: string;
  currentNode: string;
  interrupted: boolean;
}>();
const body = ref<HTMLElement | null>(null);
const title = computed(() => {
  if (props.interrupted || props.phase === 'failed') return '生成已暂停 · 草稿未定稿';
  if (props.phase === 'validating') return '正在校验引用';
  if (props.phase === 'generated') return '正在保存报告';
  return '报告生成中';
});
const status = computed(() => {
  if (props.connection === 'reconnecting') return '实时连接重连中 · 进度查询继续';
  if (props.connection === 'connecting') return '正在连接实时进度';
  if (props.connection === 'live') return props.currentNode ? '正在执行：' + nodeLabel(props.currentNode) : '实时连接已建立';
  return '';
});
watch(() => props.draft, async () => {
  const element = body.value;
  // 用户向上阅读时不强制拉回底部；停留底部时跟随新增文字。
  const follow = !element || element.scrollHeight - element.scrollTop - element.clientHeight < 60;
  await nextTick();
  if (follow && body.value) body.value.scrollTop = body.value.scrollHeight;
});
</script>

<template>
  <div class="stream-panel">
    <p v-if="status" class="stream-status" role="status"><Radio :size="14" />{{ status }}</p>
    <section v-if="draft || ['streaming', 'validating', 'generated'].includes(phase)" class="stream-draft" aria-label="报告草稿">
      <header><strong>{{ title }}</strong><span>{{ draft.length.toLocaleString() }} 字符</span></header>
      <!-- 用插值渲染纯文本，未完成的 Markdown/HTML 不作为最终报告渲染。 -->
      <pre ref="body" class="stream-body">{{ draft || '等待正文…' }}</pre>
      <p>生成中的预览；完成后以校验后的正式报告为准。</p>
    </section>
  </div>
</template>
