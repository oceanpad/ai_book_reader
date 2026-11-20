import { useCallback, useEffect, useRef, useState } from "react";

type CameraStatus = "idle" | "starting" | "live" | "paused" | "error";
type Facing = "environment" | "user";

export function cameraError(error: unknown): string {
  if (error instanceof DOMException) {
    if (error.name === "NotAllowedError") return "Camera permission was denied. Allow camera access in browser settings, then try again.";
    if (error.name === "NotFoundError") return "No camera was found. Connect a camera and try again.";
    if (error.name === "NotReadableError") return "The camera is busy. Close other camera apps and try again.";
  }
  return error instanceof Error ? error.message : "The camera could not start. Try again.";
}

export function useCamera() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const generation = useRef(0);
  const [status, setStatus] = useState<CameraStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [facing, setFacing] = useState<Facing>("environment");
  const [aspect, setAspect] = useState(4 / 3);

  const release = useCallback(() => {
    generation.current += 1;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
  }, []);

  const stop = useCallback((next: CameraStatus = "idle") => {
    release();
    setStatus(next);
    setError(null);
  }, [release]);

  const start = useCallback(async (direction: Facing = facing) => {
    release();
    const current = generation.current;
    setError(null);
    setStatus("starting");
    try {
      if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error("Camera access requires HTTPS or localhost and a supported browser.");
      }
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { facingMode: { ideal: direction }, width: { ideal: 3840 }, height: { ideal: 2160 } },
      });
      if (current !== generation.current || document.hidden) {
        stream.getTracks().forEach((track) => track.stop());
        if (current === generation.current) setStatus("paused");
        return;
      }
      streamRef.current = stream;
      const video = videoRef.current;
      if (!video) throw new Error("Camera preview is unavailable. Try again.");
      video.srcObject = stream;
      await video.play();
      if (current !== generation.current) return;
      if (!video.videoWidth || !video.videoHeight) throw new Error("The camera returned no video. Try again.");
      setAspect(video.videoWidth / video.videoHeight);
      setFacing(direction);
      setStatus("live");
      for (const track of stream.getVideoTracks()) {
        track.addEventListener("ended", () => {
          if (current !== generation.current) return;
          release();
          setError("The camera disconnected. Reconnect it and try again.");
          setStatus("error");
        }, { once: true });
      }
    } catch (cause) {
      if (current !== generation.current) return;
      release();
      setError(cameraError(cause));
      setStatus("error");
    }
  }, [facing, release]);

  useEffect(() => {
    const onHidden = () => {
      if (document.hidden && (streamRef.current || status === "starting")) stop("paused");
    };
    document.addEventListener("visibilitychange", onHidden);
    return () => document.removeEventListener("visibilitychange", onHidden);
  }, [status, stop]);

  useEffect(() => release, [release]);
  function resize() {
    const video = videoRef.current;
    if (video?.videoWidth && video.videoHeight) setAspect(video.videoWidth / video.videoHeight);
  }
  return { videoRef, status, error, facing, aspect, start, stop, resize };
}
