import { useEffect, useRef, useState } from "react";
import { renameBook, type Book } from "../api/client";

type Props = { book: Book; run: string; disabled: boolean; renamed: (name: string) => void };

export function BookHeading({ book, run, disabled, renamed }: Props) {
  const [draft, setDraft] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  async function save() {
    const name = draft?.trim();
    if (!name || name === book.name || disabled || active.current) return;
    const request = new AbortController();
    active.current = request;
    setSaving(true); setError("");
    try {
      await renameBook(run, book.id, name, request.signal);
      if (!request.signal.aborted) { renamed(name); setDraft(null); }
    } catch (cause) {
      if (!request.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not rename the book.");
    } finally { active.current = null; if (!request.signal.aborted) setSaving(false); }
  }
  if (draft === null) return <div className="section-heading studio-title"><h1>{book.name}</h1>{book.id !== "earlier" && <button disabled={disabled} onClick={() => { setError(""); setDraft(book.name); }}>Rename</button>}</div>;
  return <form className="studio-rename" onSubmit={event => { event.preventDefault(); void save(); }}>
    <label htmlFor="rename-book">Book name</label>
    <input id="rename-book" autoFocus required maxLength={200} value={draft} disabled={saving} onChange={event => setDraft(event.target.value)} />
    {error && <p className="error" role="alert">{error}</p>}
    <div className="camera-controls"><button className="primary" disabled={disabled || saving || !draft.trim() || draft.trim() === book.name}>{saving ? "Saving…" : "Save name"}</button><button type="button" disabled={saving} onClick={() => setDraft(null)}>Cancel</button></div>
  </form>;
}
