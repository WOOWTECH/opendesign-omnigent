// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../src/i18n', () => ({
  useT: () => (key: string) => key,
}));

vi.mock('../src/providers/omnigent', () => ({
  OmnigentRequestError: class OmnigentRequestError extends Error {},
  setupOmnigent: vi.fn(),
}));

vi.mock('../src/components/OmnigentIssue', () => ({
  OmnigentIssue: () => null,
}));

import { OmnigentSetupDialog } from '../src/components/OmnigentSetupDialog';

afterEach(() => cleanup());

describe('Omnigent setup dialog focus', () => {
  it('focuses the server field after the initial busy state unlocks the form', () => {
    const props = {
      workspaceContext: null,
      issueCode: null,
      onClose: vi.fn(),
      onStatus: vi.fn(),
    };
    const view = render(<OmnigentSetupDialog {...props} busy />);
    const server = screen.getByLabelText('omnigent.server');

    expect(screen.getByRole('dialog')).toBeVisible();
    expect(document.activeElement).toBe(document.body);
    expect(server).toBeDisabled();

    view.rerender(<OmnigentSetupDialog {...props} busy={false} />);

    expect(server).toHaveFocus();
  });
});
