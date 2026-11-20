import { useEffect, useRef, useState } from "react";
import { createBook, listBooks, recordCompletedRead, type Book } from "../api/client";
import type { PublicConfig } from "../api/contracts.generated";
import { Thumbnail } from "./Thumbnail";
import { Camera } from "../camera/Camera";

function libraryRun() {
  const key = "little-pages-library-run";
  const run = sessionStorage.getItem(key) || crypto.randomUUID();
  sessionStorage.setItem(key, run);
  return run;
}

export function BookLibrary({ config }: { config: PublicConfig }) {
  const [run] = useState(libraryRun);
  const [selected, setSelected] = useState<{ book: Book; preview: boolean }>();
  const [books, setBooks] = useState<Book[]>();
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState<string>();
  const [newBook, setNewBook] = useState(false);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const creation = useRef<AbortController | null>(null);
  useEffect(() => () => creation.current?.abort(), []);
  useEffect(() => {
    if (selected) return;
    const request = new AbortController();
    setError(undefined);
    void listBooks(run, request.signal).then(result => {
      if (!request.signal.aborted) setBooks(result.items);
    }).catch(() => { if (!request.signal.aborted) setError("Could not load your books. Select Refresh to try again."); });
    return () => request.abort();
  }, [run, revision, selected]);

  type Completion = { bookId: string; id: string };
  const [unsaved, setUnsaved] = useState<Completion[]>([]);
  const sending = useRef(new Set<string>());
  async function finish(completion: Completion) {
    if (sending.current.has(completion.id)) return;
    sending.current.add(completion.id);
    setUnsaved(items => items.filter(item => item.id !== completion.id));
    try {
      await recordCompletedRead(run, completion.bookId, completion.id);
      setRevision(value => value + 1);
    } catch {
      setUnsaved(items => [...items, completion]);
    } finally { sending.current.delete(completion.id); }
  }

  async function save() {
    if (!name.trim() || creation.current) return;
    const request = new AbortController();
    creation.current = request;
    setSaving(true);
    setError(undefined);
    try {
      const book = await createBook(run, name.trim(), request.signal);
      if (!request.signal.aborted) { setNewBook(false); setName(""); setSelected({ book, preview: false }); }
    } catch (cause) {
      if (!request.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not create the book.");
    } finally { creation.current = null; if (!request.signal.aborted) setSaving(false); }
  }
  if (selected) return <Camera key={selected.book.id} config={config} run={run} book={selected.book} preview={selected.preview} onComplete={() => void finish({ bookId: selected.book.id, id: crypto.randomUUID() })} back={() => { setBooks(undefined); setSelected(undefined); }} />;
  return <section className="camera-card library" aria-label="Book library">
    <div className="section-heading"><h1>My books</h1><button onClick={() => setRevision(value => value + 1)}>Refresh</button></div>
    <p>Open a book to record pages, or select Slide Preview to listen from the beginning.</p>
    {newBook ? <form className="new-book" onSubmit={event => { event.preventDefault(); void save(); }}>
      <label htmlFor="book-name">Book name</label><input id="book-name" autoFocus required maxLength={200} value={name} onChange={event => setName(event.target.value)} disabled={saving} />
      <div className="camera-controls"><button className="primary" disabled={!name.trim() || saving}>{saving ? "Creating…" : "Create book"}</button><button type="button" disabled={saving} onClick={() => { setName(""); setNewBook(false); }}>Cancel</button></div>
    </form> : <button className="primary" onClick={() => setNewBook(true)}>Create new book</button>}
    {error && <p role="alert">{error}</p>}
    {unsaved.length > 0 && <div role="alert">Could not save all completed reads. <button onClick={() => unsaved.forEach(item => void finish(item))}>Retry saving</button></div>}
    {!books && !error && <p role="status">Loading books…</p>}
    {books?.length === 0 && <p>No books yet. Create one to start recording.</p>}
    <ul className="book-list">{books?.map(book => <li key={book.id}>
      <button className="book-title" disabled={saving} onClick={() => setSelected({ book, preview: false })}>{book.cover && <Thumbnail item={book.cover} placeholder="Book" />}<span>{book.name}<small className="book-read-count">Completed reads: {book.read_count ?? 0}</small></span></button>
      <button disabled={saving} aria-label={`Slide Preview: ${book.name}`} onClick={() => setSelected({ book, preview: true })}>Slide Preview</button>
    </li>)}</ul>
  </section>;
}
