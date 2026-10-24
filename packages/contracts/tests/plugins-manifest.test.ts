import { describe, expect, it } from 'vitest';
import {
  OPEN_DESIGN_PLUGIN_SPEC_VERSION,
  MarketplacePluginEntrySchema,
  PluginManifestSchema,
  builtInLaunchPath,
  isSameOriginLaunchPath,
  resolveLocalizedText,
} from '../src/plugins/index.js';

describe('plugin manifest localized text', () => {
  it('exports the current plugin spec version for manifests and registries', () => {
    expect(OPEN_DESIGN_PLUGIN_SPEC_VERSION).toBe('1.0.0');
  });

  it('accepts legacy string use-case queries', () => {
    const manifest = PluginManifestSchema.parse({
      name: 'sample-plugin',
      version: '1.0.0',
      od: {
        useCase: {
          query: 'Make a {{topic}} brief.',
        },
      },
    });

    expect(manifest.od?.useCase?.query).toBe('Make a {{topic}} brief.');
  });

  it('accepts locale-map use-case queries', () => {
    const manifest = PluginManifestSchema.parse({
      name: 'sample-plugin',
      version: '1.0.0',
      od: {
        useCase: {
          query: {
            en: 'Make a {{topic}} brief.',
            'zh-CN': '围绕 {{topic}} 写一份简报。',
          },
        },
      },
    });

    expect(resolveLocalizedText(manifest.od?.useCase?.query, 'zh-CN')).toBe(
      '围绕 {{topic}} 写一份简报。',
    );
  });

  it('accepts a valid preview motion and rejects an invalid one', () => {
    const manifest = PluginManifestSchema.parse({
      name: 'sample-plugin',
      version: '1.0.0',
      od: {
        preview: { type: 'html', entry: './index.html', motion: 'deck' },
      },
    });
    expect(manifest.od?.preview?.motion).toBe('deck');

    expect(() =>
      PluginManifestSchema.parse({
        name: 'sample-plugin',
        version: '1.0.0',
        od: { preview: { type: 'html', motion: 'sideways' } },
      }),
    ).toThrow();
  });

  it('accepts localized title and description metadata', () => {
    const manifest = PluginManifestSchema.parse({
      name: 'sample-plugin',
      version: '1.0.0',
      title: 'Sample Plugin',
      title_i18n: {
        en: 'Sample Plugin',
        'zh-CN': '示例插件',
      },
      description: 'English fallback.',
      description_i18n: {
        en: 'English fallback.',
        'zh-CN': '中文描述。',
      },
    });

    expect(resolveLocalizedText(manifest.title_i18n, 'zh-CN')).toBe('示例插件');
    expect(resolveLocalizedText(manifest.description_i18n, 'zh-CN')).toBe('中文描述。');
  });

  it('accepts localized marketplace entry metadata', () => {
    const entry = MarketplacePluginEntrySchema.parse({
      name: 'open-design/example-sample',
      source: 'github:open-design/plugins/examples/sample',
      version: '1.0.0',
      title: 'Sample',
      title_i18n: {
        en: 'Sample',
        'zh-CN': '示例',
      },
      description: 'English fallback.',
      description_i18n: {
        en: 'English fallback.',
        'zh-CN': '中文描述。',
      },
    });

    expect(resolveLocalizedText(entry.title_i18n, 'zh-CN')).toBe('示例');
    expect(resolveLocalizedText(entry.description_i18n, 'zh-CN')).toBe('中文描述。');
  });

  it('falls back from exact locale to base language, English, then first value', () => {
    expect(resolveLocalizedText({ en: 'English', zh: '中文' }, 'zh-CN')).toBe('中文');
    expect(resolveLocalizedText({ 'zh-CN': '中文' }, 'fr')).toBe('中文');
  });
});

describe('plugin manifest od.launch', () => {
  const withLaunch = (launchPath: string) => ({
    name: 'sample-launcher',
    version: '1.0.0',
    od: { launch: { path: launchPath } },
  });

  it.each(['/console/', '/mcp-admin/', '/console/?tab=1#top', '/a/b.c/'])(
    'accepts same-origin path %s and keeps it',
    (launchPath) => {
      const manifest = PluginManifestSchema.parse(withLaunch(launchPath));
      expect(manifest.od?.launch?.path).toBe(launchPath);
    },
  );

  it.each([
    '',
    'console/',
    '//evil.example/x',
    '/\\evil.example',
    'https://evil.example/',
    'javascript:alert(1)',
    '/console/../api',
    '/console/%2e%2e/api',
    '/%2F%2Fevil.example',
    '/con sole/',
    '/..',
  ])('rejects %j', (launchPath) => {
    expect(PluginManifestSchema.safeParse(withLaunch(launchPath)).success).toBe(false);
    expect(isSameOriginLaunchPath(launchPath)).toBe(false);
  });

  it('honours od.launch only on bundled records', () => {
    const manifest = PluginManifestSchema.parse(withLaunch('/console/'));
    expect(builtInLaunchPath({ sourceKind: 'bundled', manifest })).toBe('/console/');
    expect(builtInLaunchPath({ sourceKind: 'user', manifest })).toBeNull();
    expect(builtInLaunchPath({ sourceKind: 'bundled', manifest: { name: 'x', version: '1' } })).toBeNull();
    // Records read back from storage are re-checked.
    expect(builtInLaunchPath({
      sourceKind: 'bundled',
      manifest: { od: { launch: { path: '//evil.example' } } },
    })).toBeNull();
  });
});
