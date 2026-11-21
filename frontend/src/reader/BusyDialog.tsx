import { useEffect, useRef } from "react";

export function BusyDialog({ message, onStop }: { message: string; onStop: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current!;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return <dialog ref={ref} className="studio-dialog studio-busy" aria-label="Preparing your reading" onCancel={event => { event.preventDefault(); onStop(); }}>
    <span className="spinner" aria-hidden="true" />
    <h2>Preparing your reading</h2>
    <p role="status">{message}</p>
    <button onClick={onStop}>Stop waiting</button>
  </dialog>;
}
