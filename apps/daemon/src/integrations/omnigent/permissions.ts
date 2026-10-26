import { constants } from 'node:fs';
import { open, stat, type FileHandle } from 'node:fs/promises';
import path from 'node:path';

export type OmnigentPrivacyResult = 'restored' | 'missing' | 'unsupported-platform';

const directoryFlags = constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW;
const fileFlags = constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK;
const bound = (directory: FileHandle, name: string) => `/proc/self/fd/${directory.fd}/${name}`;

async function openExisting(target: string, flags: number): Promise<FileHandle | undefined> {
  try { return await open(target, flags); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined;
    throw error;
  }
}

async function openStateDirectory(stateDir: string): Promise<FileHandle | undefined> {
  const parts = stateDir.split('/').filter(Boolean);
  if (!path.isAbsolute(stateDir) || !parts.length || parts.some(part => part === '.' || part === '..')) {
    throw new Error('Invalid private state path');
  }
  let current: FileHandle | undefined = await open('/', directoryFlags);
  try {
    // Missing procfs must fail, not masquerade as an absent integration. This
    // inode check also verifies the same Linux fd-binding used by snapshots.
    const root = await current.stat({ bigint: true });
    const alias = await stat(`/proc/self/fd/${current.fd}`, { bigint: true });
    if (!alias.isDirectory() || root.dev !== alias.dev || root.ino !== alias.ino) {
      throw new Error('Unavailable directory descriptor');
    }
    // Node has no openat API. Walk EVERY component via the already-open parent;
    // O_NOFOLLOW on the full stateDir alone would still follow ancestor links.
    for (const part of parts) {
      const next = await openExisting(bound(current, part), directoryFlags);
      if (!next) return undefined;
      const previous = current;
      current = next;
      await previous.close();
    }
    const result = current;
    current = undefined;
    return result;
  } finally { await current?.close(); }
}

async function privateFile(directory: FileHandle, name: string): Promise<void> {
  const file = await openExisting(bound(directory, name), fileFlags);
  if (!file) return;
  try {
    const info = await file.stat();
    if (!info.isFile() || info.nlink !== 1) throw new Error('Unsafe private file');
    if ((info.mode & 0o7777) !== 0o600) await file.chmod(0o600);
  } finally { await file.close(); }
}

/**
 * Repair existing daemon-owned state before service.load reads it, including
 * disabled integrations. Never creates files or reads credentials.
 * Linux uses procfs-bound descriptors, as in task-input-snapshot.ts. Other
 * platforms retain their existing behavior: no POSIX privacy claim is made.
 * Errors intentionally contain no OS paths or credential content.
 */
export async function restoreOmnigentPrivacy(stateDir: string): Promise<OmnigentPrivacyResult> {
  if (process.platform !== 'linux') return 'unsupported-platform';
  try {
    const state = await openStateDirectory(stateDir);
    if (!state) return 'missing';
    try {
      await state.chmod(0o700);
      await privateFile(state, 'state.json');
      await privateFile(state, 'host-owner.json');
      const host = await openExisting(bound(state, 'host'), directoryFlags);
      if (host) {
        try {
          await host.chmod(0o700);
          await privateFile(host, 'auth_tokens.json');
          await privateFile(host, 'config.yaml');
        } finally { await host.close(); }
      }
      return 'restored';
    } finally { await state.close(); }
  } catch {
    throw new Error('Unable to restore private Omnigent permissions.');
  }
}
