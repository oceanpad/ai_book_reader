import { useEffect, useState } from "react";
import { thumbnailSource } from "../api/client";
import type { PlayableCapture } from "../api/contracts.generated";
import type { useHistory } from "./useHistory";

function Thumbnail({ item }: { item: PlayableCapture }) {
  const [source, setSource] = useState<string>();
  useEffect(() => {
    setSource(undefined);
    if (!item.thumbnail_url) return;
    const request = new AbortController();
    let owned: string | undefined;
    void thumbnailSource(item.thumbnail_url, item.run_id, request.signal).then(url => {
      if (url.startsWith("blob:")) owned = url;
      if (!request.signal.aborted) setSource(url);
      else if (owned) URL.revokeObjectURL(owned);
    }).catch(() => {});
    return () => { request.abort(); if (owned) URL.revokeObjectURL(owned); };
  }, [item.thumbnail_url, item.run_id]);
  return source ? <img alt="" src={source} referrerPolicy="no-referrer" onError={() => setSource(undefined)} /> : <span className="thumbnail-placeholder" aria-hidden="true">Page</span>;
}

type Props = { history: ReturnType<typeof useHistory>; disabled: boolean; play: (item: PlayableCapture) => void; remove: (item: PlayableCapture) => Promise<void> };
export function HistoryCards({ history, disabled, play, remove }: Props) {
  const [error, setError] = useState<string>();
  async function del(item: PlayableCapture) {
    if (!window.confirm("Delete this capture and its narration? This cannot be undone.")) return;
    setError(undefined);
    try { await remove(item); }
    catch { setError("Could not delete the capture. Please try again."); }
  }
  return <section className="recent-captures" aria-label="Capture history">
    <div className="section-heading"><h2>{history.older ? "Saved captures" : "Recent captures"}</h2><button onClick={history.retry}>Refresh</button></div>
    {error && <p role="alert">{error}</p>}
    {history.loading ? <p role="status">Loading captures…</p> : history.error ? <p role="alert">Could not load captures. Select Refresh to try again.</p> : !history.items.length && <p>No captures on this page.</p>}
    <ol>{history.items.map(item => {
      const date = new Date(item.created_at * 1000).toLocaleString();
      return <li key={`${item.run_id}/${item.id}`}><button className="history-play" disabled={disabled} onClick={() => play(item)}>
        <Thumbnail item={item} /><span className="history-description"><strong>Play · {date}</strong><span lang={item.language}>{item.narration.length > 100 ? item.narration.slice(0, 99).trimEnd() + "…" : item.narration}</span></span>
      </button><button disabled={disabled} aria-label={`Delete capture from ${date}`} onClick={() => void del(item)}>Delete</button></li>;
    })}</ol>
    <nav className="camera-controls" aria-label="Capture history pages">
      {history.older && <><button onClick={history.refreshHistory}>Latest 10</button><button onClick={history.previous}>Previous page</button></>}
      {history.hasNext && <button onClick={history.next}>{history.older ? "Next page" : "Browse all captures"}</button>}
    </nav>
  </section>;
}
