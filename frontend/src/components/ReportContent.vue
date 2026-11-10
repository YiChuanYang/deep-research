<script setup lang="ts">
import { computed } from 'vue';
import MarkdownIt from 'markdown-it';
import DOMPurify from 'dompurify';
import type { Citation } from '../types';
import { safeUrl } from '../display';

const props = defineProps<{ markdown: string; citations: Citation[] }>();
const emit = defineEmits<{ select: [id: string] }>();
const parser = new MarkdownIt({ html: false, linkify: false, breaks: false });

const rendered = computed(() => {
  const html = DOMPurify.sanitize(parser.render(props.markdown), {
    USE_PROFILES: { html: true }, FORBID_TAGS: ['img', 'iframe', 'form', 'input', 'button'],
  });
  const doc = new DOMParser().parseFromString(html, 'text/html');
  for (const link of doc.querySelectorAll('a')) {
    const url = safeUrl(link.getAttribute('href') ?? '');
    if (url) { link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; }
    else link.removeAttribute('href');
  }
  const allowed = new Set(props.citations.map(item => item.evidence_id));
  const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  let current = walker.nextNode();
  while (current) {
    if (!current.parentElement?.closest('pre, code, a, button')) nodes.push(current as Text);
    current = walker.nextNode();
  }
  // Operate on text nodes so citation replacement cannot alter HTML attributes.
  for (const node of nodes) {
    const text = node.textContent ?? '';
    const fragment = doc.createDocumentFragment();
    let cursor = 0;
    let changed = false;
    for (const match of text.matchAll(/\[(E\d{4,})\]/g)) {
      const id = match[1];
      if (!id || match.index === undefined || !allowed.has(id)) continue;
      fragment.append(doc.createTextNode(text.slice(cursor, match.index)));
      const button = doc.createElement('button');
      button.type = 'button'; button.className = 'citation-link'; button.textContent = match[0];
      button.dataset.evidenceId = id; button.setAttribute('aria-label', `查看证据 ${id}`);
      fragment.append(button);
      cursor = match.index + match[0].length; changed = true;
    }
    if (changed) { fragment.append(doc.createTextNode(text.slice(cursor))); node.replaceWith(fragment); }
  }
  return doc.body.innerHTML;
});
function click(event: MouseEvent): void {
  if (!(event.target instanceof Element)) return;
  const id = event.target.closest<HTMLButtonElement>('button[data-evidence-id]')?.dataset.evidenceId;
  if (id && props.citations.some(item => item.evidence_id === id)) emit('select', id);
}
</script>

<template><article class="prose" @click="click"><div v-html="rendered"></div></article></template>
