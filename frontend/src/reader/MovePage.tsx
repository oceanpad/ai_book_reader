import { useEffect, useRef, useState } from "react";
import { moveBookPage } from "../api/client";
import type { PlayableCapture } from "../api/contracts.generated";

type Props = {
  run: string;
  bookId: string;
  page: PlayableCapture;
  position: number;
  count: number;
  close: () => void;
  saved: (order: string[], destination: number) => void;
};

export function MovePage({ run, bookId, page, position, count, close, saved }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const request = useRef<AbortController | null>(null);
  const [destination, setDestination] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const pageNumber = Number(destination);
  const valid = position > 0 && Number.isInteger(pageNumber) && pageNumber >= 1 && pageNumber <= count && pageNumber !== position;
  useEffect(() => {
    const modal = dialog.current!;
    modal.showModal();
    return () => { request.current?.abort(); modal.close(); };
  }, []);

  function dismiss() {
    dialog.current?.close();
    close();
  }

  async function save() {
    if (!valid || request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError(undefined);
    try {
      const result = await moveBookPage(run, bookId, { run_id: page.run_id, page_id: page.id, page_number: pageNumber }, controller.signal);
      if (!controller.signal.aborted) {
        dialog.current?.close();
        saved(result.page_order, pageNumber);
      }
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not move the page. Try again.");
    } finally {
      request.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return <dialog ref={dialog} className="studio-dialog" aria-label="Move page" onCancel={event => { event.preventDefault(); if (!request.current) dismiss(); }}>
    <h2>{position ? `Move page ${position}` : "Move page"}</h2>
    <p id="move-page-help">{position ? `Choose a position from 1 to ${count}. The pages in between will shift automatically.` : "This page is no longer in the book."}</p>
    <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <label className="studio-page-number">New page number
        <input autoFocus type="number" inputMode="numeric" min={1} max={count} step={1} required
          aria-describedby="move-page-help" value={destination} disabled={busy || !position}
          onChange={event => setDestination(event.target.value)} />
      </label>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="studio-actions">
        <button type="submit" className="primary" disabled={busy || !valid}>{busy ? "Moving…" : "Move page"}</button>
        <button type="button" disabled={busy} onClick={dismiss}>Cancel</button>
      </div>
    </form>
  </dialog>;
}
