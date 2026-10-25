import { readFileSync } from 'node:fs';
import { InstalledPluginRecordSchema } from '@open-design/contracts';
import type { Locator, Page } from '@playwright/test';

import { expect, test } from '@/playwright/suite';
import { applyStandardMocks } from '@/playwright/mock-factory';

const launchers = [
  { id: 'woow-cli-terminal', path: '/console/' },
  { id: 'woow-mcp-server', path: '/mcp-admin/' },
  { id: 'woow-omnigent', path: '/settings/omnigent' },
  { id: 'woow-paseo', path: '/paseo/setup/' },
] as const;

// Paseo is supplied by the optional image layer. The other manifests come
// directly from this Omnigent base so its launcher contract stays covered.
const plugins = launchers.map(({ id, path }) => {
  const manifest = id === 'woow-paseo'
    ? { name: id, title: 'Paseo', version: '0.2.0', author: { name: 'WoowTech' },
        description: 'Pair the official Paseo App with this workspace.', od: { launch: { path } } }
    : JSON.parse(readFileSync(new URL(`../../plugins/_official/woow/${id}/open-design.json`, import.meta.url), 'utf8'));
  return InstalledPluginRecordSchema.parse({
    id, title: manifest.title, version: manifest.version, manifest,
    sourceKind: 'bundled', source: `/plugins/${id}`, fsPath: `/plugins/${id}`,
    trust: 'bundled', capabilitiesGranted: [], installedAt: 0, updatedAt: 0,
  });
});

async function expectInsideViewport(page: Page, locator: Locator) {
  await expect(locator).toBeVisible();
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
}

async function openLauncher(page: Page, link: Locator, path: string, touch: boolean) {
  await expectInsideViewport(page, link);
  await expect(link).toHaveAttribute('href', path);
  await expect(link).toHaveAttribute('target', '_blank');
  await expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  if (touch) {
    const box = (await link.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(44);
    expect(box.width).toBeGreaterThanOrEqual(44);
  }
  const previousUrl = page.url();
  const opened = page.waitForEvent('popup');
  if (touch) await link.tap();
  else {
    await link.focus();
    await page.keyboard.press('Enter');
  }
  const popup = await opened;
  try {
    await expect(popup).toHaveURL(new URL(path, previousUrl).href);
    await expect(popup.getByTestId('launcher-destination')).toBeVisible();
    expect(page.url()).toBe(previousUrl);
  } finally {
    await popup.close();
  }
}

for (const width of [320, 390, 720, 721, 1440]) {
  test.describe(`${width}px native launchers`, () => {
    const touch = width <= 720;
    test.use({ viewport: { width, height: 900 }, hasTouch: touch, isMobile: touch });

    test.beforeEach(async ({ page, context }) => {
      await applyStandardMocks(page);
      await page.addInitScript(() => {
        localStorage.setItem('open-design:locale', 'zh-TW');
        localStorage.setItem('open-design:locale-source', 'manual');
      });
      await page.route('**/api/plugins**', async (route) => {
        const pathname = new URL(route.request().url()).pathname;
        const plugin = plugins.find(({ id }) => pathname === `/api/plugins/${id}`);
        await route.fulfill({ json: plugin ?? { plugins } });
      });
      await page.route('**/api/marketplaces**', (route) => route.fulfill({ json: { marketplaces: [] } }));
      // Only the destination is a fixture: real product markup, styles,
      // navigation and native anchor activation are exercised by Chromium.
      for (const { path } of launchers) {
        await context.route(`**${path}`, (route) => route.fulfill({
          contentType: 'text/html', body: '<main data-testid="launcher-destination">Local launcher destination</main>',
        }));
      }
      await page.goto('/plugins');
      await expect(page.getByTestId('plugins-card-woow-omnigent')).toBeVisible();
    });

    test('[P1] all four list Open links can be activated without navigating the card', async ({ page }, testInfo) => {
      for (const { id, path } of launchers) {
        await openLauncher(page, page.getByTestId(`plugins-card-open-${id}`), path, touch);
      }
      await testInfo.attach('native-launchers-list', {
        body: await page.screenshot(), contentType: 'image/png',
      });
    });

    test('[P1] card details retain an actionable Open link at every breakpoint', async ({ page }) => {
      for (const { id, path } of launchers) {
        const card = page.getByTestId(`plugins-card-${id}`).locator('strong').first();
        if (touch) await card.tap();
        else await card.click();
        await openLauncher(page, page.getByTestId('plugin-detail-open'), path, touch);
        const back = page.locator('.plugin-suite-detail__back');
        if (touch) await back.tap();
        else await back.click();
        await expect(page.getByTestId(`plugins-card-${id}`)).toBeVisible();
      }
    });

    test('[P1] non-launcher actions keep their original responsive visibility', async ({ page }) => {
      // Evaluate the real stylesheet with ordinary row-action controls both
      // inside and outside a marketplace row. Restricting the hide selector
      // to direct row children would accidentally unhide detail actions.
      const displays = await page.evaluate(() => {
        return ['plugin-marketplace__row', 'plugin-suite-detail__section'].map((className) => {
          const parent = document.createElement('section');
          parent.className = className;
          const action = document.createElement('button');
          action.className = 'plugin-marketplace__row-action';
          action.textContent = 'Install';
          parent.append(action);
          document.body.append(parent);
          const display = getComputedStyle(action).display;
          parent.remove();
          return display;
        });
      });
      // Grid items blockify inline-flex to flex on desktop; the detail control stays inline.
      expect(displays).toEqual(touch ? ['none', 'none'] : ['flex', 'inline-flex']);
    });
  });
}
