import { useEffect, useRef } from "react";

export function BusyDialog({ message, onStop }: { message: string; onStop: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current!;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return <dialog ref={ref} className="busy-dialog" aria-label="Preparing your reading" onCancel={event => { event.preventDefault(); onStop(); }}>
    <div><span className="spinner" aria-hidden="true" /><p role="status">{message}</p><button onClick={onStop}>Stop waiting</button></div>
  </dialog>;
}
