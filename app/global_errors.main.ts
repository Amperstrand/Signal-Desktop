// Copyright 2018 Signal Messenger, LLC
// SPDX-License-Identifier: AGPL-3.0-only

import { app, dialog, clipboard } from 'electron';
import os from 'node:os';

import * as Errors from '../ts/types/errors.std.ts';
import { redactAll } from '../ts/util/privacy.node.ts';
import { drop } from '../ts/util/drop.std.ts';
import { createLogger } from '../ts/logging/log.std.ts';
import { reallyJsonStringify } from '../ts/util/reallyJsonStringify.std.ts';
import type { LocaleType } from './locale.node.ts';

const log = createLogger('global_errors');

// Consecutive identical fatal errors (e.g. a storm of EIO/EPIPE write failures
// from an orphaned stdio) must present as a single modal, not an uncloseable
// conveyor of duplicate dialogs. Within the window, duplicates are dropped;
// the first occurrence still shows the dialog and exits.
// See https://github.com/signalapp/Signal-Desktop/issues/7644
const DEDUPE_WINDOW_MS = 30_000;
let lastErrorKey: string | undefined;
let lastErrorAt = 0;
let dedupedCount = 0;

function isDuplicateFatal(prefix: string, error: Error): boolean {
  const key = `${prefix}|${error?.message ?? ''}`;
  const now = Date.now();
  if (key === lastErrorKey && now - lastErrorAt < DEDUPE_WINDOW_MS) {
    lastErrorAt = now;
    dedupedCount += 1;
    return true;
  }
  lastErrorKey = key;
  lastErrorAt = now;
  return false;
}

// We use hard-coded strings until we're able to update these strings from the locale.
let quitText = 'Quit';
let copyErrorAndQuitText = 'Copy error and quit';

function handleError(prefix: string, error: Error): void {
  if (isDuplicateFatal(prefix, error)) {
    return;
  }
  const formattedError = Errors.toLogFormat(error);
  // oxlint-disable-next-line no-console
  console.error(`${prefix}:`, formattedError);
  log.error(`${prefix}:`, formattedError);

  if (app.isReady()) {
    // title field is not shown on macOS, so we don't use it
    const buttonIndex = dialog.showMessageBoxSync({
      buttons: [quitText, copyErrorAndQuitText],
      defaultId: 0,
      detail: redactAll(formattedError),
      message: prefix,
      noLink: true,
      type: 'error',
    });

    if (buttonIndex === 1) {
      drop(
        clipboard.writeText(
          `${prefix}\n\n${redactAll(formattedError)}\n\n` +
            `App Version: ${app.getVersion()}\n` +
            `OS: ${os.platform()}`
        )
      );
    }
  } else {
    dialog.showErrorBox(prefix, formattedError);
  }

  app.exit(1);
}

export const updateLocale = (locale: LocaleType): void => {
  quitText = locale.i18n('icu:quit');
  copyErrorAndQuitText = locale.i18n('icu:copyErrorAndQuit');
};

function _getError(reason: unknown): Error {
  if (reason instanceof Error) {
    return reason;
  }

  const errorString = reallyJsonStringify(reason);
  return new Error(`Promise rejected with a non-error: ${errorString}`);
}

export const addHandler = (): void => {
  app.on('render-process-gone', (_event, _webContents, details) => {
    const { reason, exitCode } = details;

    if (reason === 'clean-exit') {
      return;
    }

    handleError(
      'Render process is gone',
      new Error(`Reason: ${reason}, Exit Code: ${exitCode}`)
    );
  });

  process.on('uncaughtException', (reason: unknown) => {
    handleError('Unhandled Error', _getError(reason));
  });

  process.on('unhandledRejection', (reason: unknown) => {
    handleError('Unhandled Promise Rejection', _getError(reason));
  });
};
