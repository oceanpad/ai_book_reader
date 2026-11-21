import { useCallback, useEffect, useRef, useState } from "react";
import { discardReading, readingAudio, readingRequest, submitReading } from "../api/client";
import { pendingState, useHistory } from "./useHistory";
import type { PlayableCapture, ReadingJob } from "../api/contracts.generated";

type SelectedCapture = { run: string; id: string; frame?: { image: Blob; mode: string }; pending?: boolean; checking?: boolean };

export function useReading(run: string, bookId: string, poll = true) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const current = useRef<SelectedCapture | null>(null);
  const history = useHistory(run, bookId, observeHistory, poll);
  const { refreshHistory } = history;
  const [deleting, setDeleting] = useState(false);
  const generation = useRef(0);
  const playbackGeneration = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const urls = useRef<string[]>([]);
  const chunkIndex = useRef(0);
  const paused = useRef(false);
  const requestBusy = useRef(false);
  const [job, setJob] = useState<ReadingJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const [cleanupFailed, setCleanupFailed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [playbackMessage, setPlaybackMessage] = useState<string | null>(null);
  const [audioReady, setAudioReady] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [finished, setFinished] = useState(false);

  const play = useCallback(async () => {
    const audio = audioRef.current;
    if (!audio?.src || document.hidden) return;
    const version = generation.current;
    const playback = ++playbackGeneration.current;
    paused.current = false;
    setPlaybackMessage(null);
    try {
      await audio.play();
      if (version === generation.current && playback === playbackGeneration.current) setPlaying(!audio.paused);
    } catch (cause) {
      if (version !== generation.current || playback !== playbackGeneration.current) return;
      paused.current = true;
      setPlaying(false);
      setPlaybackMessage(cause instanceof DOMException && cause.name === "NotAllowedError"
        ? "Your browser needs another tap. Select Play to hear this page."
        : "Audio playback did not start. Select Replay or try another browser.");
    }
  }, []);

  const pause = useCallback(() => {
    playbackGeneration.current += 1;
    paused.current = true;
    audioRef.current?.pause();
    setPlaying(false);
    setPlaybackMessage("Paused. Select Play to continue.");
  }, []);

  const clear = useCallback(() => {
    generation.current += 1;
    playbackGeneration.current += 1;
    controller.current?.abort();
    const audio = audioRef.current;
    if (audio) { audio.pause(); audio.removeAttribute("src"); audio.load(); }
    for (const url of urls.current) URL.revokeObjectURL(url);
    urls.current = [];
    chunkIndex.current = 0;
    const previous = current.current;
    current.current = null;
    return previous;
  }, []);

  const reset = useCallback(async (discard = true): Promise<boolean> => {
    const previous = clear();
    const version = generation.current;
    requestBusy.current = false;
    setJob(null);
    setBusy(false);
    setError(null);
    setPlaybackMessage(null);
    setAudioReady(false);
    setPlaying(false);
    setFinished(false);
    setCleanupFailed(false);
    setCleaning(Boolean(discard && previous));
    try {
      if (previous && discard) {
        await discardReading(previous.run, previous.id);
        refreshHistory();
      }
      if (version !== generation.current) return false;
      setCleaning(false);
      return true;
    } catch (cause) {
      if (version !== generation.current) return false;
      current.current = previous;
      setCleaning(false);
      setCleanupFailed(true);
      setError(cause instanceof Error ? `Could not discard the server capture: ${cause.message}` : "Server cleanup failed. Retry Discard.");
      return false;
    }
  }, [clear, refreshHistory]);

  // History owns the only poll timer. Fetch full details once a selected page
  // leaves the queue (or if a legacy/lagging listing does not include it).
  function observeHistory(items?: PlayableCapture[]) {
    const selected = current.current;
    const signal = controller.current?.signal;
    if (!selected || !signal || signal.aborted || requestBusy.current) return false;
    const entry = items?.find(item => item.run_id === selected.run && item.id === selected.id);
    // Retrying the selected failed page from its history card also updates the selection.
    if (!selected.pending && job?.id === selected.id && job.state === "FAILED" && entry && entry.state !== "FAILED" && !error) selected.pending = true;
    if (!selected.pending) return false;
    if (items && !selected.checking) {
      if (entry?.state && pendingState(entry.state)) {
        const state = entry.state;
        setJob(old => old?.id === selected.id && old.state !== state ? { ...old, state } : old);
      } else {
        const version = generation.current;
        selected.checking = true;
        void readingRequest(selected.run, selected.id, signal).then(result => watch(result, version, signal))
          .catch(cause => fail(cause, version)).finally(() => { selected.checking = false; });
      }
    }
    return true;
  }

  async function watch(result: ReadingJob, version: number, signal: AbortSignal): Promise<void> {
    const selected = current.current;
    if (version !== generation.current || !selected) return;
    setJob(result);
    selected.pending = pendingState(result.state);
    selected.frame = undefined;
    requestBusy.current = false;
    setBusy(false);
    if (result.state !== "READY") return;
    setPlaybackMessage("Loading audio...");
    for (let index = 0; index < result.audio_chunks; index += 1) {
      const blob = await readingAudio(selected.run, result.id, index, signal);
      if (version !== generation.current) return;
      urls.current.push(URL.createObjectURL(blob));
    }
    if (!urls.current.length || !audioRef.current) throw new Error("No audio was returned for this page. Retry the capture.");
    audioRef.current.src = urls.current[0];
    setAudioReady(true);
    if (document.hidden || paused.current) {
      setPlaybackMessage("Audio is ready. Select Play when you are ready.");
    } else await play();
  }

  function fail(cause: unknown, version: number) {
    if (version !== generation.current) return;
    if (current.current) current.current.pending = false;
    requestBusy.current = false;
    setBusy(false);
    setPlaybackMessage(null);
    setError(cause instanceof Error ? cause.message : "The reading request failed. Check the connection and try again.");
  }

  async function openReading(capture: SelectedCapture) {
    if (requestBusy.current) return;
    void reset(false);
    const version = generation.current;
    requestBusy.current = true;
    const request = new AbortController();
    controller.current = request;
    paused.current = document.hidden;
    setBusy(true);
    current.current = capture;
    if (request.signal.aborted || document.hidden) {
      fail(new Error("Reading paused. Select Retry / Check Status to continue."), version);
      return;
    }
    try {
      const result = capture.frame
        ? await submitReading(capture.run, capture.id, capture.frame.image, capture.frame.mode, request.signal, bookId)
        : await readingRequest(capture.run, capture.id, request.signal);
      if (version !== generation.current) return;
      refreshHistory();
      await watch(result, version, request.signal);
    } catch (cause) { fail(cause, version); }
  }

  function submit(image: Blob, mode: string) {
    return openReading({ run, id: crypto.randomUUID(), frame: { image, mode } });
  }

  async function remove(item: PlayableCapture) {
    const selected = current.current;
    setDeleting(true);
    try {
      if (selected?.id === item.id && selected.run === item.run_id) {
        if (!await reset()) throw new Error("Could not delete the selected capture.");
      } else {
        await discardReading(item.run_id, item.id);
        refreshHistory();
      }
    } finally { setDeleting(false); }
  }

  const interrupt = useCallback(() => {
    if (!requestBusy.current) return;
    controller.current?.abort();
    generation.current += 1;
    requestBusy.current = false;
    setBusy(false);
    setPlaybackMessage(null);
    setError("Reading paused. Select Retry / Check Status to continue.");
  }, []);

  async function recover(updated?: ReadingJob) {
    const capture = current.current;
    if (!capture || requestBusy.current || (updated && capture.id !== updated.id)) return;
    requestBusy.current = true;
    controller.current?.abort();
    capture.pending = false;
    capture.checking = false;
    controller.current = new AbortController();
    const version = ++generation.current;
    setError(null);
    setBusy(true);
    setAudioReady(false);
    setPlaying(false);
    setFinished(false);
    if (audioRef.current) { audioRef.current.pause(); audioRef.current.removeAttribute("src"); audioRef.current.load(); }
    // A failed media fetch must not append old chunks or regenerate paid audio.
    for (const url of urls.current) URL.revokeObjectURL(url);
    urls.current = [];
    chunkIndex.current = 0;
    try {
      const result = updated ?? (!job && capture.frame
        ? await submitReading(capture.run, capture.id, capture.frame.image, capture.frame.mode, controller.current.signal, bookId)
        : await readingRequest(capture.run, capture.id, controller.current.signal, job?.state === "FAILED"));
      if (version !== generation.current) return;
      refreshHistory();
      await watch(result, version, controller.current.signal);
    } catch (cause) { fail(cause, version); }
  }

  function replay() {
    if (!audioRef.current || !urls.current.length) return;
    chunkIndex.current = 0;
    audioRef.current.src = urls.current[0];
    audioRef.current.currentTime = 0;
    setFinished(false);
    void play();
  }

  function ended() {
    if (!urls.current.length || !audioRef.current?.ended || paused.current || document.hidden) return;
    chunkIndex.current += 1;
    if (chunkIndex.current < urls.current.length && audioRef.current) {
      audioRef.current.src = urls.current[chunkIndex.current];
      void play();
    } else {
      setPlaying(false);
      setFinished(true);
      setPlaybackMessage("Page finished. Replay or capture the next page.");
    }
  }

  useEffect(() => {
    const hidden = () => { if (document.hidden) { pause(); interrupt(); } };
    document.addEventListener("visibilitychange", hidden);
    return () => document.removeEventListener("visibilitychange", hidden);
  }, [pause, interrupt]);

  useEffect(() => () => { clear(); }, [clear]);

  return { audioRef, job, busy, cleaning, cleanupFailed, error, playbackMessage, audioReady, playing, finished,
    history, deleting, remove, interrupt, submit, reset, recover, play, pause, replay, ended,
    playbackError: () => { setPlaying(false); setPlaybackMessage("This browser could not decode the audio. Select Replay or capture another page."); } };
}
