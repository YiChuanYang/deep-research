<script setup lang="ts">
import { computed, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ArrowRight, ArrowUpRight, BookOpen, Check, ChevronDown, Compass, FileText, LoaderCircle, Search, ShieldCheck } from '@lucide/vue';
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
    <div class="page-heading"><span class="eyebrow">YOUR NEXT DISCOVERY</span><span class="edition">研究工作台 / 01</span></div>
    <section class="hero" aria-labelledby="new-title">
      <div class="hero-label"><span class="little-line"></span>让每个结论，都有据可循</div>
      <h1 id="new-title">好的研究，<br />始于一个<span class="serif-accent">好问题。</span></h1>
      <p class="hero-description">从问题出发，规划、检索、归纳。<br class="mobile-break" />把分散的信息，整理成一份可追溯的研究报告。</p>
      <span class="hero-index" aria-hidden="true">[ R ]</span>
    </section>

    <form class="research-form" @submit.prevent="submit">
      <div class="form-heading"><label for="research-topic"><Compass :size="18" />你想研究什么？</label><span>RESEARCH QUESTION</span></div>
      <textarea id="research-topic" v-model="topic" name="topic" rows="4" maxlength="1000" placeholder="描述你的研究主题、关注方向或想解决的问题……" :disabled="busy" :aria-invalid="Boolean(error)" aria-describedby="topic-hint" required></textarea>
      <div class="textarea-footer"><span id="topic-hint">具体的问题，通常能带来更聚焦的研究。</span><span :class="{ 'text-error': count > 500 }">{{ count }} / 500</span></div>
      <div class="form-controls">
        <div class="task-limit"><label for="task-limit">研究任务</label><div class="select-wrap"><select id="task-limit" v-model.number="maxTasks" :disabled="busy"><option v-for="n in 20" :key="n" :value="n">最多 {{ n }} 项</option></select><ChevronDown :size="14" /></div><span class="control-hint">按需拆解，逐项研究</span></div>
        <button class="button primary start-button" type="submit" :disabled="busy || uncertain || Boolean(knownRunId) || count > 500"><LoaderCircle v-if="busy" class="spin" :size="17" /><span>{{ busy ? '正在提交' : '开始研究' }}</span><ArrowRight v-if="!busy" :size="17" /></button>
      </div>
      <div v-if="error" class="inline-error" role="alert"><p>{{ error }}</p><template v-if="uncertain"><p>后端可能已受理，请先刷新左侧历史，确认是否已经创建研究。</p><button type="button" class="text-button" @click="uncertain = false">已检查历史，允许重新提交</button></template><RouterLink v-if="knownRunId" :to="{ name: 'research', params: { runId: knownRunId } }">查看已保留的研究</RouterLink></div>
    </form>
    <div class="under-form"><ShieldCheck :size="14" /><span>后台持续执行</span><span class="dot-separator">·</span><span>支持中断恢复</span><span class="dot-separator">·</span><span>附带来源与证据</span></div>

    <section class="suggestions" aria-labelledby="suggestions-title">
      <div class="section-heading"><h2 id="suggestions-title">从一个方向开始</h2><span>或者，试试这些问题</span></div>
      <div class="suggestion-grid"><button v-for="suggestion in suggestions" :key="suggestion.category" type="button" class="suggestion" :disabled="busy" @click="topic = suggestion.prompt"><span class="suggestion-category">{{ suggestion.category }}<ArrowUpRight :size="16" /></span><span class="suggestion-title">{{ suggestion.title }}</span></button></div>
    </section>

    <section class="method-strip" aria-label="研究流程">
      <div><Compass :size="17" /><span><b>01</b> 问题拆解</span></div><ArrowRight :size="13" class="method-arrow" />
      <div><Search :size="17" /><span><b>02</b> 资料检索</span></div><ArrowRight :size="13" class="method-arrow" />
      <div><BookOpen :size="17" /><span><b>03</b> 证据归纳</span></div><ArrowRight :size="13" class="method-arrow" />
      <div><FileText :size="17" /><span><b>04</b> 报告生成</span></div>
    </section>
    <footer class="page-footer"><span>DEEP RESEARCHER</span><span><Check :size="12" /> 以证据为基础，以理解为目的。</span></footer>
  </div>
</template>
