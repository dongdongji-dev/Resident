import { createRouter, createWebHashHistory } from "vue-router";
import ChatView from "./views/ChatView.vue";
import ConsoleView from "./views/ConsoleView.vue";
import SkillsView from "./views/SkillsView.vue";
import ConfigView from "./views/ConfigView.vue";

export default createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", component: ChatView },
    { path: "/console", component: ConsoleView },
    { path: "/skills", component: SkillsView },
    { path: "/config", component: ConfigView },
  ],
});
