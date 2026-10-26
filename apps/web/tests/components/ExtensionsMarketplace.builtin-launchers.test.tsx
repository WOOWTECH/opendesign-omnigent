// @vitest-environment jsdom

// WoowTech built-in launcher plugins (bundled, `od.launch`): pinned above the
// installed (personal) list with a Built-in badge, opened through a real
// root-absolute anchor in a new tab, never uninstallable, and kept out of
// every apply/use catalogue (composer @-search, home gallery, rails).

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { InstalledPluginRecordSchema } from '@open-design/contracts';

import { ExtensionsMarketplace } from '../../src/components/PluginsView';
import { PluginDetailView } from '../../src/components/PluginDetailView';
import { I18nProvider } from '../../src/i18n';
import type { Locale } from '../../src/i18n/types';
import { isVisiblePlugin, listPlugins, resetPluginsCache } from '../../src/state/projects';

vi.mock('../../src/analytics/provider', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/analytics/provider')>();
  return { ...actual, useAnalytics: () => ({ track: vi.fn() }) };
});

// Spread the real module — see the note in ExtensionsMarketplace.team-scope.test.tsx.
vi.mock('../../src/collab/useWorkspaceContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../src/collab/useWorkspaceContext')>()),
  useWorkspaceContext: () => ({ context: null, loading: false, refresh: vi.fn() }),
  useWorkspaceBilling: () => null,
}));

function record(input: {
  id: string;
  sourceKind: 'bundled' | 'user';
  title: string;
  titleI18n?: Record<string, string>;
  launch?: string;
}) {
  return InstalledPluginRecordSchema.parse({
    id: input.id,
    title: input.title,
    version: '0.1.0',
    sourceKind: input.sourceKind,
    source: `/plugins/${input.id}`,
    trust: input.sourceKind === 'bundled' ? 'bundled' : 'trusted',
    capabilitiesGranted: [],
    manifest: {
      name: input.id,
      title: input.title,
      ...(input.titleI18n ? { title_i18n: input.titleI18n } : {}),
      version: '0.1.0',
      description: `${input.title} description`,
      ...(input.sourceKind === 'bundled' && input.launch ? { author: { name: 'WoowTech' } } : {}),
      od: input.launch ? { launch: { path: input.launch } } : { kind: 'skill' },
    },
    fsPath: `/plugins/${input.id}`,
    installedAt: 0,
    updatedAt: 0,
  });
}

const MCP = record({ id: 'woow-mcp-server', sourceKind: 'bundled', title: 'MCP Server', launch: '/mcp-admin/' });
const CLI = record({
  id: 'woow-cli-terminal',
  sourceKind: 'bundled',
  title: 'CLI Terminal',
  titleI18n: { en: 'CLI Terminal', 'zh-CN': 'CLI 终端', 'zh-TW': 'CLI 終端機' },
  launch: '/console/',
});
const PASEO = record({
  id: 'woow-paseo', sourceKind: 'bundled', title: 'Paseo',
  titleI18n: { en: 'Paseo', 'zh-CN': 'Paseo 设置', 'zh-TW': 'Paseo 設定' },
  launch: '/paseo/setup/',
});
const USER_PLUGIN = record({ id: 'my-plugin', sourceKind: 'user', title: 'My plugin' });
const BUNDLED_PLUGIN = record({ id: 'bundled-suite', sourceKind: 'bundled', title: 'Bundled suite' });
// od.launch on a non-bundled plugin is ignored: it stays an ordinary plugin.
const USER_WITH_LAUNCH = record({ id: 'user-launch', sourceKind: 'user', title: 'User launch', launch: '/x/' });

const omnigentManifest = JSON.parse(readFileSync(resolve(__dirname, '../../../../plugins/_official/woow/woow-omnigent/open-design.json'), 'utf8'));
const OMNIGENT = InstalledPluginRecordSchema.parse({
  ...record({ id: omnigentManifest.name, sourceKind: 'bundled', title: omnigentManifest.title }),
  manifest: omnigentManifest,
});
const LAUNCHERS = [CLI, MCP, OMNIGENT, PASEO];
const PLUGINS = [MCP, USER_PLUGIN, BUNDLED_PLUGIN, CLI, OMNIGENT, PASEO];

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  resetPluginsCache();
  window.history.replaceState(null, '', '/plugins');
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const plugin = LAUNCHERS.find(({ id }) => url === `/api/plugins/${id}`);
    if (plugin) return jsonResponse(plugin);
    if (url.startsWith('/api/plugins')) return jsonResponse({ plugins: PLUGINS });
    if (url.startsWith('/api/marketplaces')) return jsonResponse({ marketplaces: [] });
    if (url === '/api/skills') return jsonResponse({ skills: [] });
    if (url.includes('/api/workspace/')) return jsonResponse({ ids: [], resources: [] });
    return jsonResponse({});
  }) as typeof fetch;
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

async function renderInstalledTab(locale: Locale = 'en', onUsePlugin = vi.fn()) {
  const utils = render(
    <I18nProvider initial={locale}>
      <ExtensionsMarketplace onCreatePlugin={vi.fn()} onUsePlugin={onUsePlugin} />
    </I18nProvider>,
  );
  fireEvent.click(await screen.findByTestId('plugins-tab-installed'));
  await waitFor(() => {
    expect(utils.container.querySelectorAll('.plugin-marketplace__item').length).toBe(5);
  });
  return utils;
}

function cardIds(container: HTMLElement): string[] {
  return [...container.querySelectorAll<HTMLElement>('.plugin-marketplace__item')]
    .map((card) => card.dataset.testid ?? '');
}

describe('built-in launcher plugins on the Plugins page', () => {
  it('leads the default (official) tab too, so they show as soon as the page opens', async () => {
    const { container } = render(
      <I18nProvider initial="zh-TW">
        <ExtensionsMarketplace onCreatePlugin={vi.fn()} onUsePlugin={vi.fn()} />
      </I18nProvider>,
    );
    await waitFor(() => {
      expect(cardIds(container).slice(0, 4)).toEqual([
        'plugins-card-woow-cli-terminal',
        'plugins-card-woow-mcp-server',
        'plugins-card-woow-omnigent',
        'plugins-card-woow-paseo',
      ]);
    });
    expect(screen.getByTestId('plugins-card-open-woow-cli-terminal').getAttribute('href')).toBe('/console/');
    expect(screen.getByTestId('plugins-card-builtin-woow-cli-terminal').textContent).toBe('內建');
    expect(screen.getByTestId('plugins-tab-installed').textContent).toContain('已安裝');
  });

  it('pins the launchers above personal plugins, leaving other bundled plugins out', async () => {
    const { container } = await renderInstalledTab();
    expect(cardIds(container)).toEqual([
      'plugins-card-woow-cli-terminal',
      'plugins-card-woow-mcp-server',
      'plugins-card-woow-omnigent',
      'plugins-card-woow-paseo',
      'plugins-card-my-plugin',
    ]);
    expect(screen.getByTestId('plugins-card-builtin-woow-mcp-server').textContent).toBe('Built-in');
    expect(screen.queryByTestId('plugins-card-builtin-my-plugin')).toBeNull();
  });

  it('opens the launch path in a new tab through a plain anchor and offers no uninstall', async () => {
    const onUsePlugin = vi.fn();
    await renderInstalledTab('en', onUsePlugin);
    for (const [id, href] of [['woow-mcp-server', '/mcp-admin/'], ['woow-cli-terminal', '/console/'], ['woow-omnigent', '/settings/omnigent'], ['woow-paseo', '/paseo/setup/']]) {
      const open = screen.getByTestId(`plugins-card-open-${id}`);
      expect(open.tagName).toBe('A');
      expect(open.classList.contains('plugin-launcher-action')).toBe(true);
      expect(open.getAttribute('href')).toBe(href);
      expect(open.getAttribute('target')).toBe('_blank');
      expect(open.getAttribute('rel')).toBe('noopener noreferrer');
      expect(open.textContent).toBe('Open');
      // No overflow menu (share/uninstall) and no "Try it" on a launcher.
      expect(screen.queryByTestId(`plugins-card-more-${id}`)).toBeNull();
      expect(screen.queryByTestId(`plugins-card-uninstall-${id}`)).toBeNull();
      const card = screen.getByTestId(`plugins-card-${id}`);
      expect(within(card).queryByText('Try it')).toBeNull();
    }
    // The ordinary personal plugin keeps its uninstall menu.
    expect(screen.getByTestId('plugins-card-more-my-plugin')).toBeTruthy();

    fireEvent.click(screen.getByTestId('plugins-card-open-woow-paseo'));
    expect(onUsePlugin).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe('/plugins');
  });

  it.each([['terminal', CLI.id], ['paseo', PASEO.id]])('finds %s through the Plugins page search', async (query, id) => {
    const { container } = await renderInstalledTab();
    fireEvent.change(container.querySelector('.plugin-marketplace__search input')!, {
      target: { value: query },
    });
    await waitFor(() => {
      expect(cardIds(container)).toEqual([`plugins-card-${id}`]);
    });
  });

  it('renders zh-TW titles and chrome from the zh-TW locale', async () => {
    await renderInstalledTab('zh-TW');
    const card = screen.getByTestId('plugins-card-woow-cli-terminal');
    expect(within(card).getByText('CLI 終端機')).toBeTruthy();
    expect(screen.getByTestId('plugins-card-builtin-woow-cli-terminal').textContent).toBe('內建');
    expect(screen.getByTestId('plugins-card-open-woow-cli-terminal').textContent).toBe('開啟');
    const paseoCard = screen.getByTestId('plugins-card-woow-paseo');
    expect(within(paseoCard).getByText('Paseo 設定')).toBeTruthy();
    expect(screen.getByTestId('plugins-card-builtin-woow-paseo').textContent).toBe('內建');
    expect(screen.getByTestId('plugins-card-open-woow-paseo').textContent).toBe('開啟');
  });

  it.each(LAUNCHERS)('gives $id details an Open link instead of Use', async (plugin) => {
    render(
      <I18nProvider initial="zh-TW">
        <PluginDetailView pluginId={plugin.id} />
      </I18nProvider>,
    );
    const open = await screen.findByTestId('plugin-detail-open');
    expect(open.tagName).toBe('A');
    expect(open.classList.contains('plugin-launcher-action')).toBe(true);
    expect(open.getAttribute('href')).toBe(plugin.manifest.od?.launch?.path);
    expect(open.getAttribute('target')).toBe('_blank');
    expect(open.getAttribute('rel')).toBe('noopener noreferrer');
    expect(open.textContent).toBe('開啟');
    expect(screen.queryByTestId('plugin-detail-use')).toBeNull();
    expect(screen.getByText('內建')).toBeTruthy();
    expect(screen.getByText('@WoowTech')).toBeTruthy();
  });
});

describe('launchers stay out of apply/use catalogues', () => {
  it('drops bundled launchers from the visible catalogue the composer and home read', async () => {
    expect(isVisiblePlugin(MCP)).toBe(false);
    expect(isVisiblePlugin(CLI)).toBe(false);
    expect(isVisiblePlugin(OMNIGENT)).toBe(false);
    expect(isVisiblePlugin(PASEO)).toBe(false);
    expect(isVisiblePlugin(BUNDLED_PLUGIN)).toBe(true);
    expect(isVisiblePlugin(USER_WITH_LAUNCH)).toBe(true);

    const visible = await listPlugins();
    expect(visible.map((plugin) => plugin.id)).toEqual(['my-plugin', 'bundled-suite']);
    const all = await listPlugins({ includeHidden: true });
    expect(all.map((plugin) => plugin.id)).toContain('woow-cli-terminal');
    expect(all.map((plugin) => plugin.id)).toContain('woow-paseo');
  });
});
