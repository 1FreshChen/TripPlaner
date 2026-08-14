export interface Location {
  longitude: number
  latitude: number
}

export interface Attraction {
  name: string
  address: string
  location: Location
  visit_duration: number
  description: string
  category?: string
  rating?: number
  image_url?: string
  ticket_price?: number
}

export interface Meal {
  type: 'breakfast' | 'lunch' | 'dinner' | 'snack' | string
  name: string
  address?: string
  location?: Location
  description?: string
  estimated_cost: number
  // Baidu Maps restaurant detail fields
  rating?: number
  price_per_person?: number
  shop_hours?: string
  comment_num?: number
}

export interface Hotel {
  name: string
  address: string
  location?: Location
  price_range: string
  rating: string
  distance: string
  type: string
  estimated_cost: number
}

export interface Budget {
  total_attractions: number
  total_hotels: number
  total_meals: number
  total_transportation: number
  total: number
}

export interface WeatherInfo {
  date: string
  day_weather: string
  night_weather: string
  day_temp: number
  night_temp: number
  wind_direction: string
  wind_power: string
}

export interface DayPlan {
  date: string
  day_index: number
  description: string
  transportation: string
  accommodation: string
  hotel?: Hotel
  attractions: Attraction[]
  meals: Meal[]
}

export interface TripPlan {
  city: string
  start_date: string
  end_date: string
  days: DayPlan[]
  weather_info: WeatherInfo[]
  overall_suggestions: string
  budget?: Budget
}

export type PlanStatus = 'idle' | 'draft' | 'generating' | 'completed' | 'editing' | 'archived'

export interface TripPlanResponse extends TripPlan {
  plan_id: string
  status: PlanStatus
  version: number
}

export interface TripPlanRequest {
  session_id?: string
  city: string
  start_date: string
  end_date: string
  days: number
  preferences: string
  budget: string
  transportation: string
  accommodation: string
}

export type TaskStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'expired'
export type TaskPhase =
  | 'queued'
  | 'preparing'
  | 'collecting_context'
  | 'llm_planning'
  | 'validating'
  | 'meal_enrichment'
  | 'saving'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'expired'

export interface TripPlanTaskCreatedResponse {
  task_id: string
  status: TaskStatus
  status_url: string
  events_url: string
  result_url: string
}

export interface TripPlanTaskStatusResponse {
  task_id: string
  status: TaskStatus
  phase: TaskPhase
  progress: number
  message: string
  queued_at: string
  started_at?: string
  finished_at?: string
  updated_at: string
  elapsed_ms: number
  phase_elapsed_ms: number
  phase_timings: Record<string, number>
  result_url?: string
  error_code?: string
  error_message?: string
}

export interface TripPlanTaskPendingResult {
  status: TaskStatus
  message: string
}

export interface TripPlanUpdateRequest {
  plan_json: TripPlan
  expected_version: number
  change_summary: string
}

export interface PlanVersion {
  version: number
  change_summary?: string
  created_at: string
}

export interface SessionInfo {
  session_id: string
  user_id: string
  created_at: string
  updated_at?: string
  is_new?: boolean
}

export interface SessionTripPlanSummary {
  id: string
  city: string
  start_date: string
  end_date: string
  status: PlanStatus
  version: number
  created_at: string
  updated_at: string
  days_count?: number
  preferences?: string
  budget_level?: string
}

export interface SessionDetailResponse extends SessionInfo {
  trip_plans: SessionTripPlanSummary[]
  conversation_count: number
  updated_at: string
}

export interface ConversationMessage {
  id: string
  role: 'user' | 'assistant' | 'system' | 'tool' | string
  content: string
  tool_calls?: Record<string, unknown>[] | null
  created_at: string
  plan_updated?: boolean
  plan_update_failed?: boolean
  _planUpdated?: boolean
  _planUpdateFailed?: boolean
}

export interface ConversationReply {
  message_id: string
  role: string
  content: string
  tool_calls: Record<string, unknown>[]
  updated_plan?: TripPlanResponse | null
  plan_update_failed?: boolean
}

export interface ConversationListResponse {
  messages: ConversationMessage[]
  has_more: boolean
}

export interface UserPreferences {
  preferred_categories: string[]
  budget_profile: Record<string, number>
  travel_style: string
  favorite_cities: string[]
}

export interface SavedItem {
  id: string
  item_type: string
  item_data: Record<string, unknown>
  tags?: string[]
  note?: string
  created_at: string
}
