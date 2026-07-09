# Smart Trip Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a runnable basic intelligent travel assistant based on HelloAgents Chapter 13.

**Architecture:** The app is split into a FastAPI backend and a Vue 3 frontend. The backend owns validation, agent orchestration, mock fallbacks, and optional external API integrations; the frontend owns form input, itinerary display, editing, map rendering, and export.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic v2, requests, pytest, Vue 3, TypeScript, Vite, Ant Design Vue, Axios, AMap JS API, html2canvas, jsPDF.

---

### Task 1: Backend Tests

**Files:**
- Create: `backend/tests/test_models.py`
- Create: `backend/tests/test_trip_planner.py`
- Create: `backend/tests/test_api.py`

- [x] Write failing tests for schema validation, deterministic planner output, planner query construction, and the API response.
- [x] Run `python -m pytest tests -q`.
- [x] Confirm tests fail before implementation because the `app` package and dependencies are missing.

### Task 2: Backend Models and Services

**Files:**
- Create: `backend/app/models/schemas.py`
- Create: `backend/app/services/mock_data.py`
- Create: `backend/app/services/budget.py`
- Create: `backend/app/services/amap_service.py`
- Create: `backend/app/services/unsplash_service.py`
- Create: `backend/app/services/llm_service.py`
- Create: `backend/app/config.py`

- [x] Implement Pydantic models matching Chapter 13.
- [x] Implement mock data generators for no-key local execution.
- [x] Implement external-service adapters that safely return empty results when keys are absent.

### Task 3: Backend Agent Orchestration

**Files:**
- Create: `backend/app/agents/prompts.py`
- Create: `backend/app/agents/trip_planner.py`

- [x] Implement four agent roles: attraction search, weather query, hotel recommendation, and planner.
- [x] Implement `TripPlannerAgent.plan_trip`.
- [x] Keep the Chapter 13 planner query builder for future LLM integration.

### Task 4: FastAPI API

**Files:**
- Create: `backend/app/api/main.py`
- Create: `backend/app/api/routes/trip.py`
- Create: `backend/run.py`
- Create: `backend/requirements.txt`

- [x] Expose `/api/health`.
- [x] Expose `POST /api/trip/plan`.
- [x] Enrich attractions with Unsplash images when `UNSPLASH_ACCESS_KEY` is present.

### Task 5: Frontend App

**Files:**
- Create: `frontend/src/types/index.ts`
- Create: `frontend/src/services/api.ts`
- Create: `frontend/src/router/index.ts`
- Create: `frontend/src/views/Home.vue`
- Create: `frontend/src/views/Result.vue`
- Create: `frontend/src/App.vue`
- Create: `frontend/src/main.ts`
- Create: `frontend/src/styles.css`

- [x] Build a form-first home screen.
- [x] Build result sections for overview, budget, map, itinerary, and weather.
- [x] Add editing controls and export controls.

### Task 6: Documentation and Handoff

**Files:**
- Create: `.env.example`
- Create: `README.md`
- Create: `docs/API_KEYS.md`
- Create: `docs/PROJECT_STATUS.md`

- [x] Document all empty API Key locations.
- [x] Document completed and pending work for continuation.
- [x] Document backend/frontend startup commands.

### Task 7: Verification

**Files:**
- All files above.

- [x] Run `python -m compileall app tests -q`.
- [x] Run `python -m pytest tests -q` after installing requirements.
- [x] Run `npm install` and `npm run build` after installing frontend dependencies.
- [x] Start backend and frontend, then verify local HTTP responses.
- [ ] Inspect the local app visually in an in-app browser when a Browser tool is available.
