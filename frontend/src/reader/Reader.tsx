import { useEffect, useState } from "react";
import { getConfig } from "../api/client";
import type { PublicConfig } from "../api/contracts.generated";
import { Camera } from "../camera/Camera";
import { authEnabled, signOut } from "../auth/session";

export function Reader() {
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    void getConfig(controller.signal).then(setConfig).catch((cause: unknown) => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not connect to the reader service.");
    });
    return () => controller.abort();
  }, [retry]);

  return (
    <>
      <header className="site-header"><a className="wordmark" href="/">little pages</a>{authEnabled ? <button onClick={signOut}>Sign Out</button> : <span className="prototype-tag">LOCAL PROTOTYPE</span>}</header>
      <main>
        <div className="reader-layout">
          <div>
            {error ? <section className="camera-card" role="alert"><h2>Let us reconnect</h2><p>{error}</p><button onClick={() => setRetry((value) => value + 1)}>Try Again</button></section> : config ? <Camera config={config} /> : <p role="status">Connecting to the reader service...</p>}
          </div>
          <aside aria-label="Reading information">
            <section className="note-card"><span className="eyebrow">A MOMENT TOGETHER</span><h2>Ready when<br />your book is.</h2><ol><li>Find a well-lit, comfortable spot.</li><li>Open a book and frame its pages.</li><li>Capture, listen, then capture again.</li></ol><p className="small">Printed words stay in their original language. Picture-only pages receive a short illustration description. Unreadable pages request a retake.</p></section>
            <section className="privacy-card"><h2>Just the page. Just for you.</h2><p>Submit sends the framed image and narration text to OpenRouter and its routed providers for recognition and speech. Their processing and retention policies apply.</p><p>{authEnabled ? "Your signed-in captures, thumbnails, and narration are saved to your account until you delete them." : "Local captures and narration are temporary, held in memory for this reading run. You can delete them sooner."} Recent captures shows the latest 10 first.</p><span className="voice-disclosure">Narration uses an AI-generated voice.</span></section>
          </aside>
        </div>
      </main>
      <footer>Made for unhurried stories.<span>M1 / manual recognition and narration evaluation</span></footer>
    </>
  );
}
