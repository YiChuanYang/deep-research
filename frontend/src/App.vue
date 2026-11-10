<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { ArrowUpRight, BookOpen, ChevronLeft, ChevronRight, CircleHelp, FlaskConical, Layers3, Menu, Plus, RefreshCw, X } from '@lucide/vue';
import BrandMark from './components/BrandMark.vue';
import { errorText, getHealth, listRuns } from './api';
import { formatDate, runLabels, shortId } from './display';
import { historyRevision } from './composables/workspace';
import type { RunList } from './types';

const route = useRoute();
const history = ref<RunList | null>(null);
const offset = ref(0);
const loading = ref(false);
const error = ref('');
const connected = ref<boolean | null>(null);
const menuOpen = ref(false);
const helpOpen = ref(false);
const pageNumber = computed(() => offset.value / 10 + 1);
let controller: AbortController | undefined;
let timer: number | undefined;
let disposed = false;

async function reload(): Promise<void> {
  if (disposed || document.hidden) return;
  window.clearTimeout(timer);
  controller?.abort();
  const current = new AbortController(); controller = current; loading.value = true;
  const [runs, health] = await Promise.allSettled([listRuns(offset.value, current.signal), getHealth(current.signal)]);
  if (disposed || current.signal.aborted) return;
  connected.value = health.status === 'fulfilled';
  if (runs.status === 'fulfilled') { history.value = runs.value; error.value = ''; }
  else error.value = errorText(runs.reason);
  loading.value = false;
  timer = window.setTimeout(() => { void reload(); }, 15_000);
}
function visibility(): void {
  if (document.hidden) { window.clearTimeout(timer); controller?.abort(); loading.value = false; }
  else void reload();
}
watch(offset, () => { history.value = null; void reload(); });
watch(historyRevision, () => { void reload(); });
watch(() => route.fullPath, () => { menuOpen.value = false; void reload(); });
onMounted(() => { document.addEventListener('visibilitychange', visibility); void reload(); });
onBeforeUnmount(() => { disposed = true; window.clearTimeout(timer); controller?.abort(); document.removeEventListener('visibilitychange', visibility); });
</script>

<template>
  <div class="workspace" @keydown.esc="menuOpen = false; helpOpen = false">
    <div class="mobile-topbar"><RouterLink class="brand" to="/"><BrandMark /><span>深研<span class="brand-en">DEEP RESEARCHER</span></span></RouterLink><button class="icon-button" :aria-expanded="menuOpen" aria-controls="workspace-sidebar" aria-label="切换研究历史" @click="menuOpen = !menuOpen"><X v-if="menuOpen" :size="21" /><Menu v-else :size="21" /></button></div>
    <aside id="workspace-sidebar" class="sidebar" :class="{ open: menuOpen }">
      <RouterLink class="brand desktop-brand" to="/"><BrandMark /><span>深研<span class="brand-en">DEEP RESEARCHER</span></span></RouterLink>
      <div class="workspace-label"><FlaskConical :size="14" /><span>个人研究空间</span><span class="workspace-dot"></span></div>
      <RouterLink to="/" class="new-research-link"><Plus :size="17" /><span>新建研究</span><span class="shortcut">＋</span></RouterLink>
      <div class="history-heading"><span>研究记录</span><button class="icon-button small" aria-label="刷新研究历史" :disabled="loading" @click="reload"><RefreshCw :size="13" :class="{ spin: loading }" /></button></div>
      <p v-if="error" class="sidebar-error" role="status">暂时无法读取记录。<button class="text-button" @click="reload">重试</button></p>
      <div v-if="loading && !history" class="history-skeleton" aria-label="正在读取历史"><span></span><span></span><span></span></div>
      <nav v-else-if="history?.items.length" class="history-list" aria-label="历史研究">
        <RouterLink v-for="item in history.items" :key="item.run_id" :to="{ name: 'research', params: { runId: item.run_id } }" class="history-item" :class="{ selected: route.params.runId === item.run_id }" :title="item.topic">
          <div class="history-title"><BookOpen :size="15" /><span>{{ item.topic }}</span></div>
          <div class="history-meta"><span :class="['status-dot', item.status]"></span><span>{{ runLabels[item.status] }}</span><time :title="formatDate(item.updated_at, true)">{{ formatDate(item.updated_at) }}</time></div>
          <span class="history-id">{{ shortId(item.run_id) }}</span>
        </RouterLink>
      </nav>
      <div v-else-if="!error" class="history-empty"><Layers3 :size="25" :stroke-width="1.2" /><p>研究，从这里积累</p><span>创建的研究会保留在这里</span></div>
      <div v-if="history && (history.items.length || offset > 0)" class="history-pagination"><button class="icon-button small" aria-label="上一页研究" :disabled="offset === 0 || loading" @click="offset = Math.max(0, offset - 10)"><ChevronLeft :size="16" /></button><span>{{ pageNumber.toString().padStart(2, '0') }}</span><button class="icon-button small" aria-label="下一页研究" :disabled="!history.has_more || loading" @click="offset += 10"><ChevronRight :size="16" /></button></div>
      <div class="sidebar-bottom"><div class="sidebar-note"><span class="note-cross">＋</span><p>把问题交给探索，<br />把判断留给自己。</p></div><button class="help-button" :aria-expanded="helpOpen" @click="helpOpen = !helpOpen"><CircleHelp :size="15" /><span>使用说明</span><ArrowUpRight :size="14" /></button><div v-if="helpOpen" class="help-copy">填写主题后，研究会在后台执行。刷新或关闭页面不会取消研究。你可以通过历史记录继续查看、恢复或下载结果。</div><span class="version-label">DEEP RESEARCH / v0.1</span></div>
    </aside>
    <div class="main-shell">
      <header class="topbar"><div class="breadcrumb"><span>工作空间</span><span class="breadcrumb-slash">/</span><strong>{{ route.name === 'research' ? '研究详情' : '新建研究' }}</strong></div><div class="connection" :class="{ offline: connected === false }"><span class="connection-dot"></span>{{ connected === null ? '连接中' : connected ? 'API 已连接' : 'API 未连接' }}</div></header>
      <main id="main-content"><RouterView v-slot="{ Component }"><component :is="Component" :key="route.fullPath" /></RouterView></main>
    </div>
  </div>
</template>
