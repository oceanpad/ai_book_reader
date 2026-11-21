import { useCallback, useEffect, useRef, useState } from "react";
import { BookHeading } from "../reader/BookHeading";
import { BookSlideshow } from "../reader/BookSlideshow";
import type { Book } from "../api/client";
import { NarrationEditor } from "../reader/NarrationEditor";
import { BusyDialog } from "../reader/BusyDialog";
import { HistoryCards } from "../reader/HistoryCards";
import { DeleteBook } from "../reader/DeleteBook";
import type { PublicConfig } from "../api/contracts.generated";
import { useReading } from "../reader/useReading";
import { pendingState } from "../reader/useHistory";
import { captureAndStop, MIN_PAGE_ASPECT } from "./frame";
import { cameraError, useCamera } from "./useCamera";

type Props = { config: PublicConfig; book: Book; run: string; preview: boolean; back: () => void; onComplete?: () => void };
export function Camera({ config: publicConfig, book: initialBook, run, preview, back, onComplete }: Props) {
  const [book, setBook] = useState(initialBook);
  const config = publicConfig.camera;
  const canRead = publicConfig.recognition_available && publicConfig.speech_available;
  const camera = useCamera();
  const [slide, setSlide] = useState<number | null>(preview ? 0 : null);
  // The slideshow follows its active page itself while the history is covered.
  const reading = useReading(run, book.id, slide === null);
  function openSlides(index: number) {
    camera.stop();
    reading.pause();
    setSlide(index);
  }
  const [editing, setEditing] = useState(false);
  const editAccepted = useRef(false);
  const captureVersion = useRef(0);
  const frameInFlight = useRef(false);
  const [previewOnlyDone, setPreviewOnlyDone] = useState(false);
  const [capturing, setCapturing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const cancelFrame = useCallback(() => {
    captureVersion.current += 1;
    frameInFlight.current = false;
    setCapturing(false);
  }, []);
  useEffect(() => {
    const hidden = () => { if (document.hidden) cancelFrame(); };
    document.addEventListener("visibilitychange", hidden);
    return () => { captureVersion.current += 1; document.removeEventListener("visibilitychange", hidden); };
  }, [cancelFrame]);

  async function takeCapture() {
    const video = camera.videoRef.current;
    if (!video || camera.status !== "live" || frameInFlight.current) return;
    frameInFlight.current = true;
    const version = ++captureVersion.current;
    setCapturing(true);
    setError(null);
    try {
      const image = await captureAndStop(video, config, camera.stop);
      if (version !== captureVersion.current || document.hidden) return;
      if (canRead) void reading.submit(image.blob, image.mode);
      else setPreviewOnlyDone(true);
    } catch (cause) {
      if (version === captureVersion.current) setError(cameraError(cause));
    } finally {
      if (version === captureVersion.current) { frameInFlight.current = false; setCapturing(false); }
    }
  }

  const live = camera.status === "live";
  const starting = camera.status === "starting";
  const busy = capturing || reading.busy;
  const processing = editing || busy || reading.cleaning || reading.deleting;
  const frameAspect = Math.max(camera.aspect, MIN_PAGE_ASPECT);
  const stage = capturing ? "Capturing the page…" : "Sending the reading request…";
  const limit = publicConfig.limits?.max_captures_ahead ?? 10;
  const pending = reading.history.items.filter(item => pendingState(item.state));
  const queueFull = canRead && pending.length >= limit;
  return (
    <section className="camera-card" aria-label="Book camera and narration">
      <button onClick={back} disabled={busy || reading.cleaning || reading.deleting}>← Books</button>
      <BookHeading book={book} run={run} disabled={processing} renamed={name => setBook(current => ({ ...current, name }))} />
      {slide !== null && !reading.history.loading && (!reading.history.error || reading.history.items.length > 0) && <BookSlideshow onComplete={onComplete} pages={reading.history.items} startIndex={slide} changed={page => { if (reading.job?.id === page.id && reading.job.state === "READY") void reading.reset(false); }} close={() => { setSlide(null); reading.history.refreshHistory(); }} />}
      {busy && <BusyDialog message={stage} onStop={() => { cancelFrame(); reading.interrupt(); }} />}
      <div hidden={!live && !starting} className="viewfinder" style={{ aspectRatio: frameAspect, maxWidth: `min(66.67vw, calc(66.67svh * ${frameAspect}))` }}>
        <video ref={camera.videoRef} muted playsInline onResize={camera.resize} aria-label="Live camera preview" />
      </div>
      <div className="camera-controls">
        {!live && <button className="primary" disabled={starting || processing || queueFull} onClick={() => { setSlide(null); setError(null); setPreviewOnlyDone(false); reading.pause(); void camera.start(); }}>{starting ? "Starting..." : camera.status === "paused" ? "Continue" : camera.status === "error" ? "Try Camera Again" : reading.job ? "Capture next page" : "Start Reading"}</button>}
        {live && <button className="primary capture-button" aria-busy={processing} disabled={processing || reading.cleanupFailed || queueFull} onClick={() => void takeCapture()}>Submit</button>}
        {live && <button disabled={processing} onClick={() => void camera.start(camera.facing === "environment" ? "user" : "environment")}>Switch Camera</button>}
      </div>
      {queueFull && <p role="status">The queue is full ({limit} pending pages). Capture another page when one finishes.</p>}
      {(camera.error || error) && <p className="error" role="alert">{camera.error || error}</p>}
      {reading.error && <p className="error" role="alert">{reading.error}</p>}
      {reading.cleanupFailed && <button onClick={() => void reading.reset()}>Retry Discard</button>}
      <audio ref={reading.audioRef} onEnded={reading.ended} onError={reading.playbackError} preload="auto" />
      <p className="hint">{canRead ? "Start opens the camera without a microphone. Submit captures the framed page, turns off the camera, and requests AI-generated narration." : "Camera preview only. Configure server credentials to enable recognition and narration."}</p>
      {publicConfig.setup_message && <p className="setup-message" role="status">{publicConfig.setup_message}</p>}
      {previewOnlyDone && <p role="status">Page captured. The camera is off. Narration is not configured.</p>}
      {(reading.job || reading.error) && (
        <div className="capture-result">
          <div className="section-heading"><h3>Selected capture</h3>
            {reading.job?.state === "READY" ? !editing && <button disabled={processing} onClick={() => {
              camera.stop(); reading.pause(); editAccepted.current = false; setEditing(true);
            }}>Edit text</button> : !reading.cleanupFailed && <button disabled={processing} onClick={() => void reading.reset()}>Discard</button>}
          </div>
          {editing && reading.job ? <NarrationEditor page={{ ...reading.job, run_id: run }} checkAudio={false}
            changed={() => { editAccepted.current = true; reading.history.refreshHistory(); }}
            saved={(_, updated) => { setEditing(false); reading.history.refreshHistory(); void reading.recover(updated); }}
            cancel={() => { setEditing(false); if (editAccepted.current) void reading.recover(); }}
          /> : reading.job?.narration && <div className="narration"><h3>{reading.job.language ? `Page narration (${reading.job.language})` : "Page narration"}</h3><p lang={reading.job.language}>{reading.job.narration}</p></div>}
          {pendingState(reading.job?.state) && <p role="status">Page accepted ({reading.job?.state.toLowerCase()}). You can capture the next page or switch books.</p>}
          {reading.job?.reason && <p className="error" role="alert">{reading.job.reason}</p>}
          {((reading.job?.state === "FAILED" && reading.job.retryable) || reading.error) && !processing && !reading.cleanupFailed && <button disabled={queueFull && (!reading.job || reading.job.state === "FAILED")} onClick={() => void reading.recover()}>{reading.job?.stage === "speech" ? "Retry Audio" : "Retry / Check Status"}</button>}
          {!editing && reading.audioReady && <div className="camera-controls">
            {!reading.finished && <button className="primary" onClick={() => reading.playing ? reading.pause() : void reading.play()}>{reading.playing ? "Pause" : "Play"}</button>}
            <button onClick={reading.replay}>Replay</button>
          </div>}
          {!editing && reading.playbackMessage && <p role="status">{reading.playbackMessage}</p>}
          {reading.job?.state === "READY" && <p className="hint">AI-generated voice / {reading.job.vision_model} / OCR {reading.job.prompt_version} / {reading.job.speech_model} / {reading.job.voice}. Evaluation settings, not yet quality-certified.</p>}
        </div>
      )}
      <HistoryCards book={book} run={run} history={reading.history} disabled={processing || reading.cleanupFailed} remove={reading.remove} play={openSlides} />
      <DeleteBook book={book} run={run} disabled={processing} done={back} />
    </section>
  );
}
