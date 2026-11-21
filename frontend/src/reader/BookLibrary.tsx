import { useEffect, useRef, useState } from "react";
import { createBook, listBooks, recordCompletedRead, type Book } from "../api/client";
import type { PublicConfig } from "../api/contracts.generated";
import { Thumbnail } from "./Thumbnail";
import { Camera } from "../camera/Camera";
import { Icon, ShelfArt } from "../Studio";

function libraryRun() {
  const key = "little-pages-library-run";
  const run = sessionStorage.getItem(key) || crypto.randomUUID();
  sessionStorage.setItem(key, run);
  return run;
}

export function BookLibrary({ config }: { config: PublicConfig }) {
  const [run] = useState(libraryRun);
  const [selected, setSelected] = useState<{ book: Book; preview: boolean }>();
  const previousBook = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (selected?.book.id !== previousBook.current && !selected?.preview) {
      document.getElementById("workspace")?.focus();
    }
    previousBook.current = selected?.book.id;
  }, [selected]);
  const [books, setBooks] = useState<Book[]>();
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState<string>();
  const [newBook, setNewBook] = useState(false);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const createTrigger = useRef<HTMLButtonElement>(null);
  const searchField = useRef<HTMLInputElement>(null);
  const nameField = useRef<HTMLInputElement>(null);
  useEffect(() => { if (newBook) nameField.current?.focus(); }, [newBook]);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const creation = useRef<AbortController | null>(null);
  useEffect(() => () => creation.current?.abort(), []);
  useEffect(() => {
    if (selected) return;
    const request = new AbortController();
    setError(undefined);
    setLoading(true);
    void listBooks(run, request.signal).then(result => {
      if (!request.signal.aborted) setBooks(result.items);
    }).catch(() => { if (!request.signal.aborted) setError("Could not load your books. Select Refresh to try again."); })
      .finally(() => { if (!request.signal.aborted) setLoading(false); });
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
  const term = query.trim().toLocaleLowerCase();
  const matches = books?.filter(book => book.name.toLocaleLowerCase().includes(term));
  function clearSearch() { setQuery(""); searchField.current?.focus(); }
  return <section className="library" aria-label="Book library">
    <div className="shelf-banner"><div><span className="eyebrow">Reading studio</span><h1>My books</h1><p>Capture the pages you love and listen to them, one book at a time.</p>
      <button ref={createTrigger} className="banner-action" aria-expanded={newBook} onClick={() => { setNewBook(true); nameField.current?.focus(); }}><Icon name="plus" />Create new book</button>
    </div><ShelfArt /></div>
    {newBook && <form className="new-book camera-card" aria-label="Create new book" onSubmit={event => { event.preventDefault(); void save(); }}>
      <label htmlFor="book-name">Book name</label><p>Give your book a title. You’ll add its pages next.</p><div className="form-row"><input ref={nameField} id="book-name" placeholder="e.g. The garden journal" required maxLength={200} value={name} onChange={event => setName(event.target.value)} disabled={saving} />
      <button className="primary" disabled={!name.trim() || saving}>{saving ? "Creating…" : "Create book"}</button><button type="button" disabled={saving} onClick={() => { setName(""); setNewBook(false); createTrigger.current?.focus(); }}>Cancel</button></div>
    </form>}
    {error && <p className="error" role="alert">{error}</p>}
    {unsaved.length > 0 && <div className="error" role="alert">Could not save all completed reads. <button onClick={() => unsaved.forEach(item => void finish(item))}>Retry saving</button></div>}
    <section className="collection" aria-labelledby="shelf-title"><div className="shelf-toolbar"><div><span className="eyebrow">Library</span><h2 id="shelf-title">Your bookshelf {books && <span className="count">{books.length}</span>}</h2></div>
      <div className="shelf-tools">{Boolean(books?.length || term) && <label className="search-field"><Icon name="search" /><input ref={searchField} type="search" aria-label="Search books by title" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search by title" /></label>}
      <button className="refresh-button" disabled={loading} onClick={() => setRevision(value => value + 1)}><Icon name="refresh" />Refresh</button></div></div>
    <div className={loading ? "status-line" : "sr-only"} role="status">{loading ? <><span className="spinner" />Loading books…</> : matches ? `${matches.length} ${matches.length === 1 ? "book" : "books"} ${term ? "found" : "on your shelf"}` : ""}</div>
    {matches?.length === 0 ? <div className="shelf-empty"><span className="empty-mark"><Icon name={term ? "search" : "shelf"} /></span><h3>{term ? "No matching books" : "Your first book starts here"}</h3><p>{term ? "Try another title or clear your search to see the full shelf." : "Create a book above, then use your camera to add its first page."}</p>{term && <button onClick={clearSearch}>Clear search</button>}</div> : <ul className="book-list">{matches?.map(book => <li key={book.id}>
      <button className="book-title" disabled={saving} aria-label={`Open book: ${book.name}`} onClick={() => setSelected({ book, preview: false })}><span className="cover-tile">{book.cover ? <Thumbnail item={book.cover} placeholder="Book" /> : <Icon name="book" />}</span><span><strong>{book.name}</strong><small>Completed reads: {book.read_count ?? 0}</small></span></button>
      <button className="preview-link" disabled={saving} aria-label={`Slide Preview: ${book.name}`} onClick={() => setSelected({ book, preview: true })}><Icon name="play" />Slide Preview</button>
    </li>)}</ul>}
    </section>
  </section>;
}
