import { useEffect, useState } from "react";
import { thumbnailSource } from "../api/client";

export function Thumbnail({ item, placeholder = "Page" }: { item: { thumbnail_url: string | null; run_id: string }; placeholder?: string }) {
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
  return source ? <img alt="" src={source} referrerPolicy="no-referrer" onError={() => setSource(undefined)} /> : <span className="thumbnail-placeholder" aria-hidden="true">{placeholder}</span>;
}
