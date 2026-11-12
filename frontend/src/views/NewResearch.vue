<script setup lang="ts">
import { computed, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ArrowRight, ArrowUpRight, ChevronDown, LoaderCircle } from '@lucide/vue';
import { ApiError, createResearch, errorText } from '../api';
import { refreshHistory } from '../composables/workspace';

const router = useRouter();
const topic = ref('');
const maxTasks = ref(3);
const busy = ref(false);
const error = ref('');
const uncertain = ref(false);
const knownRunId = ref<string | null>(null);
const count = computed(() => Array.from(topic.value).length);
const suggestions = [
  { category: '人工智能', title: '检索增强生成的技术演进与应用边界', prompt: '检索增强生成（RAG）的技术演进、主要方法、实际应用与当前局限' },
  { category: '遥感科学', title: '高光谱图像分类的研究现状', prompt: '高光谱图像分类的研究现状：主要方法、公开数据集与当前挑战' },
  { category: '智能系统', title: '多智能体协作如何落地？', prompt: '大模型多智能体协作的主要架构、适用场景与工程实践中的挑战' },
];

async function submit(): Promise<void> {
  if (busy.value || uncertain.value || knownRunId.value) return;
  error.value = '';
  const value = topic.value.trim();
  if (Array.from(value).length < 2 || Array.from(value).length > 500) { error.value = '请填写 2–500 个字符的研究主题。'; return; }
  busy.value = true;
  try {
    const accepted = await createResearch(value, maxTasks.value);
    refreshHistory();
    await router.push({ name: 'research', params: { runId: accepted.run_id } });
  } catch (caught) {
    error.value = errorText(caught);
    if (caught instanceof ApiError) { uncertain.value = caught.status === 0; knownRunId.value = caught.runId; }
  } finally { busy.value = false; }
}
</script>

<template>
  <div class="new-page page-enter">
    <section class="hero" aria-labelledby="new-title">
      <h1 id="new-title">开始一项研究</h1>
      <p class="hero-description">输入主题，生成附带证据的研究报告。</p>
    </section>

    <form class="research-form" @submit.prevent="submit">
      <div class="form-heading"><label for="research-topic">你想研究什么？</label></div>
      <textarea id="research-topic" v-model="topic" name="topic" rows="4" maxlength="1000" placeholder="描述你的研究主题、关注方向或想解决的问题……" :disabled="busy" :aria-invalid="Boolean(error)" aria-describedby="topic-hint" required></textarea>
      <div class="textarea-footer"><span id="topic-hint">2–500 字</span><span :class="{ 'text-error': count > 500 }">{{ count }} / 500</span></div>
      <div class="form-controls">
        <div class="task-limit"><label for="task-limit">任务上限</label><div class="select-wrap"><select id="task-limit" v-model.number="maxTasks" :disabled="busy"><option v-for="n in 20" :key="n" :value="n">{{ n }} 项</option></select><ChevronDown :size="14" /></div></div>
        <button class="button primary start-button" type="submit" :disabled="busy || uncertain || Boolean(knownRunId) || count > 500"><LoaderCircle v-if="busy" class="spin" :size="17" /><span>{{ busy ? '正在提交' : '开始研究' }}</span><ArrowRight v-if="!busy" :size="17" /></button>
      </div>
      <div v-if="error" class="inline-error" role="alert"><p>{{ error }}</p><template v-if="uncertain"><p>后端可能已受理，请先刷新左侧历史，确认是否已经创建研究。</p><button type="button" class="text-button" @click="uncertain = false">已检查历史，允许重新提交</button></template><RouterLink v-if="knownRunId" :to="{ name: 'research', params: { runId: knownRunId } }">查看已保留的研究</RouterLink></div>
    </form>

    <section class="suggestions" aria-labelledby="suggestions-title">
      <div class="section-heading"><h2 id="suggestions-title">示例主题</h2></div>
      <div class="suggestion-grid"><button v-for="suggestion in suggestions" :key="suggestion.category" type="button" class="suggestion" :disabled="busy" @click="topic = suggestion.prompt"><span class="suggestion-category">{{ suggestion.category }}<ArrowUpRight :size="16" /></span><span class="suggestion-title">{{ suggestion.title }}</span></button></div>
    </section>

  </div>
</template>
