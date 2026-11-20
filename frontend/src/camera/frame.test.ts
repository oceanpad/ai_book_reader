import { afterEach, describe, expect, test, vi } from "vitest";
import { captureAndStop, captureFrame, frameBounds, outputSize } from "./frame";
import { cameraError } from "./useCamera";

const video = { videoWidth: 2000, videoHeight: 1000, readyState: 2 } as HTMLVideoElement;
const limits = { min_crop_width: 300, min_crop_height: 250, max_longest_edge: 1200, max_upload_bytes: 10 };
const bytes = Uint8Array.of(0xff, 0xd8, 0xff, 0xe0);

function mockCanvas(encode: () => Blob | null, dataUrl = "data:,") {
  const createCanvas = () => ({
    width: 0, height: 0, drawImage: vi.fn(), getContext() { return this; },
    toBlob: vi.fn((callback: BlobCallback) => callback(encode())),
    toDataURL: vi.fn(() => dataUrl),
  });
  const canvases: ReturnType<typeof createCanvas>[] = [];
  vi.stubGlobal("document", { createElement() {
    const canvas = createCanvas();
    canvases.push(canvas);
    return canvas;
  } });
  return canvases;
}

afterEach(() => vi.unstubAllGlobals());

describe("JPEG capture", () => {
  test("accepts a JPEG without MIME metadata and freezes at the capped dimensions", async () => {
    const canvases = mockCanvas(() => new Blob([bytes]));
    const result = await captureFrame(video, limits);
    expect(result).toMatchObject({ mode: "spread", width: 1200, height: 500, blob: { type: "image/jpeg" } });
    expect(canvases).toHaveLength(1);
    expect(canvases[0].drawImage).toHaveBeenCalledWith(video, 100, 125, 1800, 750, 0, 0, 1200, 500);
    expect(canvases[0].toDataURL).not.toHaveBeenCalled();
    expect(canvases[0]).toMatchObject({ width: 0, height: 0 });
  });

  test("Submit stops the camera after freezing pixels, without waiting on the JPEG encoder", async () => {
    let finish!: BlobCallback;
    const drawImage = vi.fn();
    vi.stubGlobal("document", { createElement: () => ({ getContext: () => ({ drawImage }), toBlob: (cb: BlobCallback) => { finish = cb; } }) });
    const stop = vi.fn(() => expect(drawImage).toHaveBeenCalledTimes(1));
    const pending = captureAndStop(video, limits, stop);
    expect(stop).toHaveBeenCalledOnce();
    finish(new Blob([bytes]));
    expect((await pending).blob.size).toBe(bytes.length);
  });

  test("Submit also stops the camera when the frame is invalid", async () => {
    const stop = vi.fn();
    await expect(captureAndStop({ ...video, readyState: 0 } as HTMLVideoElement, limits, stop)).rejects.toThrow("not ready");
    expect(stop).toHaveBeenCalledOnce();
  });

  test("recovers from a null Safari encoder result", async () => {
    mockCanvas(() => null, "data:image/jpeg;base64,/9j/4A==");
    const { blob } = await captureFrame(video, "single", limits);
    expect(new Uint8Array(await blob.arrayBuffer())).toEqual(bytes);
    expect(blob.type).toBe("image/jpeg");
  });

  test("rejects non-JPEG bytes even if labelled JPEG, and releases the canvas", async () => {
    const canvases = mockCanvas(() => new Blob(["not JPEG"], { type: "image/jpeg" }), "data:image/png;base64,iVBORw==");
    await expect(captureFrame(video, limits)).rejects.toThrow("could not encode");
    expect(canvases[0]).toMatchObject({ width: 0, height: 0 });
  });

  test("downsizes the frozen snapshot when lowering quality is insufficient", async () => {
    let calls = 0;
    const canvases = mockCanvas(() => ++calls <= 3 ? new Blob([bytes, new Uint8Array(20)]) : new Blob([bytes]));
    expect(await captureFrame(video, limits)).toMatchObject({ width: 1020, height: 425 });
    expect(canvases[0].drawImage).toHaveBeenCalledTimes(1);
    expect(canvases[1].drawImage).toHaveBeenCalledWith(canvases[0], 0, 0, 1020, 425);
    expect(canvases.every(canvas => !canvas.width && !canvas.height)).toBe(true);
  });
});

describe("framing", () => {
  test("captures exactly the centered guide in either mode", () => {
    expect(frameBounds(2000, 1000, "spread")).toEqual({ x: 100, y: 125, width: 1800, height: 750 });
    expect(frameBounds(2000, 1000, "single")).toEqual({ x: 400, y: 125, width: 1200, height: 750 });
    expect(() => frameBounds(0, 0, "spread")).toThrow("not ready");
  });

  test("caps landscape and portrait resolution without enlarging smaller images", () => {
    expect(outputSize(4000, 2000, 2560)).toEqual({ width: 2560, height: 1280 });
    expect(outputSize(2000, 4000, 2560)).toEqual({ width: 1280, height: 2560 });
    expect(outputSize(800, 600, 2560)).toEqual({ width: 800, height: 600 });
  });

  test("provides permission and hardware recovery messages", () => {
    expect(cameraError(new DOMException("denied", "NotAllowedError"))).toContain("browser settings");
    expect(cameraError(new DOMException("missing", "NotFoundError"))).toContain("No camera");
    expect(cameraError(new DOMException("busy", "NotReadableError"))).toContain("busy");
  });
});
