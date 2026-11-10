import { onBeforeUnmount, onMounted, ref } from 'vue';
import { ApiError, errorText, getProgress, getResult } from '../api';
import type { ResearchResult, RunProgress } from '../types';

/** Only reads snapshots. Writes are deliberate user actions and never auto-retried. */
export function useResearch(runId: string) {
  const progress = ref<RunProgress | null>(null);
  const result = ref<ResearchResult | null>(null);
  const loading = ref(false);
  const error = ref('');
  const notFound = ref(false);
  let disposed = false;
  let timer: number | undefined;
  let controller: AbortController | undefined;
  const clearTimer = () => { window.clearTimeout(timer); timer = undefined; };

  async function refresh(): Promise<void> {
    if (disposed || document.hidden) return;
    clearTimer();
    controller?.abort();
    const current = new AbortController();
    controller = current;
    loading.value = true;
    let again = false;
    let delay = 2500;
    try {
      if (!/^[0-9a-f]{32}$/.test(runId)) throw new ApiError(422, 'invalid_run_id', '研究编号格式不正确');
      const value = await getProgress(runId, current.signal);
      if (disposed || current.signal.aborted) return;
      progress.value = value;
      error.value = '';
      notFound.value = false;
      if (value.report_ready && !result.value) result.value = await getResult(runId, current.signal);
      again = !value.report_ready && (value.execution !== 'idle' || (value.status === 'running' && !value.requires_recovery_confirmation));
    } catch (caught) {
      if (disposed || current.signal.aborted) return;
      error.value = errorText(caught);
      notFound.value = caught instanceof ApiError && [404, 422].includes(caught.status);
      again = !notFound.value && (!(caught instanceof ApiError) || caught.status === 0 || caught.status >= 500 || caught.code === 'result_not_ready');
      delay = 5000;
    } finally {
      if (!disposed && controller === current) {
        loading.value = false;
        if (again && !document.hidden) timer = window.setTimeout(() => { void refresh(); }, delay);
      }
    }
  }
  function visibility(): void {
    if (document.hidden) { clearTimer(); controller?.abort(); loading.value = false; }
    else void refresh();
  }
  onMounted(() => { document.addEventListener('visibilitychange', visibility); void refresh(); });
  onBeforeUnmount(() => { disposed = true; clearTimer(); controller?.abort(); document.removeEventListener('visibilitychange', visibility); });
  return { progress, result, loading, error, notFound, refresh };
}
