<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { BookOpen, ChevronLeft, ChevronRight, Layers3, Menu, PanelLeftClose, PanelLeftOpen, Plus, RefreshCw, Trash2, X } from '@lucide/vue';
import BrandMark from './components/BrandMark.vue';
import { ApiError, deleteResearch, errorText, getHealth, listRuns } from './api';
import { formatDate, runLabels } from './display';
import { historyRevision } from './composables/workspace';
import type { RunItem, RunList } from './types';

const route = useRoute();
const router = useRouter();
const history = ref<RunList | null>(null);
const offset = ref(0);
const loading = ref(false);
const error = ref('');
const connected = ref<boolean | null>(null);
const menuOpen = ref(false);
const collapsed = ref(false);
const deleteDialog = ref<HTMLDialogElement | null>(null);
const deleteTarget = ref<RunItem | null>(null);
const deleting = ref(false);
const deleteError = ref('');
const historyNotice = ref('');
const pageNumber = computed(() => offset.value / 10 + 1);
let controller: AbortController | undefined;
let timer: number | undefined;
let disposed = false;

try { collapsed.value = localStorage.getItem('research.sidebar.collapsed') === 'true'; } catch { /* Storage can be unavailable. */ }
watch(collapsed, value => {
  try { localStorage.setItem('research.sidebar.collapsed', String(value)); } catch { /* The toggle still works without storage. */ }
});

async function reload(): Promise<void> {
  if (disposed || document.hidden || deleteTarget.value) return;
  window.clearTimeout(timer);
  controller?.abort();
  const current = new AbortController();
  controller = current;
  loading.value = true;
  const [runs, health] = await Promise.allSettled([listRuns(offset.value, current.signal), getHealth(current.signal)]);
  if (disposed || current.signal.aborted) return;
  connected.value = health.status === 'fulfilled';
  if (runs.status === 'fulfilled') {
    if (!runs.value.items.length && offset.value > 0) {
      loading.value = false;
      error.value = '';
      offset.value = Math.max(0, offset.value - 10);
      return;
    }
    history.value = runs.value;
    error.value = '';
  } else error.value = errorText(runs.reason);
  loading.value = false;
  timer = window.setTimeout(() => { void reload(); }, 15_000);
}

function requestDelete(item: RunItem): void {
  if (deleting.value || deleteTarget.value) return;
  window.clearTimeout(timer);
  controller?.abort();
  loading.value = false;
  deleteTarget.value = { ...item };
  deleteError.value = '';
  historyNotice.value = '';
  deleteDialog.value?.showModal();
}

function closeDelete(): void {
  if (deleting.value) return;
  deleteDialog.value?.close();
  deleteTarget.value = null;
  deleteError.value = '';
  void reload();
}

async function confirmDelete(): Promise<void> {
  const target = deleteTarget.value;
  if (!target || deleting.value) return;
  deleting.value = true;
  deleteError.value = '';
  try {
    await deleteResearch(target.run_id);
  } catch (caught) {
    deleteError.value = caught instanceof ApiError && caught.status === 409
      ? errorText(caught)
      : errorText(caught) + '。可重试删除此研究，请求不会自动重发。';
    return;
  } finally { deleting.value = false; }
  if (disposed) return;
  controller?.abort();
  deleteDialog.value?.close();
  deleteTarget.value = null;
  historyNotice.value = '研究已删除';
  const page = history.value;
  if (page) {
    const items = page.items.filter(item => item.run_id !== target.run_id);
    history.value = { ...page, items };
    if (!items.length && offset.value > 0) offset.value = Math.max(0, offset.value - 10);
  }
  if (route.params.runId === target.run_id) await router.replace('/');
  await reload();
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
  <div class="workspace" :class="{ 'sidebar-collapsed': collapsed }" @keydown.esc="menuOpen = false">
    <div class="mobile-topbar">
      <RouterLink class="brand" to="/"><BrandMark /><span>深研</span></RouterLink>
      <button class="icon-button" :aria-expanded="menuOpen" aria-controls="workspace-sidebar" aria-label="切换研究历史" @click="menuOpen = !menuOpen"><X v-if="menuOpen" :size="21" /><Menu v-else :size="21" /></button>
    </div>
    <aside id="workspace-sidebar" class="sidebar" :class="{ open: menuOpen }">
      <div class="sidebar-brand-row">
        <RouterLink class="brand" to="/"><BrandMark /><span>深研</span></RouterLink>
        <button class="icon-button desktop-collapse" aria-label="收起研究历史" @click="collapsed = true"><PanelLeftClose :size="18" /></button>
      </div>
      <RouterLink to="/" class="new-research-link"><Plus :size="17" /><span>新建研究</span></RouterLink>
      <div class="history-heading"><span>研究记录</span><button class="icon-button small" aria-label="刷新研究历史" :disabled="loading" @click="reload"><RefreshCw :size="14" :class="{ spin: loading }" /></button></div>
      <p v-if="error" class="sidebar-error" role="status">读取失败 <button class="text-button" @click="reload">重试</button></p>
      <div v-if="loading && !history" class="history-skeleton" aria-label="正在读取历史"><span></span><span></span><span></span></div>
      <nav v-else-if="history?.items.length" class="history-list" aria-label="历史研究">
        <div v-for="item in history.items" :key="item.run_id" class="history-row">
          <RouterLink :to="{ name: 'research', params: { runId: item.run_id } }" class="history-item" :class="{ selected: route.params.runId === item.run_id }" :title="item.topic">
            <div class="history-title"><BookOpen :size="15" /><span>{{ item.topic }}</span></div>
            <div class="history-meta"><span :class="['status-dot', item.status]"></span><span>{{ runLabels[item.status] }}</span><time :title="formatDate(item.updated_at, true)">{{ formatDate(item.updated_at) }}</time></div>
          </RouterLink>
          <button v-if="route.params.runId === item.run_id" class="icon-button small history-delete" :aria-label="'删除研究：' + item.topic" title="删除研究" :disabled="deleting" @click="requestDelete(item)"><Trash2 :size="15" /></button>
        </div>
      </nav>
      <div v-else-if="!error" class="history-empty"><Layers3 :size="23" /><p>暂无研究记录</p></div>
      <div v-if="history && (history.items.length || offset > 0)" class="history-pagination"><button class="icon-button small" aria-label="上一页研究" :disabled="offset === 0 || loading" @click="offset = Math.max(0, offset - 10)"><ChevronLeft :size="16" /></button><span>{{ pageNumber }}</span><button class="icon-button small" aria-label="下一页研究" :disabled="!history.has_more || loading" @click="offset += 10"><ChevronRight :size="16" /></button></div>
    </aside>
    <div class="main-shell">
      <header class="topbar">
        <div class="topbar-start"><button class="icon-button sidebar-toggle" :aria-label="collapsed ? '展开侧栏' : '折叠侧栏'" :aria-expanded="!collapsed" aria-controls="workspace-sidebar" @click="collapsed = !collapsed"><PanelLeftOpen v-if="collapsed" :size="19" /><PanelLeftClose v-else :size="19" /></button><span>{{ route.name === 'research' ? '研究详情' : '新建研究' }}</span></div>
        <div class="connection" :class="{ offline: connected === false }"><span class="connection-dot"></span>{{ connected === null ? '连接中' : connected ? 'API 已连接' : 'API 未连接' }}</div>
      </header>
      <p v-if="historyNotice" class="workspace-notice" role="status">{{ historyNotice }}<button class="icon-button small" aria-label="关闭删除提示" @click="historyNotice = ''"><X :size="14" /></button></p>
      <main id="main-content"><RouterView v-slot="{ Component }"><component :is="Component" :key="route.fullPath" /></RouterView></main>
    </div>
    <dialog ref="deleteDialog" class="confirm-dialog delete-dialog" aria-labelledby="delete-title" aria-describedby="delete-description" @cancel.prevent="closeDelete" @keydown.esc.stop>
      <div class="confirm-content"><Trash2 :size="25" /><h2 id="delete-title">删除这项研究？</h2><p class="delete-topic">{{ deleteTarget?.topic }}</p><p id="delete-description">研究记录、报告、保存进度和关联笔记将被删除，无法恢复。已下载文件和评测副本保留。</p><p v-if="deleteError" class="delete-error" role="alert">{{ deleteError }}</p><div class="actions"><button class="button" :disabled="deleting" autofocus @click="closeDelete">取消</button><button class="button danger" :disabled="deleting" @click="confirmDelete">{{ deleting ? '正在删除…' : '确认删除' }}</button></div></div>
    </dialog>
  </div>
</template>
