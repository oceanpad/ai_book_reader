import { useCallback, useEffect, useRef, useState } from "react";
import { BusyDialog } from "../reader/BusyDialog";
import { HistoryCards } from "../reader/HistoryCards";
import type { PublicConfig } from "../api/contracts.generated";
import { useReading } from "../reader/useReading";
import { captureAndStop, FRAMES } from "./frame";
import { cameraError, useCamera } from "./useCamera";

export function Camera({ config: publicConfig }: { config: PublicConfig }) {
  const config = publicConfig.camera;
  const canRead = publicConfig.recognition_available && publicConfig.speech_available;
  const camera = useCamera();
  const reading = useReading();
  const captureVersion = useRef(0);
  const [previewOnlyDone, setPreviewOnlyDone] = useState(false);
  const [capturing, setCapturing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const cancelFrame = useCallback(() => {
    captureVersion.current += 1;
    setCapturing(false);
  }, []);
  useEffect(() => {
    const hidden = () => { if (document.hidden) cancelFrame(); };
    document.addEventListener("visibilitychange", hidden);
    return () => { captureVersion.current += 1; document.removeEventListener("visibilitychange", hidden); };
  }, [cancelFrame]);

  async function takeCapture() {
    const video = camera.videoRef.current;
    if (!video || camera.status !== "live") return;
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
      if (version === captureVersion.current) setCapturing(false);
    }
  }

  const live = camera.status === "live";
  const starting = camera.status === "starting";
  const busy = capturing || reading.busy;
  const processing = busy || reading.cleaning || reading.deleting;
  const stage = capturing ? "Capturing the page…" : !reading.job ? "Preparing the reading…" : reading.job.state === "READY" ? "Loading audio…" : reading.job.stage === "speech" ? "Generating narration…" : "Recognizing the page…";
  return (
    <section className="camera-card" aria-label="Book camera and narration">
      <h1>Read a book page</h1>
      {busy && <BusyDialog message={stage} onStop={() => { cancelFrame(); reading.interrupt(); }} />}
      <div hidden={!live && !starting} className="viewfinder" style={{ aspectRatio: camera.aspect, maxWidth: `calc((100svh - 10rem) * ${camera.aspect})` }}>
        <video ref={camera.videoRef} muted playsInline onResize={camera.resize} aria-label="Live camera preview" />
        {live && (
          <div className="frame-guide" style={{ width: `${FRAMES.spread.width * 100}%`, height: `${FRAMES.spread.height * 100}%` }}><span>Fill the frame with the page; keep every printed line visible</span></div>
        )}
      </div>
      <div className="camera-controls">
        {!live && <button className="primary" disabled={starting || processing} onClick={() => { setError(null); setPreviewOnlyDone(false); if (reading.playing) reading.pause(); void camera.start(); }}>{starting ? "Starting..." : camera.status === "paused" ? "Continue" : camera.status === "error" ? "Try Camera Again" : "Start Reading"}</button>}
        {live && <button className="primary capture-button" aria-busy={processing} disabled={processing || reading.cleanupFailed} onClick={() => void takeCapture()}>Submit</button>}
        {live && <button disabled={processing} onClick={() => void camera.start(camera.facing === "environment" ? "user" : "environment")}>Switch Camera</button>}
      </div>
      {(camera.error || error) && <p className="error" role="alert">{camera.error || error}</p>}
      {reading.error && <p className="error" role="alert">{reading.error}</p>}
      {reading.cleanupFailed && <button onClick={() => void reading.reset()}>Retry Discard</button>}
      <audio ref={reading.audioRef} onEnded={reading.ended} onError={reading.playbackError} preload="auto" />
      <p className="hint">{canRead ? "Start opens the camera without a microphone. Submit captures the framed page, turns off the camera, and requests AI-generated narration." : "Camera preview only. Configure server credentials to enable recognition and narration."}</p>
      {publicConfig.setup_message && <p className="setup-message" role="status">{publicConfig.setup_message}</p>}
      {previewOnlyDone && <p role="status">Page captured. The camera is off. Narration is not configured.</p>}
      {(reading.job || reading.error) && (
        <div className="capture-result">
          <div className="section-heading"><h3>Selected capture</h3>{reading.job?.state !== "READY" && !reading.cleanupFailed && <button disabled={processing} onClick={() => void reading.reset()}>Discard</button>}</div>
          {reading.job?.narration && <div className="narration"><h3>{reading.job.language ? `Page narration (${reading.job.language})` : "Page narration"}</h3><p lang={reading.job.language}>{reading.job.narration}</p></div>}
          {reading.job?.reason && <p className="error" role="alert">{reading.job.reason}</p>}
          {((reading.job?.state === "FAILED" && reading.job.retryable) || reading.error) && !processing && !reading.cleanupFailed && <button onClick={() => void reading.recover()}>{reading.job?.stage === "speech" ? "Retry Audio" : "Retry / Check Status"}</button>}
          {reading.audioReady && <div className="camera-controls">
            {!reading.finished && <button className="primary" onClick={() => reading.playing ? reading.pause() : void reading.play()}>{reading.playing ? "Pause" : "Play"}</button>}
            <button onClick={reading.replay}>Replay</button>
          </div>}
          {reading.playbackMessage && <p role="status">{reading.playbackMessage}</p>}
          {reading.job?.state === "READY" && <p className="hint">AI-generated voice / {reading.job.vision_model} / OCR {reading.job.prompt_version} / {reading.job.speech_model} / {reading.job.voice}. Evaluation settings, not yet quality-certified.</p>}
        </div>
      )}
      <HistoryCards history={reading.history} disabled={processing || reading.cleanupFailed} remove={reading.remove} play={item => { camera.stop(); setError(null); setPreviewOnlyDone(false); void reading.selectHistory(item); }} />
    </section>
  );
}
