import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import type { Ref } from 'vue';
import type { RunProgress } from '../types';

type Phase = 'waiting' | 'streaming' | 'validating' | 'generated' | 'failed';
interface ResearchEvent {
  event_id: number;
  run_id: string;
  execution_id: string;
  type: string;
  data: { text?: string; generation_id?: string; node?: string; task_id?: number | null };
}

/** 事件只用于展示；研究是否完成始终由状态/结果接口决定。 */
export function useResearchStream(
  runId: string,
  progress: Ref<RunProgress | null>,
  refresh: () => Promise<void>,
) {
  const draft = ref('');
  const phase = ref<Phase>('waiting');
  const connection = ref<'closed' | 'connecting' | 'live' | 'reconnecting'>('closed');
  const currentNode = ref('');
  let source: EventSource | undefined;
  let retryTimer: number | undefined;
  let cursor = 0;
  let executionId = '';
  let generationId = '';
  let stopped = false;
  let disposed = false;
  // 同一个节点可以有多个任务在执行；不能因其中一个返回就清空整个阶段。
  const runningNodes = new Map<string, string>();

  function close(): void {
    window.clearTimeout(retryTimer);
    retryTimer = undefined;
    source?.close();
    source = undefined;
    connection.value = 'closed';
  }

  function connect(): void {
    const value = progress.value;
    if (disposed || document.hidden || source || !value || value.report_ready) return;
    if (stopped && value.execution === 'idle') return;
    stopped = false;
    window.clearTimeout(retryTimer);
    retryTimer = undefined;
    connection.value = 'connecting';

    // 每次重连都显式传递已处理游标，包含断线重连和页面重新可见。
    // 后端也支持标准 Last-Event-ID，供其他 SSE 客户端使用。
    const current = new EventSource(
      '/api/research/' + encodeURIComponent(runId) + '/events?after=' + cursor,
    );
    source = current;
    current.onopen = () => { if (source === current) connection.value = 'live'; };
    current.onerror = () => {
      if (source !== current) return;
      current.close();
      source = undefined;
      connection.value = 'reconnecting';
      // 使用应用保存的游标重连，避免代理/客户端丢失请求头后全量回放。
      // 保留草稿；隐藏页面或卸载组件会取消重连计时器。
      retryTimer = window.setTimeout(connect, 1500);
    };
    current.addEventListener('research.event', (message: MessageEvent<string>) => {
      if (source !== current) return;
      let event: ResearchEvent;
      try { event = JSON.parse(message.data) as ResearchEvent; }
      catch { return; }
      if (!event || !event.data || event.run_id !== runId || !Number.isSafeInteger(event.event_id) || event.event_id <= cursor) return;
      cursor = event.event_id; // 去重：断线补发不能重复追加文字。
      if (event.execution_id !== executionId) {
        executionId = event.execution_id;
        generationId = '';
        draft.value = '';
        phase.value = 'waiting';
        currentNode.value = '';
        runningNodes.clear();
      }
      if (event.type === 'report.started') {
        generationId = event.data.generation_id ?? '';
        draft.value = '';
        phase.value = 'streaming';
      } else if (event.type === 'report.delta' && event.data.generation_id === generationId && generationId) {
        if (typeof event.data.text === 'string') draft.value += event.data.text;
      } else if (event.type === 'report.validating' && event.data.generation_id === generationId) {
        phase.value = 'validating';
      } else if (event.type === 'report.generated' && event.data.generation_id === generationId) {
        phase.value = 'generated';
      } else if (event.type === 'node.started') {
        runningNodes.set(`${event.data.node}:${event.data.task_id ?? ''}`, event.data.node ?? '');
        currentNode.value = event.data.node ?? '';
      } else if (event.type === 'node.returned' || event.type === 'node.failed') {
        runningNodes.delete(`${event.data.node}:${event.data.task_id ?? ''}`);
        currentNode.value = Array.from(runningNodes.values()).pop() ?? '';
      } else if (event.type === 'research.failed') {
        phase.value = 'failed';
        currentNode.value = '';
        runningNodes.clear();
      }
      // 不在历史 failed/completed 处关连接：后面可能紧跟恢复执行事件。
    });
    current.addEventListener('research.sync', () => {
      if (source !== current) return;
      // 服务端确认已补发到当前末尾且研究已停止，才关闭订阅。
      stopped = true;
      currentNode.value = '';
      runningNodes.clear();
      close();
      void refresh();
    });
    current.addEventListener('research.removed', () => {
      if (source !== current) return;
      stopped = true;
      close();
      void refresh();
    });
  }

  watch(progress, value => {
    if (!value || value.report_ready) {
      close();
      currentNode.value = '';
    } else connect();
  });

  function visibility(): void {
    if (document.hidden) close();
    else connect();
  }
  onMounted(() => document.addEventListener('visibilitychange', visibility));
  onBeforeUnmount(() => {
    disposed = true;
    close();
    document.removeEventListener('visibilitychange', visibility);
  });
  return { draft, phase, connection, currentNode };
}
