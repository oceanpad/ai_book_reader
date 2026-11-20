import { useEffect, useRef, useState } from "react";
import { editNarration, readingRequest } from "../api/client";
import type { PlayableCapture, ReadingJob } from "../api/contracts.generated";

type NarratedPage = Pick<PlayableCapture, "id" | "run_id" | "narration" | "language" | "audio_chunks">;
type Props<T extends NarratedPage> = { page: T; saved: (page: T, job: ReadingJob) => void; cancel: () => void; changed: () => void; checkAudio: boolean };

export async function waitForNarration(page: NarratedPage, job: ReadingJob, signal: AbortSignal) {
  while (["QUEUED", "UPLOADING", "PROCESSING"].includes(job.state)) {
    signal.throwIfAborted();
    await new Promise<void>((resolve, reject) => {
      const abort = () => { clearTimeout(timer); reject(signal.reason); };
      const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, 1000);
      signal.addEventListener("abort", abort, { once: true });
    });
    job = await readingRequest(page.run_id, page.id, signal);
  }
  return job;
}

export function NarrationEditor<T extends NarratedPage>({ page, saved, cancel, changed, checkAudio }: Props<T>) {
  const [draft, setDraft] = useState(page.narration);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [next, setNext] = useState<"save" | "check" | "retry">(checkAudio ? "check" : "save");
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  async function submit() {
    if (active.current) return;
    const controller = new AbortController();
    active.current = controller;
    const { signal } = controller;
    setBusy(true); setError("");
    try {
      const job = next === "save" ? await editNarration(page.run_id, page.id, draft.trim(), signal)
        : await readingRequest(page.run_id, page.id, signal, next === "retry");
      changed(); setNext("check");
      const result = await waitForNarration(page, job, signal);
      signal.throwIfAborted();
      if (result.state !== "READY" || !result.audio_chunks) {
        setNext(result.retryable ? "retry" : "save");
        throw new Error(result.reason || "Audio regeneration failed.");
      }
      saved({ ...page, narration: result.narration, language: result.language || page.language, audio_chunks: result.audio_chunks }, result);
    } catch (cause) {
      if (!signal.aborted) setError(cause instanceof Error ? cause.message : "Could not update narration.");
    } finally { active.current = null; if (!signal.aborted) setBusy(false); }
  }
  return <form className="narration-editor" onSubmit={event => { event.preventDefault(); void submit(); }}>
    <label htmlFor="page-narration">Recognized text</label>
    <textarea id="page-narration" autoFocus rows={6} maxLength={20000} lang={page.language} value={draft} disabled={busy || next !== "save"} onChange={event => setDraft(event.target.value)} />
    <p>Saving replaces this page’s narration with audio generated from your text.</p>
    {busy && <p role="status">Regenerating audio…</p>}{error && <p role="alert">{error}</p>}
    <button className="primary" disabled={busy || (next === "save" && (!draft.trim() || draft.trim() === page.narration.trim()))}>{next === "retry" ? "Retry audio" : next === "check" ? "Check status" : "Save and regenerate audio"}</button>
    {next === "save" && <button type="button" disabled={busy} onClick={cancel}>Cancel</button>}
  </form>;
}
