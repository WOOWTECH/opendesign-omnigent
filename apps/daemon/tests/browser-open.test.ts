import { EventEmitter } from 'node:events';
import type { ChildProcess, SpawnOptions } from 'node:child_process';
import { describe, expect, it, vi } from 'vitest';

import { createBrowserOpenInvocation, openBrowser } from '../src/browser/index.js';

describe('browser open helper', () => {
  it.each([
    {},
    { DISPLAY: ' ', WAYLAND_DISPLAY: '' },
  ])('declines headless Linux without spawning an opener', (env) => {
    const spawn = vi.fn();
    const warn = vi.fn();

    expect(openBrowser('https://omnigent.example/', {
      platform: 'linux', env, spawn, warn,
    })).toBeNull();
    expect(spawn).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
  });

  it.each([
    { platform: 'linux' as const, env: { DISPLAY: ':0' }, command: 'xdg-open' },
    { platform: 'linux' as const, env: { WAYLAND_DISPLAY: 'wayland-0' }, command: 'xdg-open' },
    { platform: 'darwin' as const, env: {}, command: 'open' },
    { platform: 'win32' as const, env: {}, command: 'cmd.exe' },
  ])('retains desktop opening on $platform', ({ platform, env, command }) => {
    const child = new EventEmitter() as ChildProcess;
    child.unref = vi.fn() as ChildProcess['unref'];
    const spawn = vi.fn((_command: string, _args: string[], _options: SpawnOptions) => child);
    const url = 'https://omnigent.example/';

    expect(openBrowser(url, { platform, env, spawn })).toBe(child);
    expect(spawn).toHaveBeenCalledOnce();
    expect(spawn.mock.calls[0]?.[0]).toBe(command);
    expect(child.unref).toHaveBeenCalledOnce();
  });

  it('opens URLs on Windows through cmd.exe instead of spawning the shell builtin directly', () => {
    const invocation = createBrowserOpenInvocation('win32', 'http://127.0.0.1:7456/');

    expect(invocation.command).toMatch(/cmd(?:\.exe)?$/i);
    expect(invocation.args.slice(0, 3)).toEqual(['/d', '/s', '/c']);
    expect(invocation.args[3]).toContain('start "" "http://127.0.0.1:7456/"');
    expect(invocation.options).toMatchObject({ windowsVerbatimArguments: true });
  });

  it('escapes cmd.exe metacharacters before opening Windows URLs', () => {
    const invocation = createBrowserOpenInvocation('win32', 'https://example.test/a b?q=%USERPROFILE%&name="A"');

    expect(invocation.args[3]).toMatch(/^"start "" /);
    expect(invocation.args[3]).toContain('"^%"USERPROFILE"^%"');
    expect(invocation.args[3]).toContain('name=""A""');
  });

  it('handles opener spawn errors without crashing the daemon', () => {
    const child = new EventEmitter() as ChildProcess;
    child.unref = vi.fn() as ChildProcess['unref'];
    const spawn = vi.fn(() => child);
    const warn = vi.fn();

    openBrowser('http://127.0.0.1:7456/', {
      platform: 'linux',
      env: { DISPLAY: ':0' },
      spawn,
      warn,
    });

    expect(() => child.emit('error', new Error('spawn xdg-open ENOENT'))).not.toThrow();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('failed to open browser'));
  });
});
