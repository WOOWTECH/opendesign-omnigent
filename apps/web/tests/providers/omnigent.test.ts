import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  fetchOmnigentStatus, isOmnigentPlugin, OmnigentRequestError,
  omnigentNeedsAgentAuth, omnigentProbeUrl, omnigentPublicUrl, runOmnigentAction, setupOmnigent, safeOmnigentIssueCode,
} from '../../src/providers/omnigent';

afterEach(() => vi.unstubAllGlobals());
describe('native Omnigent request boundary', () => {
  it('recognizes only the canonical ID', () => {
    expect(isOmnigentPlugin({ id: 'woow-omnigent' })).toBe(true);
    expect(isOmnigentPlugin({ id: 'woow-omnigent-copy' })).toBe(false);
    expect(isOmnigentPlugin({ id: 'Omnigent' })).toBe(false);
    expect(isOmnigentPlugin(null)).toBe(false);
  });
  it('sends credentials only in the explicit setup body', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true, status: {} })));
    vi.stubGlobal('fetch', fetch);
    await setupOmnigent({ server: 'https://omnigent.example', username: 'user', password: 'synthetic-private-password' });
    expect(fetch).toHaveBeenCalledWith('/api/integrations/omnigent/actions', expect.objectContaining({
      method: 'POST', cache: 'no-store', credentials: 'same-origin',
      body: JSON.stringify({ action: 'setup', server: 'https://omnigent.example', username: 'user', password: 'synthetic-private-password' }),
    }));
  });
  it('does not attach verification fields to another lifecycle action', async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ ok: true, status: {} }))));
    vi.stubGlobal('fetch', fetch);
    await runOmnigentAction('disable', {}, { engine: 'claude', model: 'private-model' });
    expect(JSON.parse(fetch.mock.calls[0]![1].body)).toEqual({ action: 'disable' });
    await fetchOmnigentStatus();
    expect(fetch.mock.calls[1]![1].body).toBeUndefined();
  });
  it('drops remote error messages and unrecognized codes', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: { code: 'synthetic-private-password', message: 'echo synthetic-private-password' },
    }), { status: 400 })));
    await expect(setupOmnigent({ server: 'https://example.com', username: 'user', password: 'synthetic-private-password' }))
      .rejects.toMatchObject({ code: 'REQUEST_FAILED', message: 'REQUEST_FAILED' });
  });
  it('preserves an allowlisted busy code without forwarding its message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: { code: 'OPERATION_BUSY', message: 'synthetic-private-password' },
    }), { status: 409 })));
    await expect(runOmnigentAction('verify')).rejects.toBeInstanceOf(OmnigentRequestError);
  });
  it('opens only credential-free HTTP origins/paths', () => {
    expect(omnigentPublicUrl('https://omnigent.example')).toBe('https://omnigent.example/');
    for (const url of ['https://u:p@example.com', 'https://example.com?token=p', 'https://example.com#p', 'javascript:alert(1)'])
      expect(omnigentPublicUrl(url)).toBeNull();
  });
  it.each(['RATE_LIMITED', 'FREE_USAGE_LIMIT_REACHED', 'INSUFFICIENT_QUOTA', 'QUOTA_EXCEEDED'])('preserves only the structured provider code %s', code => {
    expect(safeOmnigentIssueCode(code)).toBe(code);
    expect(omnigentNeedsAgentAuth(code)).toBe(false);
    expect(safeOmnigentIssueCode(code + ' private-detail')).toBe('REQUEST_FAILED');
  });
  it('maps only known native-auth, model-catalog and timeout receipt reasons', () => {
    expect(safeOmnigentIssueCode('NATIVE_AUTH_REQUIRED')).toBe('NEEDS_AUTH');
    expect(safeOmnigentIssueCode('needs-auth')).toBe('NEEDS_AUTH');
    expect(safeOmnigentIssueCode('MODELS_UNAVAILABLE')).toBe('MODELS_UNAVAILABLE');
    expect(safeOmnigentIssueCode('OP_TIMEOUT')).toBe('OP_TIMEOUT');
    expect(safeOmnigentIssueCode('OP_TIMEOUT synthetic-private-output')).toBe('REQUEST_FAILED');
  });
  it('allowlists readiness without assuming it or a missing catalog means missing auth', () => {
    expect(safeOmnigentIssueCode('ENGINE_NOT_READY')).toBe('ENGINE_NOT_READY');
    expect(safeOmnigentIssueCode('ENGINE_NOT_READY synthetic-private-output')).toBe('REQUEST_FAILED');
    expect(omnigentNeedsAgentAuth('ENGINE_NOT_READY')).toBe(false);
    expect(omnigentNeedsAgentAuth('MODELS_UNAVAILABLE')).toBe(false);
    expect(omnigentNeedsAgentAuth('NEEDS_AUTH')).toBe(true);
  });
  it('accepts only the matching existing session on the exact configured server and base path', () => {
    expect(omnigentProbeUrl('https://omnigent.example', { sessionId: 'probe', url: 'https://omnigent.example/c/probe' }))
      .toBe('https://omnigent.example/c/probe');
    expect(omnigentProbeUrl('https://omnigent.example/tenant/', { sessionId: 'probe', url: 'https://omnigent.example/tenant/c/probe' }))
      .toBe('https://omnigent.example/tenant/c/probe');
  });
  it.each([
    'https://other.example/c/probe', 'https://omnigent.example.evil/c/probe',
    'http://omnigent.example/c/probe', 'https://omnigent.example:8443/c/probe',
    'https://u:p@omnigent.example/c/probe', 'https://omnigent.example/c/probe?token=p',
    'https://omnigent.example/c/probe#p', 'https://omnigent.example/c/another',
    'https://omnigent.example/redirect', 'javascript:alert(1)',
  ])('rejects an unsafe or mismatched probe link %s', (url) => {
    expect(omnigentProbeUrl('https://omnigent.example', { sessionId: 'probe', url })).toBeNull();
  });
  it('rejects missing session identity and a different configured base path', () => {
    expect(omnigentProbeUrl('https://omnigent.example', { url: 'https://omnigent.example/c/probe' })).toBeNull();
    expect(omnigentProbeUrl('https://omnigent.example/tenant', { sessionId: 'probe', url: 'https://omnigent.example/c/probe' })).toBeNull();
  });
});
