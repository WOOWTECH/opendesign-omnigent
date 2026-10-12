export const OMNIGENT_PLUGIN_ID = 'woow-omnigent';
export type OmnigentAction = 'enable' | 'disable' | 'setup' | 'verify' | 'disconnect' | 'repair' | 'update';
export interface OmnigentIssue { code: string; message: string; }
export interface OmnigentModel { id: string; displayName?: string; model?: string; isDefault?: boolean; }
export interface OmnigentEngine { installed: boolean; available?: boolean; ready?: boolean; version?: string | null; authStatus?: string | null; models?: OmnigentModel[]; harness?: string; agentId?: string | null; }
export interface OmnigentReceipt { engine?: string; ok: boolean; checkedAt?: string; cliVersion?: string; model?: string | null; sessionId?: string; url?: string; cleanup?: string; reason?: string; reply?: string; }
export interface OmnigentOnboarding { projectId: string; conversationId: string; }
export interface OmnigentOperation { id: string; action: OmnigentAction; phase: string; startedAt: string; finishedAt?: string; issue?: OmnigentIssue; }
export interface OmnigentIntegrationStatus {
  installed: boolean; enabled: boolean; phase: string; server: string | null; loginConfirmed: boolean;
  host: { id?: string | null; name?: string | null; connected: boolean; manager?: string; };
  installedEngines: string[]; engines: Record<string, OmnigentEngine>; verification: Record<string, OmnigentReceipt>;
  modules?: Array<{ key: string; name: string }>;
  onboarding?: OmnigentOnboarding; operation?: OmnigentOperation; issue?: OmnigentIssue;
}
export interface OmnigentActionRequest { action: OmnigentAction; server?: string; username?: string; password?: string; engine?: string; model?: string; }
export interface OmnigentActionResponse { ok: boolean; status: OmnigentIntegrationStatus; }
