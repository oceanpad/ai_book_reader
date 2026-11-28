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
    const result = await captureFrame(video, "spread", limits);
    expect(result).toMatchObject({ mode: "spread", width: 1200, height: 774, blob: { type: "image/jpeg" } });
    expect(canvases).toHaveLength(1);
    expect(canvases[0].drawImage).toHaveBeenCalledWith(video, 225, 0, 1550, 1000, 0, 0, 1200, 774);
    expect(canvases[0].toDataURL).not.toHaveBeenCalled();
    expect(canvases[0]).toMatchObject({ width: 0, height: 0 });
  });

  test("crops tall camera input to the centered portrait book preview", async () => {
    const portrait = { videoWidth: 1080, videoHeight: 1920, readyState: 2 } as HTMLVideoElement;
    const canvases = mockCanvas(() => new Blob([bytes]));
    await captureFrame(portrait, "single", limits);
    expect(canvases[0].drawImage).toHaveBeenCalledWith(portrait, 0, 251, 1080, 1417, 0, 0, 915, 1200);
  });

  test("Submit stops the camera after freezing pixels, without waiting on the JPEG encoder", async () => {
    let finish!: BlobCallback;
    const drawImage = vi.fn();
    vi.stubGlobal("document", { createElement: () => ({ getContext: () => ({ drawImage }), toBlob: (cb: BlobCallback) => { finish = cb; } }) });
    const stop = vi.fn(() => expect(drawImage).toHaveBeenCalledTimes(1));
    const pending = captureAndStop(video, "spread", limits, stop);
    expect(stop).toHaveBeenCalledOnce();
    finish(new Blob([bytes]));
    expect((await pending).blob.size).toBe(bytes.length);
  });

  test("Submit also stops the camera when the frame is invalid", async () => {
    const stop = vi.fn();
    await expect(captureAndStop({ ...video, readyState: 0 } as HTMLVideoElement, "spread", limits, stop)).rejects.toThrow("not ready");
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
    await expect(captureFrame(video, "spread", limits)).rejects.toThrow("could not encode");
    expect(canvases[0]).toMatchObject({ width: 0, height: 0 });
  });

  test("downsizes the frozen snapshot when lowering quality is insufficient", async () => {
    let calls = 0;
    const canvases = mockCanvas(() => ++calls <= 3 ? new Blob([bytes, new Uint8Array(20)]) : new Blob([bytes]));
    expect(await captureFrame(video, "spread", limits)).toMatchObject({ width: 1020, height: 657 });
    expect(canvases[0].drawImage).toHaveBeenCalledTimes(1);
    expect(canvases[1].drawImage).toHaveBeenCalledWith(canvases[0], 0, 0, 1020, 657);
    expect(canvases.every(canvas => !canvas.width && !canvas.height)).toBe(true);
  });
});

describe("framing", () => {
  test.each([
    ["single", 2000, 1000, { x: 619, y: 0, width: 761, height: 1000 }],
    ["spread", 2000, 1000, { x: 225, y: 0, width: 1550, height: 1000 }],
    ["single", 1080, 1920, { x: 0, y: 251, width: 1080, height: 1417 }],
    ["spread", 1080, 1920, { x: 0, y: 612, width: 1080, height: 696 }],
  ] as const)("centers the %s preview in a %s by %s camera stream", (mode, width, height, bounds) => {
    expect(frameBounds(width, height, mode)).toEqual(bounds);
  });

  test("rejects camera input that is not ready", () => {
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
