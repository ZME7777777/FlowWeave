import { createContext, useContext } from 'react';

export type DialogTone = 'default' | 'danger';
export interface ConfirmOptions {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: DialogTone;
}
export interface PromptOptions extends ConfirmOptions {
  inputLabel: string;
  placeholder?: string;
  initialValue?: string;
}
export interface DialogApi {
  confirm: (options: ConfirmOptions) => Promise<boolean>;
  prompt: (options: PromptOptions) => Promise<string | null>;
}

export const DialogContext = createContext<DialogApi | null>(null);

export function useProductDialog(): DialogApi {
  const value = useContext(DialogContext);
  if (!value) throw new Error('useProductDialog must be used inside ProductDialogProvider');
  return value;
}
