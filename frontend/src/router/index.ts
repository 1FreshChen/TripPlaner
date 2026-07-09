import { createRouter, createWebHistory } from 'vue-router'
import Home from '../views/Home.vue'
import Result from '../views/Result.vue'
import History from '../views/History.vue'
import Conversation from '../views/Conversation.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: Home },
    { path: '/result', name: 'result', component: Result },
    { path: '/history', name: 'history', component: History },
    { path: '/conversation/:sessionId?', name: 'conversation', component: Conversation }
  ]
})

export default router
