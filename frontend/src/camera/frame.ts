import type { CameraConfig } from "../api/contracts.generated";

export const MIN_PAGE_ASPECT = 3 / 4;
export type CaptureMode = "spread" | "single";
const FRAMES = {
  spread: { width: 1, height: 1 },
  single: { width: 0.6, height: 0.75 },
} satisfies Record<CaptureMode, { width: number; height: number }>;

export function frameBounds(width: number, height: number, mode: CaptureMode) {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    throw new Error("The camera is not ready. Wait for the preview before capturing.");
  }
  const cropWidth = Math.floor(width * FRAMES[mode].width);
  const cropHeight = Math.floor(mode === "spread" ? Math.min(height, width / MIN_PAGE_ASPECT) : height * FRAMES[mode].height);
  return {
    x: Math.floor((width - cropWidth) / 2),
    y: Math.floor((height - cropHeight) / 2),
    width: cropWidth,
    height: cropHeight,
  };
}

export function outputSize(width: number, height: number, longestEdge: number) {
  const scale = Math.min(1, longestEdge / Math.max(width, height));
  return { width: Math.round(width * scale), height: Math.round(height * scale) };
}

async function asJpeg(blob: Blob | null) {
  if (!blob || blob.size < 4) return null;
  const header = new Uint8Array(await blob.slice(0, 3).arrayBuffer());
  if (header[0] !== 0xff || header[1] !== 0xd8 || header[2] !== 0xff) return null;
  return blob.type === "image/jpeg" ? blob : blob.slice(0, blob.size, "image/jpeg");
}

async function jpeg(canvas: HTMLCanvasElement, quality: number) {
  if (typeof canvas.toBlob === "function") {
    try {
      const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
      const image = await asJpeg(blob);
      if (image) return image;
    } catch (error) {
      if (error instanceof DOMException && error.name === "SecurityError") throw error;
    }
  }
  // Some WebKit versions fail the asynchronous encoder but can serialize the same canvas.
  const url = canvas.toDataURL("image/jpeg", quality);
  const prefix = "data:image/jpeg;base64,";
  if (url.startsWith(prefix)) {
    const bytes = Uint8Array.from(atob(url.slice(prefix.length)), (char) => char.charCodeAt(0));
    const image = await asJpeg(new Blob([bytes], { type: "image/jpeg" }));
    if (image) return image;
  }
  throw new Error("The camera could not encode this frame as JPEG. Wait for the preview and try again.");
}

type CaptureLimits = Pick<CameraConfig, "min_crop_width" | "min_crop_height" | "max_longest_edge" | "max_upload_bytes">;

export function captureAndStop(video: HTMLVideoElement, limits: CaptureLimits, stop: () => void) {
  // captureFrame copies the pixels before its first await. Encoding needs no live stream.
  const frame = captureFrame(video, limits);
  stop();
  return frame;
}

export async function captureFrame(video: HTMLVideoElement, ...args: [CaptureLimits] | [CaptureMode, CaptureLimits]) {
  const [mode, config] = args.length === 2 ? args : ["spread" as const, args[0]];
  const bounds = frameBounds(video.videoWidth, video.videoHeight, mode);
  if (video.readyState < 2) throw new Error("The camera is not ready. Wait for the preview before capturing.");
  if (bounds.width < config.min_crop_width || bounds.height < config.min_crop_height) {
    throw new Error("The camera resolution is too low. Use a higher-resolution camera.");
  }
  let size = outputSize(bounds.width, bounds.height, config.max_longest_edge);
  const source = document.createElement("canvas");
  let smaller: HTMLCanvasElement | undefined;
  try {
    while (size.width >= config.min_crop_width && size.height >= config.min_crop_height) {
      const canvas = smaller ?? source;
      canvas.width = size.width;
      canvas.height = size.height;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("Image capture is unavailable. Restart the camera and try again.");
      // Freeze directly at the capped size; retries must never read a later video frame.
      if (smaller) context.drawImage(source, 0, 0, size.width, size.height);
      else context.drawImage(video, bounds.x, bounds.y, bounds.width, bounds.height, 0, 0, size.width, size.height);
      for (const quality of [0.92, 0.85, 0.75]) {
        const blob = await jpeg(canvas, quality);
        if (blob.size <= config.max_upload_bytes) return { blob, ...size, mode };
      }
      smaller ??= document.createElement("canvas");
      size = { width: Math.floor(size.width * 0.85), height: Math.floor(size.height * 0.85) };
    }
    throw new Error("The image cannot fit the upload limit without losing detail. Reframe and try again.");
  } finally {
    source.width = source.height = 0;
    if (smaller) smaller.width = smaller.height = 0;
  }
}
