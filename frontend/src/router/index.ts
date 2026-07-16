import { createRouter, createWebHistory } from 'vue-router'

const Home = () => import('../views/Home.vue')
const Result = () => import('../views/Result.vue')
const History = () => import('../views/History.vue')
const Conversation = () => import('../views/Conversation.vue')
const Task = () => import('../views/Task.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: Home },
    { path: '/result/:planId?', name: 'result', component: Result },
    { path: '/trip/tasks/:taskId', name: 'task', component: Task },
    { path: '/history', name: 'history', component: History },
    { path: '/conversation/:sessionId?', name: 'conversation', component: Conversation }
  ]
})

export default router
