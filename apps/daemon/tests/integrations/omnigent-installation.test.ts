import { mkdtemp, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import type { InstalledPluginRecord } from '@open-design/contracts';
import { describe, expect, it, vi } from 'vitest';
import { createOmnigentInstallationLookup } from '../../src/integrations/omnigent/onboarding.js';

function record(): InstalledPluginRecord {
  return {
    id: 'woow-omnigent', title: 'Omnigent', version: '1.0.0', sourceKind: 'local',
    source: 'fixture', fsPath: '/unused', installedAt: 42, updatedAt: 84,
    trust: 'trusted', capabilitiesGranted: [], manifest: { name: 'woow-omnigent', version: '1.0.0' },
  } as InstalledPluginRecord;
}

describe('Omnigent scoped installation identity', () => {
  it('preserves the native local installation record and exact resolver context', async () => {
    const native = record();
    const resolve = vi.fn(async () => native);
    const lookup = createOmnigentInstallationLookup(resolve);
    expect(await lookup(null)).toEqual(native);
    expect(resolve).toHaveBeenCalledWith(null);
  });

  it('rejects noncanonical IDs and unsafe installation generation numbers', async () => {
    for (const patch of [{ id: 'other' }, { installedAt: NaN }, { installedAt: Infinity }, { installedAt: -1 }, { updatedAt: NaN }]) {
      const lookup = createOmnigentInstallationLookup(async () => ({ ...record(), ...patch }));
      expect(await lookup(null)).toBeNull();
    }
  });

  it('uses durable Team marker identity instead of ephemeral resolver timestamps', async () => {
    const root = await mkdtemp(path.join(tmpdir(), 'od-omnigent-team-generation-'));
    try {
      const marker = path.join(root, '.od-team-resource.json');
      await writeFile(marker, '{"kind":"plugin"}');
      let reads = 0;
      const lookup = createOmnigentInstallationLookup(async () => ({
        ...record(), source: 'team:plugin:workspace-a:woow-omnigent', fsPath: root,
        installedAt: ++reads, updatedAt: reads,
      }));
      const first = await lookup(null);
      const second = await lookup(null);
      const info = await stat(marker);
      expect(first?.installedAt).toBe(info.birthtimeMs > 0 ? info.birthtimeMs : info.mtimeMs);
      expect(second?.installedAt).toBe(first?.installedAt);
      expect(second?.updatedAt).toBe(info.mtimeMs);
      expect(second?.source).toBe('team:plugin:workspace-a:woow-omnigent');
      expect(second?.manifest).toEqual(record().manifest);
    } finally { await rm(root, { recursive: true, force: true }); }
  });

  it('fails closed when a Team materialization marker no longer exists', async () => {
    const lookup = createOmnigentInstallationLookup(async () => ({ ...record(), source: 'team:plugin:workspace-a:woow-omnigent' }));
    expect(await lookup(null)).toBeNull();
  });
});
