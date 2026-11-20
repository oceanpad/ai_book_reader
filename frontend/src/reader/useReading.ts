import { useCallback, useEffect, useRef, useState } from "react";
import { discardReading, readingAudio, readingRequest, submitReading } from "../api/client";
import { useHistory } from "./useHistory";
import { authEnabled } from "../auth/session";
import type { PlayableCapture, ReadingJob } from "../api/contracts.generated";

type SelectedCapture = { run: string; id: string; frame?: { image: Blob; mode: string }; ready: boolean };

export function useReading() {
  const audioRef = useRef<HTMLAudioElement>(null);
  const run = useRef(crypto.randomUUID());
  const current = useRef<SelectedCapture | null>(null);
  const history = useHistory(run.current);
  const { refreshHistory } = history;
  const [deleting, setDeleting] = useState(false);
  const generation = useRef(0);
  const playbackGeneration = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
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
    if (timer.current) clearTimeout(timer.current);
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
    setCleaning(Boolean(previous));
    try {
      if (previous && (discard || (!authEnabled && !previous.ready))) {
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

  async function watch(result: ReadingJob, version: number, signal: AbortSignal): Promise<void> {
    const selected = current.current;
    if (version !== generation.current || !selected) return;
    setJob(result);
    if (result.state === "UPLOADING" || result.state === "QUEUED" || result.state === "PROCESSING") {
      timer.current = setTimeout(() => {
        void readingRequest(selected.run, result.id, signal).then((next) => watch(next, version, signal)).catch((cause: unknown) => fail(cause, version));
      }, 1000);
      return;
    }
    if (result.state !== "READY") {
      requestBusy.current = false;
      setBusy(false);
      return;
    }
    if (!selected.ready) refreshHistory();
    selected.ready = true;
    selected.frame = undefined;
    setPlaybackMessage("Loading audio...");
    for (let index = 0; index < result.audio_chunks; index += 1) {
      const blob = await readingAudio(selected.run, result.id, index, signal);
      if (version !== generation.current) return;
      urls.current.push(URL.createObjectURL(blob));
    }
    if (!urls.current.length || !audioRef.current) throw new Error("No audio was returned for this page. Retry the capture.");
    audioRef.current.src = urls.current[0];
    setAudioReady(true);
    requestBusy.current = false;
    setBusy(false);
    if (document.hidden || paused.current) {
      setPlaybackMessage("Audio is ready. Select Play when you are ready.");
    } else await play();
  }

  function fail(cause: unknown, version: number) {
    if (version !== generation.current) return;
    requestBusy.current = false;
    setBusy(false);
    setPlaybackMessage(null);
    setError(cause instanceof Error ? cause.message : "The reading request failed. Check the connection and try again.");
  }

  async function openReading(capture: SelectedCapture) {
    if (requestBusy.current) return;
    const cleared = reset(false);
    const version = generation.current;
    requestBusy.current = true;
    const request = new AbortController();
    controller.current = request;
    paused.current = document.hidden;
    setBusy(true);
    if (!await cleared || version !== generation.current) {
      if (version === generation.current) { requestBusy.current = false; setBusy(false); }
      return;
    }
    current.current = capture;
    if (request.signal.aborted || document.hidden) {
      fail(new Error("Reading paused. Select Retry / Check Status to continue."), version);
      return;
    }
    try {
      const result = capture.frame
        ? await submitReading(capture.run, capture.id, capture.frame.image, capture.frame.mode, request.signal)
        : await readingRequest(capture.run, capture.id, request.signal);
      await watch(result, version, request.signal);
    } catch (cause) { fail(cause, version); if (!capture.frame) refreshHistory(); }
  }

  function submit(image: Blob, mode: string) {
    return openReading({ run: run.current, id: crypto.randomUUID(), frame: { image, mode }, ready: false });
  }

  function selectHistory(item: PlayableCapture) {
    return openReading({ run: item.run_id, id: item.id, ready: true });
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
    // If cleanup is still finishing, openReading observes the aborted signal before submitting.
    if (!current.current) { setBusy(false); return; }
    generation.current += 1;
    if (timer.current) clearTimeout(timer.current);
    requestBusy.current = false;
    setBusy(false);
    setPlaybackMessage(null);
    setError("Reading paused. Select Retry / Check Status to continue.");
  }, []);

  async function recover() {
    const capture = current.current;
    if (!capture || requestBusy.current) return;
    requestBusy.current = true;
    controller.current?.abort();
    if (timer.current) clearTimeout(timer.current);
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
    try {
      const result = !job && capture.frame
        ? await submitReading(capture.run, capture.id, capture.frame.image, capture.frame.mode, controller.current.signal)
        : await readingRequest(capture.run, capture.id, controller.current.signal, job?.state === "FAILED");
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

  useEffect(() => () => {
    const previous = clear();
    if (!authEnabled && previous && !previous.ready) void discardReading(previous.run, previous.id).catch((cause: unknown) => {
      console.error("Local reading cleanup failed", cause instanceof Error ? cause.message : "Unknown error");
    });
  }, [clear]);

  return { audioRef, job, busy, cleaning, cleanupFailed, error, playbackMessage, audioReady, playing, finished,
    history, deleting, remove, interrupt, selectHistory, submit, reset, recover, play, pause, replay, ended,
    playbackError: () => { setPlaying(false); setPlaybackMessage("This browser could not decode the audio. Select Replay or capture another page."); } };
}
