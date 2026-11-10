import { createRouter, createWebHashHistory } from 'vue-router';
import NewResearch from './views/NewResearch.vue';
import ResearchDetail from './views/ResearchDetail.vue';

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'new', component: NewResearch },
    { path: '/research/:runId', name: 'research', component: ResearchDetail, props: true },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
  scrollBehavior: () => ({ top: 0 }),
});
