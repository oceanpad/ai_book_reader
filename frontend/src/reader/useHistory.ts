import { useCallback, useEffect, useState } from "react";
import { recentReadings } from "../api/client";
import type { RecentCaptures } from "../api/contracts.generated";

export function useHistory(run: string) {
  const [cursors, setCursors] = useState<string[]>([]);
  const cursor = cursors.at(-1);
  const [revision, setRevision] = useState(0);
  const [page, setPage] = useState<{ cursor?: string; revision: number; result?: RecentCaptures }>();
  const refreshHistory = useCallback(() => {
    setCursors([]);
    setRevision(value => value + 1);
  }, []);

  useEffect(() => {
    const request = new AbortController();
    void recentReadings(run, request.signal, cursor).then(result => {
      if (!request.signal.aborted) setPage({ cursor, revision, result });
    }).catch(() => {
      if (!request.signal.aborted) setPage({ cursor, revision });
    });
    return () => request.abort();
  }, [run, revision, cursor]);

  const current = page?.revision === revision && page.cursor === cursor ? page : undefined;
  const next = current?.result?.next_cursor;
  return {
    items: current?.result?.items ?? [], loading: !current, error: Boolean(current && !current.result),
    older: cursors.length > 0, hasNext: Boolean(next), refreshHistory,
    retry: () => setRevision(value => value + 1),
    next: () => { if (next) setCursors(previous => [...previous, next]); },
    previous: () => setCursors(previous => previous.slice(0, -1)),
  };
}
