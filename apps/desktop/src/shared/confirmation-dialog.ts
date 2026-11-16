import type { ExitDialogEvent, ExitDialogResponse } from './exit-dialog';

export type ConfirmationDetails = { title: string; message: string; detail: string; action: string };
export type ConfirmationDialogEvent = ExitDialogEvent & Partial<ConfirmationDetails>;
export type ConfirmationDialogResponse = ExitDialogResponse;
