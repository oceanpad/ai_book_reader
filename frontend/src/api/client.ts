import type { PublicConfig, ReadingJob, RecentCaptures } from "./contracts.generated";
import { apiFetch, authEnabled } from "../auth/session";

const readings = authEnabled ? "/api/readings" : "/api/local/readings";

async function checked(response: Response): Promise<Response> {
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const message = body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
      ? body.detail : `Reader request failed (${response.status}). Try again.`;
    throw new Error(message);
  }
  return response;
}

export async function submitReading(run: string, id: string, image: Blob, mode: string, signal: AbortSignal): Promise<ReadingJob> {
  const response = await checked(await apiFetch(`${readings}/${id}?mode=${mode}`, {
    method: "POST", body: image, signal,
    headers: { "Content-Type": "image/jpeg", "X-Reader-Run": run },
  }));
  return response.json();
}

export async function readingRequest(run: string, id: string, signal: AbortSignal, retry = false): Promise<ReadingJob> {
  const response = await checked(await apiFetch(`${readings}/${id}${retry ? "/retry" : ""}`, {
    method: retry ? "POST" : "GET", signal, cache: "no-store", headers: { "X-Reader-Run": run },
  }));
  return response.json();
}

export async function recentReadings(run: string, signal: AbortSignal, cursor?: string): Promise<RecentCaptures> {
  const query = cursor === undefined ? "" : `?${new URLSearchParams({ cursor })}`;
  const response = await checked(await apiFetch(readings + query, {
    signal, cache: "no-store", headers: { "X-Reader-Run": run },
  }));
  return response.json();
}

export async function thumbnailSource(url: string, run: string, signal: AbortSignal): Promise<string> {
  if (!authEnabled && url.startsWith("data:image/jpeg;base64,")) return url;
  const target = new URL(url, location.origin);
  if (target.origin !== location.origin) {
    if (!authEnabled || target.protocol !== "https:") throw new Error("Invalid thumbnail URL.");
    // The cloud image policy permits blob URLs. Never send API credentials to signed media URLs.
    const response = await checked(await fetch(target.href, { signal, cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer" }));
    return URL.createObjectURL(await response.blob());
  }
  const response = await checked(await apiFetch(target.pathname + target.search, {
    signal, cache: "no-store", headers: { "X-Reader-Run": run },
  }));
  return URL.createObjectURL(await response.blob());
}

export async function readingAudio(run: string, id: string, chunk: number, signal: AbortSignal): Promise<Blob> {
  const response = await checked(await apiFetch(`${readings}/${id}/audio/${chunk}`, {
    signal, cache: "no-store", headers: { "X-Reader-Run": run },
  }));
  if (!authEnabled) return response.blob();
  const media: unknown = await response.json();
  if (!media || typeof media !== "object" || !("url" in media) || typeof media.url !== "string" || !media.url.startsWith("https://")) throw new Error("The reader returned an invalid private media URL.");
  return (await checked(await fetch(media.url, { signal, cache: "no-store" }))).blob();
}

export async function discardReading(run: string, id: string): Promise<void> {
  await checked(await apiFetch(`${readings}/${id}`, {
    method: "DELETE", keepalive: true, signal: AbortSignal.timeout(20000), headers: { "X-Reader-Run": run },
  }));
}

export async function getConfig(signal: AbortSignal): Promise<PublicConfig> {
  const response = await apiFetch("/api/config", { signal, cache: "no-store" });
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      throw new Error("Sign-in is required. The local prototype does not include Cognito login yet.");
    }
    throw new Error(`The reader service is unavailable (${response.status}). Try again.`);
  }
  return response.json();
}
