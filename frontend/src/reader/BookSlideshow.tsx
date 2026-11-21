import { useEffect, useRef, useState } from "react";
import { readingAudio, readingOriginal, readingRequest, thumbnailSource } from "../api/client";
import type { PlayableCapture } from "../api/contracts.generated";
import "./BookSlideshow.css";
import { pendingState } from "./useHistory";
import { NarrationEditor, waitForNarration } from "./NarrationEditor";

type Props = { pages: PlayableCapture[]; startIndex: number; close: () => void; changed?: (page: PlayableCapture) => void; onComplete?: () => void };
type Status = "loading" | "playing" | "thinking" | "paused" | "blocked" | "error" | "finished";
type Position = { index: number; status: Status };
const pageDelaySeconds = 2;
const initialIndex = (pages: PlayableCapture[], index: number) => Math.max(0, Math.min(pages.length - 1, Math.trunc(index) || 0));

const awaitingPage = (page?: PlayableCapture) => pendingState(page?.state);

export function startBookPlayback(snapshot: PlayableCapture[], start: number, audio: HTMLAudioElement, update: (position: Position) => void, autoplay = true, onComplete?: () => void, onPage?: (index: number, page: PlayableCapture) => void) {
  const pages = snapshot.slice();
  let nextPage = 0, completed = false;
  let index = initialIndex(pages, start), chunk = 0, attempt = 0;
  let request: AbortController | undefined, url: string | undefined;
  let pageTimer: ReturnType<typeof setTimeout> | undefined;
  let pendingEnd: (() => void) | undefined;
  let status: Status = "loading", wanted = autoplay && !document.hidden, disposed = false;
  function report(next: Status) {
    status = next;
    update({ index, status });
  }
  function release() {
    clearTimeout(pageTimer);
    pendingEnd = undefined;
    request?.abort();
    ++attempt;
    audio.onended = audio.onerror = audio.onpause = null;
    audio.pause();
    if (url) {
      audio.removeAttribute("src");
      audio.load();
      URL.revokeObjectURL(url);
      url = undefined;
    }
  }
  function pause() {
    if (disposed || status === "finished" || status === "error") return;
    clearTimeout(pageTimer);
    wanted = false;
    ++attempt;
    audio.pause();
    report("paused");
  }
  async function play() {
    if (document.hidden) return pause();
    const token = ++attempt;
    report("loading");
    try {
      await audio.play();
      if (token === attempt) report("playing");
    } catch (error) {
      if (token !== attempt) return;
      wanted = false;
      report((error as Error)?.name === "NotAllowedError" ? "blocked" : "error");
    }
  }
  async function load() {
    release();
    let page = pages[index];
    if (!page) return report("error");
    request = new AbortController();
    const { signal } = request;
    report(wanted ? "loading" : "paused");
    try {
      if (awaitingPage(page)) {
        const job = await waitForNarration(page, await readingRequest(page.run_id, page.id, signal), signal);
        if (signal.aborted) return;
        page = pages[index] = { ...page, state: job.state, narration: job.narration, language: job.language, audio_chunks: job.audio_chunks, reason: job.reason, retryable: job.retryable };
        onPage?.(index, page);
      }
      if (!page.audio_chunks) return report("error");
      const blob = await readingAudio(page.run_id, page.id, chunk, signal);
      if (signal.aborted) return;
      url = URL.createObjectURL(blob);
      audio.src = url;
      audio.onerror = () => {
        if (!signal.aborted && audio.error) { ++attempt; wanted = false; report("error"); }
      };
      audio.onpause = () => {
        if (!signal.aborted && audio.paused && !audio.ended && status === "playing") pause();
      };
      function advance() {
        if (chunk + 1 < page.audio_chunks) ++chunk;
        else if (index + 1 < pages.length) { ++index; chunk = 0; }
        else { release(); wanted = false; report("finished"); return; }
        void load();
      }
      audio.onended = () => {
        if (signal.aborted || !audio.onended || !audio.ended || status === "error") return;
        audio.onended = null;
        const pageEnded = chunk + 1 === page.audio_chunks;
        if (pageEnded && index === nextPage) ++nextPage;
        const bookEnded = !completed && nextPage === pages.length;
        if (bookEnded) completed = true;
        if (document.hidden || !wanted) { pendingEnd = advance; pause(); }
        else if (pageEnded && index + 1 < pages.length) {
          pendingEnd = advance;
          report("thinking");
          pageTimer = setTimeout(() => { if (document.hidden) pause(); else advance(); }, pageDelaySeconds * 1000);
        } else advance();
        if (bookEnded) onComplete?.();
      };
      if (wanted) void play();
    } catch {
      if (!signal.aborted) { wanted = false; report("error"); }
    }
  }
  function visibility() { if (document.hidden) pause(); }
  document.addEventListener("visibilitychange", visibility);
  window.addEventListener("pagehide", pause);
  void load();
  return {
    pause,
    move(delta: number) {
      if (disposed || index + delta < 0 || index + delta >= pages.length) return;
      index += delta; chunk = 0;
      nextPage = Math.min(nextPage, index);
      void load();
    },
    toggle() {
      if (disposed || document.hidden) return;
      if (wanted && status !== "error") return pause();
      wanted = true;
      if (status === "finished") { index = 0; chunk = 0; nextPage = 0; completed = false; }
      if (status === "finished" || status === "error") void load();
      else if (pendingEnd) pendingEnd();
      else if (url) void play();
      else report("loading");
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("pagehide", pause);
      release();
    },
  };
}

export async function readPagePreview(page: PlayableCapture, thumbnailOnly: boolean, signal: AbortSignal) {
  if (!thumbnailOnly) {
    try {
      const blob = await readingOriginal(page.run_id, page.id, signal);
      signal.throwIfAborted();
      return { url: URL.createObjectURL(blob), thumbnail: false };
    } catch (error) {
      if (signal.aborted || !page.thumbnail_url) throw error;
    }
  }
  if (!page.thumbnail_url) throw new Error("No thumbnail available.");
  const url = await thumbnailSource(page.thumbnail_url, page.run_id, signal);
  return { url, thumbnail: true };
}

function PagePreview({ page }: { page: PlayableCapture }) {
  const [thumbnailOnly, setThumbnailOnly] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [image, setImage] = useState<Awaited<ReturnType<typeof readPagePreview>>>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const request = new AbortController();
    const { signal } = request;
    let owned: string | undefined;
    void readPagePreview(page, thumbnailOnly, signal).then(result => {
      if (result.url.startsWith("blob:")) owned = result.url;
      if (!signal.aborted) setImage(result);
      else if (owned) URL.revokeObjectURL(owned);
    }).catch(() => { if (!signal.aborted) setFailed(true); });
    return () => { request.abort(); if (owned) URL.revokeObjectURL(owned); };
  }, [page, thumbnailOnly, attempt]);
  if (failed) return <div><p role="alert">Could not display this page image.</p><button onClick={() => { setFailed(false); setThumbnailOnly(false); setAttempt(n => n + 1); }}>Retry image</button></div>;
  if (!image) return <p role="status">Loading page image…</p>;
  return <img src={image.url} alt={image.thumbnail ? "Captured page thumbnail" : "Original captured page"} onError={() => {
    setImage(undefined);
    if (!image.thumbnail && page.thumbnail_url) setThumbnailOnly(true);
    else setFailed(true);
  }} />;
}

const messages: Record<Status, string> = { loading: "Loading narration…", playing: "Playing narration", thinking: `Next page in ${pageDelaySeconds} seconds…`, paused: "Narration paused", blocked: "Tap Play to hear the narration.", error: "Could not play this narration.", finished: "End of book" };

export function BookSlideshow({ pages, startIndex, close, changed, onComplete }: Props) {
  const completion = useRef(onComplete);
  useEffect(() => { completion.current = onComplete; }, [onComplete]);
  const dialog = useRef<HTMLDialogElement>(null);
  const audio = useRef<HTMLAudioElement>(null);
  const player = useRef<ReturnType<typeof startBookPlayback> | null>(null);
  // Keep this viewing session stable if the history list refreshes behind the modal.
  const [book, setBook] = useState(() => ({ pages: pages.slice(), start: initialIndex(pages, startIndex), autoplay: true }));
  const [editing, setEditing] = useState<"edit" | "check" | null>(null);
  const [{ index, status }, update] = useState<Position>({ index: book.start, status: "loading" });
  const [expanded, setExpanded] = useState(false);
  const [resolved, setResolved] = useState<Record<number, PlayableCapture>>({});
  const page = resolved[index] ?? book.pages[index];
  const pending = awaitingPage(page);
  const unsuccessful = page && ["FAILED", "REJECTED", "DUPLICATE"].includes(page.state ?? "");
  const action = page && !page.audio_chunks && !pending ? "Check audio" : status === "error" ? "Retry audio" : status === "finished" ? "Replay" : ["loading", "playing", "thinking"].includes(status) ? "Pause" : "Play";
  const icons = { "Check audio": "🔄", "Retry audio": "🔄", Replay: "🔁", Pause: "⏸️", Play: "▶️" };
  useEffect(() => {
    const modal = dialog.current!;
    modal.showModal();
    return () => modal.close();
  }, []);
  useEffect(() => {
    const session = startBookPlayback(book.pages, book.start, audio.current!, update, book.autoplay, () => completion.current?.(), (i, item) => setResolved(previous => ({ ...previous, [i]: item })));
    player.current = session;
    return () => { session.dispose(); player.current = null; };
  }, [book]);
  function dismiss() { player.current?.dispose(); dialog.current?.close(); close(); }
  return <dialog ref={dialog} className="book-slideshow" aria-label="Book slideshow" onCancel={event => { event.preventDefault(); dismiss(); }}>
    <div className="book-slideshow-image" data-expanded={expanded && !editing}>
      {page && (pending || (unsuccessful && !page.thumbnail_url) ? <p>{pending ? "Preparing this page…" : "This page is unavailable."}</p> : <PagePreview key={index} page={page} />)}
      {page && !editing && <>
        <button className="slide-arrow previous" aria-label="Previous page" disabled={index === 0} onClick={() => player.current?.move(-1)}>⬅️</button>
        <button className="slide-arrow next" aria-label="Next page" disabled={index === book.pages.length - 1} onClick={() => player.current?.move(1)}>➡️</button>
        <button className="book-image-toggle" aria-pressed={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? "Show text & controls" : "Expand image"}</button>
      </>}
    </div>
    <section className="book-slideshow-panel">
      <header><h2>Book slideshow</h2><button onClick={dismiss}>Close</button></header>
      <div className="book-slideshow-content" key={index}>
      {page ? <><p className="book-slideshow-counter" role="status">Page {index + 1} of {book.pages.length}</p>
        {editing ? <NarrationEditor page={page} checkAudio={editing === "check"} cancel={() => setEditing(null)} changed={() => changed?.(page)} saved={updated => {
          setBook({ pages: book.pages.map((item, i) => i === index ? { ...updated, state: "READY" } : resolved[i] ?? item), start: index, autoplay: false });
          setResolved({});
          setEditing(null);
        }} /> : <p className="book-slideshow-text" lang={page.language}>{page.narration}</p>}
      </> : <p>No pages to show.</p>}
    </div>
    {page && !editing && <div className="book-slideshow-controls">
      <p role="status">{pending && status === "loading" ? "Waiting for this page to finish processing…" : pending || page.audio_chunks ? messages[status] : page.reason || "Page audio is not ready. Check the page status in Book pages."}</p>
      <button aria-label={action} disabled={unsuccessful && !page.retryable} onClick={() => action === "Check audio" ? setEditing("check") : player.current?.toggle()}>{icons[action]}</button>
      <button disabled={pending || !page.narration} onClick={() => { player.current?.pause(); setEditing("edit"); }}>Edit text</button>
    </div>}
    </section>
    <audio ref={audio} />
  </dialog>;
}
