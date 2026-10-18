import { useSyncExternalStore } from "react";

let toasts = [];
let nextId = 1;
const listeners = new Set();
const emit = () => listeners.forEach((fn) => fn());

export function toast(message, { kind = "info", timeout = 4500 } = {}) {
  const id = nextId++;
  toasts = [...toasts, { id, message, kind }];
  emit();
  if (timeout) setTimeout(() => dismiss(id), timeout);
  return id;
}

export function dismiss(id) {
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

export function useToasts() {
  return useSyncExternalStore(
    (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    () => toasts,
  );
}
