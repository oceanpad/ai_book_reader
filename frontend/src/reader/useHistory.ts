import { useCallback, useEffect, useRef, useState } from "react";
import { allBookPages } from "../api/client";
import type { CaptureState, PlayableCapture } from "../api/contracts.generated";

export const pendingState = (state?: CaptureState | null) => state === "UPLOADING" || state === "QUEUED" || state === "PROCESSING";
type Snapshot = { run: string; bookId: string; items: PlayableCapture[]; error: boolean };

export function useHistory(run: string, bookId: string, observe?: (items?: PlayableCapture[]) => boolean, poll = true) {
  const [revision, setRevision] = useState(0);
  const [page, setPage] = useState<Snapshot>();
  const observer = useRef(observe);
  useEffect(() => { observer.current = observe; }, [observe]);
  const pendingBook = useRef<string | undefined>(undefined);
  const refreshHistory = useCallback(() => setRevision(value => value + 1), []);

  useEffect(() => {
    const request = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const key = `${run}/${bookId}`;
    async function load() {
      let items: PlayableCapture[] | undefined;
      try { items = await allBookPages(run, bookId, request.signal); }
      catch { /* Keep the last snapshot while allowing a pending book to reconnect. */ }
      if (request.signal.aborted) return;
      if (items) {
        pendingBook.current = items.some(item => pendingState(item.state)) ? key : undefined;
        setPage({ run, bookId, items, error: false });
      } else {
        setPage(previous => ({ run, bookId, items: previous?.run === run && previous.bookId === bookId ? previous.items : [], error: true }));
      }
      if (!poll) return;
      const selectedPending = observer.current?.(items);
      if (selectedPending || pendingBook.current === key) timer = setTimeout(load, items ? 1000 : 3000);
    }
    void load();
    return () => { request.abort(); clearTimeout(timer); };
  }, [run, bookId, revision, poll]);

  const current = page?.run === run && page.bookId === bookId ? page : undefined;
  return { items: current?.items ?? [], loading: !current, error: Boolean(current?.error), refreshHistory };
}
