import { useEffect, useRef, useState } from "react";
import { deleteBook, type Book } from "../api/client";

export function DeleteBook({ book, run, disabled, done }: { book: Book; run: string; disabled: boolean; done: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const request = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  useEffect(() => () => request.current?.abort(), []);

  async function confirm() {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError(undefined);
    try {
      await deleteBook(run, book.id, controller.signal);
      if (!controller.signal.aborted) { dialog.current?.close(); done(); }
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not delete the book. Try again.");
    } finally {
      request.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  if (book.id === "earlier") return null;
  return <details className="studio-management">
    <summary>Manage book</summary>
    <p>Remove this book and its recorded pages from your library.</p>
    <button className="studio-danger" disabled={disabled} onClick={() => { setError(undefined); dialog.current?.showModal(); }}>Delete book</button>
    <dialog ref={dialog} className="studio-dialog" aria-describedby="delete-book-warning" aria-label="Delete book" onCancel={event => { if (request.current) event.preventDefault(); }}>
      <h2>Delete {book.name}?</h2>
      <p id="delete-book-warning">This permanently deletes the book, all its pages, images, and narration. This cannot be undone.</p>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="camera-controls">
        <button autoFocus disabled={busy} onClick={() => dialog.current?.close()}>Cancel</button>
        <button className="studio-danger" disabled={busy} onClick={() => void confirm()}>{busy ? "Deleting…" : "Confirm delete book"}</button>
      </div>
    </dialog>
  </details>;
}
