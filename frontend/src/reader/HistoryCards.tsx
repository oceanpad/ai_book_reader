import { useEffect, useRef, useState } from "react";
import { CaptureReview } from "./CaptureReview";
import { readingRequest, setBookCover, type Book } from "../api/client";
import { Thumbnail } from "./Thumbnail";
import type { PlayableCapture } from "../api/contracts.generated";
import type { useHistory } from "./useHistory";

type Props = { book: Book; run: string; history: ReturnType<typeof useHistory>; disabled: boolean; play: (index: number) => void; remove: (item: PlayableCapture) => Promise<void> };
export function HistoryCards({ book, run, history, disabled, play, remove }: Props) {
  const [cover, setCover] = useState(book.cover);
  const [saving, setSaving] = useState<PlayableCapture>();
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const [error, setError] = useState<string>();
  async function selectCover(item: PlayableCapture) {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setSaving(item); setError(undefined);
    try {
      const updated = await setBookCover(run, book.id, item, controller.signal);
      if (!controller.signal.aborted) setCover(updated.cover);
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not save the book cover.");
    } finally { request.current = null; if (!controller.signal.aborted) setSaving(undefined); }
  }
  const [review, setReview] = useState<PlayableCapture>();
  const [retrying, setRetrying] = useState(false);
  async function retry(item: PlayableCapture) {
    setRetrying(true); setError(undefined);
    try {
      await readingRequest(item.run_id, item.id, AbortSignal.timeout(20000), true);
      history.refreshHistory();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not retry the page."); }
    finally { setRetrying(false); }
  }
  async function del(item: PlayableCapture) {
    if (!window.confirm("Delete this capture and its narration? This cannot be undone.")) return;
    setError(undefined);
    try { await remove(item); }
    catch { setError("Could not delete the capture. Please try again."); }
  }
  return <section className="recent-captures" aria-label="Capture history">
    <div className="section-heading"><h2>Book pages</h2><div className="camera-controls"><button disabled={disabled || !history.items.length} onClick={() => play(0)}>Slide Play</button><button onClick={history.refreshHistory}>Refresh</button></div></div>
    {error && <p role="alert">{error}</p>}
    {history.loading ? <p role="status">Loading captures…</p> : history.error ? <p role="alert">Could not load captures. Select Refresh to try again.</p> : !history.items.length && <p>No pages yet. Start the camera to record the first page.</p>}
    <ol>{history.items.map((item, index) => {
      const status = item.state === "UPLOADING" ? "Uploading…" : item.state === "QUEUED" ? "Queued" : item.state === "PROCESSING" ? "Processing…" : item.state === "REJECTED" ? "Retake needed" : item.state === "FAILED" ? "Processing failed" : undefined;
      const isCover = cover?.page_id === item.id && cover.run_id === item.run_id;
      const date = new Date(item.created_at * 1000).toLocaleString();
      return <li key={`${item.run_id}/${item.id}`}><button className="history-play" disabled={disabled} onClick={() => play(index)}>
        <Thumbnail item={item} /><span className="history-description"><strong>Page {index + 1} · {date}</strong>{status && <span>{status}{item.reason && ` · ${item.reason}`}</span>}<span lang={item.language}>{item.narration.length > 100 ? item.narration.slice(0, 99).trimEnd() + "…" : item.narration}</span></span>
      </button>{item.state === "FAILED" && item.retryable && <button disabled={disabled || retrying} onClick={() => void retry(item)}>Retry page</button>}{book.id !== "earlier" && item.thumbnail_url && <button disabled={disabled || Boolean(saving) || isCover} aria-label={isCover ? `Page ${index + 1} is the book cover` : `Use page ${index + 1} as book cover`} onClick={() => void selectCover(item)}>{isCover ? "✓ Book cover" : saving === item ? "Saving…" : "Set as cover"}</button>}<button disabled={disabled || Boolean(status && !item.thumbnail_url)} aria-label={`Review capture from ${date}`} onClick={() => setReview(item)}>Review</button><button disabled={disabled} aria-label={`Delete capture from ${date}`} onClick={() => void del(item)}>Delete</button></li>;
    })}</ol>
    {review && <CaptureReview item={review} close={() => setReview(undefined)} />}
  </section>;
}
