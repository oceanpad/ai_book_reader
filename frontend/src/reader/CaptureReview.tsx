import { useEffect, useRef, useState } from "react";
import { readingOriginal } from "../api/client";
import type { PlayableCapture } from "../api/contracts.generated";

export function CaptureReview({ item, close }: { item: PlayableCapture; close: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [source, setSource] = useState<string>();
  const [error, setError] = useState<string>();
  useEffect(() => {
    const dialog = ref.current!;
    const request = new AbortController();
    let url: string | undefined;
    dialog.showModal();
    void readingOriginal(item.run_id, item.id, request.signal).then(blob => {
      if (request.signal.aborted) return;
      url = URL.createObjectURL(blob);
      setSource(url);
    }).catch(reason => {
      if (!request.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not load this image.");
    });
    return () => { request.abort(); dialog.close(); if (url) URL.revokeObjectURL(url); };
  }, [item.run_id, item.id]);
  return <dialog ref={ref} className="capture-review" aria-label="Review capture" onCancel={event => { event.preventDefault(); close(); }}>
    <div className="section-heading"><h2>Review capture</h2><button onClick={close}>Close</button></div>
    {error ? <p role="alert">{error}</p> : source ? <img src={source} alt="Original captured page" onError={() => setError("Could not display this image.")} /> : <p role="status">Loading image…</p>}
  </dialog>;
}
