import { ref } from 'vue';

// A refresh signal, not a second copy of backend state.
export const historyRevision = ref(0);
export const refreshHistory = () => { historyRevision.value += 1; };
