// Copyright 2018 Signal Messenger, LLC
// SPDX-License-Identifier: AGPL-3.0-only

import { app, dialog, clipboard } from 'electron';
import os from 'node:os';
import { appendFileSync, existsSync } from 'node:fs';

import * as Errors from '../ts/types/errors.std.ts';
import { redactAll } from '../ts/util/privacy.node.ts';
import { drop } from '../ts/util/drop.std.ts';
import { createLogger } from '../ts/logging/log.std.ts';
import { reallyJsonStringify } from '../ts/util/reallyJsonStringify.std.ts';
import type { LocaleType } from './locale.node.ts';

const log = createLogger('global_errors');

// --- EIO harness instrumentation (test-only, fully inert unless env is set) ---
const INSTR_ENABLED = process.env.SIGNAL_EIO_INSTR === '1';
const INSTR_FILE =
  process.env.SIGNAL_EIO_EVENTS ?? '/tmp/opencode/signal-eio/events.jsonl';

function instrEvent(kind: string, detail: unknown): void {
  if (!INSTR_ENABLED) {
    return;
  }
  try {
    appendFileSync(
      INSTR_FILE,
      `${JSON.stringify({
        t: new Date().toISOString(),
        pid: process.pid,
        kind,
        detail: String(detail).slice(0, 200),
      })}\n`
    );
  } catch {
    // Instrumentation must never throw.
  }
}

if (INSTR_ENABLED && process.env.SIGNAL_EIO_TRIGGER) {
  const triggerPath = process.env.SIGNAL_EIO_TRIGGER;
  let armed = true;
  const timer = setInterval(() => {
    if (armed && existsSync(triggerPath)) {
      armed = false;
      instrEvent('trigger-fired', triggerPath);
      // Both write paths: console (immediate stdio write) and pino (console
      // mirror in dev + on-disk destination). Deterministic "log-worthy
      // event" fired by the harness after killing the PTY.
      // oxlint-disable-next-line no-console
      console.warn('eio-test-trigger');
      log.warn('eio-test-trigger');
    }
  }, 250);
  // oxlint-disable-next-line @typescript-eslint/no-unsafe-call
  timer.unref?.();
}
instrEvent('instr-ready', 'armed');
// --- end EIO harness instrumentation ---

// We use hard-coded strings until we're able to update these strings from the locale.
let quitText = 'Quit';
let copyErrorAndQuitText = 'Copy error and quit';

// The error reporter must not manufacture new errors by writing to the same
// stream whose failure it is reporting. EIO/EPIPE from stdio would otherwise
// re-enter this handler and stack additional dialogs.
function safeReport(prefix: string, formattedError: string): void {
  try {
    // oxlint-disable-next-line no-console
    console.error(`${prefix}:`, formattedError);
    log.error(`${prefix}:`, formattedError);
  } catch {
    // Sync write failure (TTY-backed stdio): swallow - the on-disk rotating
    // log already captured the error via pino's file destination.
  }
}

// Consecutive identical fatal errors (e.g. a storm of EIO write failures from
// an orphaned stdio) must present as a single modal, not an uncloseable
// conveyor of duplicate dialogs. Within the window, duplicates are counted
// and dropped; the first occurrence still shows the dialog and exits.
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

function handleError(prefix: string, error: Error): void {
  instrEvent('handleError', `${prefix}: ${error?.message}`);
  if (isDuplicateFatal(prefix, error)) {
    instrEvent('deduped', `total=${dedupedCount}`);
    return;
  }
  const formattedError = Errors.toLogFormat(error);
  safeReport(prefix, formattedError);

  if (app.isReady()) {
    instrEvent('dialog', prefix);
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
  process.on('exit', code => {
    instrEvent('exit', `code=${code}`);
  });
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
