import { useEffect, useRef, useState } from "react";
import { updateBook, type Book } from "../api/client";
import { BookMetadataFields, bookLanguages, type BookMetadata } from "./BookMetadataFields";

type Props = { book: Book; run: string; disabled: boolean; updated: (book: Book) => void };

export function BookHeading({ book, run, disabled, updated }: Props) {
  const [draft, setDraft] = useState<(BookMetadata & { name: string }) | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  const changed = draft && (draft.name.trim() !== book.name || draft.level !== (book.level ?? null) || draft.language !== (book.language ?? null));
  async function save() {
    if (!draft || !draft.name.trim() || !changed || disabled || active.current) return;
    const details = { ...draft, name: draft.name.trim() };
    const request = new AbortController();
    active.current = request;
    setSaving(true); setError("");
    try {
      await updateBook(run, book.id, details, request.signal);
      if (!request.signal.aborted) { updated({ ...book, ...details }); setDraft(null); }
    } catch (cause) {
      if (!request.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not save the book details.");
    } finally { active.current = null; if (!request.signal.aborted) setSaving(false); }
  }
  if (draft === null) return <div className="section-heading studio-title"><div><h1>{book.name}</h1>{(book.level || book.language) && <p className="book-metadata">{[book.level, book.language && bookLanguages[book.language]].filter(Boolean).join(" · ")}</p>}</div>{book.id !== "earlier" && <button disabled={disabled} onClick={() => { setError(""); setDraft({ name: book.name, level: book.level ?? null, language: book.language ?? null }); }}>Edit book</button>}</div>;
  return <form className="studio-rename" aria-label="Edit book details" onSubmit={event => { event.preventDefault(); void save(); }}>
    <label htmlFor="rename-book">Book name</label>
    <input id="rename-book" autoFocus required maxLength={200} value={draft.name} disabled={saving} onChange={event => setDraft({ ...draft, name: event.target.value })} />
    <BookMetadataFields prefix="edit-book" value={draft} disabled={saving || disabled} onChange={metadata => setDraft({ ...draft, ...metadata })} />
    {error && <p className="error" role="alert">{error}</p>}
    <div className="camera-controls"><button className="primary" disabled={disabled || saving || !draft.name.trim() || !changed}>{saving ? "Saving…" : "Save changes"}</button><button type="button" disabled={saving} onClick={() => setDraft(null)}>Cancel</button></div>
  </form>;
}
