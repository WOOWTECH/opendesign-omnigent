import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  fetchOmnigentStatus, isOmnigentPlugin, OmnigentRequestError,
  omnigentPublicUrl, runOmnigentAction, setupOmnigent,
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
});
