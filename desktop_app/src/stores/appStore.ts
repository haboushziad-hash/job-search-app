import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import type { CandidateProfile, Role, RunSummary } from '@/types'
import { setApplicationStatus } from '@/services/api'

// Fire-and-forget archive write. Zustand is the source-of-truth for the UI's
// instant feedback; the archive write happens in the background so the user
// never waits for it. If it fails (offline, backend down), the localStorage
// copy still works — they'll just need to reapply on a fresh install.
function syncToArchive(
  role: Role,
  status: 'saved' | 'applied' | 'hidden',
  stage?: string,
): void {
  setApplicationStatus({
    company: role.company || '',
    jobTitle: role.job_title || '',
    jobUrl: role.job_url || undefined,
    status,
    applicationStage: stage,
  }).catch((e) => {
    console.warn('[archive] failed to sync application status:', e)
  })
}

// User-controlled status for each role.
//   saved   = bookmarked for later (didn't apply yet)
//   applied = user clicked "Mark as Applied"
//   hidden  = user removed it from view
export type RoleStatus = 'saved' | 'applied' | 'hidden'

// Application progression after marking as applied.
export type ApplicationStage =
  | 'applied'
  | 'phone_screen'
  | 'interview'
  | 'offer'
  | 'rejected'
  | 'ghosted'
  | 'withdrew'

export interface RoleStatusEntry {
  status: RoleStatus
  date: string                      // ISO timestamp when status was set
  applicationStage?: ApplicationStage
  notes?: string
  // Snapshot of role at time of save (so it survives even if not in latest run)
  roleSnapshot?: Role
}

// Stable identity for a role (used as map key).
export function roleKey(role: Role): string {
  return `${(role.company || '').toLowerCase()}::${(role.job_title || '').toLowerCase()}`
}

interface AppState {
  // Profile
  profile: CandidateProfile | null
  setProfile: (p: CandidateProfile | null) => void

  // Active run
  activeRunId: string | null
  setActiveRunId: (id: string | null) => void

  // Latest results
  lastRoles: Role[]
  lastSummary: RunSummary | null
  setLastResults: (roles: Role[], summary: RunSummary) => void

  // Cache settings
  cacheMaxAgeDays: number
  setCacheMaxAgeDays: (days: number) => void

  // Role tracking
  roleStatuses: Record<string, RoleStatusEntry>
  saveRole: (role: Role) => void
  markApplied: (role: Role, stage?: ApplicationStage) => void
  hideRole: (role: Role) => void
  unsaveRole: (role: Role) => void                 // removes any status
  updateApplicationStage: (role: Role, stage: ApplicationStage) => void
  moveAppliedToSaved: (role: Role) => void
  getStatus: (role: Role) => RoleStatusEntry | undefined

  // Reset
  reset: () => void
}

export const useAppStore = create<AppState>()(
  persist(
    (set, get) => ({
      profile: null,
      activeRunId: null,
      lastRoles: [],
      lastSummary: null,
      roleStatuses: {},
      cacheMaxAgeDays: 7,

      setProfile: (p) => set({ profile: p }),
      setActiveRunId: (id) => set({ activeRunId: id }),
      setLastResults: (roles, summary) =>
        set({ lastRoles: roles, lastSummary: summary }),
      setCacheMaxAgeDays: (days) => set({ cacheMaxAgeDays: days }),

      saveRole: (role) => {
        syncToArchive(role, 'saved')
        set((state) => ({
          roleStatuses: {
            ...state.roleStatuses,
            [roleKey(role)]: {
              status: 'saved',
              date: new Date().toISOString(),
              roleSnapshot: role,
            },
          },
        }))
      },

      markApplied: (role, stage = 'applied') => {
        syncToArchive(role, 'applied', stage)
        set((state) => ({
          roleStatuses: {
            ...state.roleStatuses,
            [roleKey(role)]: {
              status: 'applied',
              date: new Date().toISOString(),
              applicationStage: stage,
              roleSnapshot: role,
            },
          },
        }))
      },

      hideRole: (role) => {
        syncToArchive(role, 'hidden')
        set((state) => ({
          roleStatuses: {
            ...state.roleStatuses,
            [roleKey(role)]: {
              status: 'hidden',
              date: new Date().toISOString(),
              roleSnapshot: role,
            },
          },
        }))
      },

      unsaveRole: (role) =>
        set((state) => {
          const next = { ...state.roleStatuses }
          delete next[roleKey(role)]
          return { roleStatuses: next }
        }),

      updateApplicationStage: (role, stage) =>
        set((state) => {
          const key = roleKey(role)
          const entry = state.roleStatuses[key]
          if (!entry) return state
          return {
            roleStatuses: {
              ...state.roleStatuses,
              [key]: { ...entry, applicationStage: stage },
            },
          }
        }),

      moveAppliedToSaved: (role) =>
        set((state) => {
          const key = roleKey(role)
          const entry = state.roleStatuses[key]
          if (!entry) return state
          return {
            roleStatuses: {
              ...state.roleStatuses,
              [key]: {
                ...entry,
                status: 'saved',
                applicationStage: undefined,
                date: new Date().toISOString(),
              },
            },
          }
        }),

      getStatus: (role) => get().roleStatuses[roleKey(role)],

      reset: () =>
        set({
          profile: null,
          activeRunId: null,
          lastRoles: [],
          lastSummary: null,
          roleStatuses: {},
        }),
    }),
    {
      name: 'job-search-app-state',
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        profile: state.profile,
        lastRoles: state.lastRoles,
        lastSummary: state.lastSummary,
        roleStatuses: state.roleStatuses,
      }),
    }
  )
)
