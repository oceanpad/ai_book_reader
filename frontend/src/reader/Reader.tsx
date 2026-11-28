import { useEffect, useState } from "react";
import { getConfig } from "../api/client";
import type { PublicConfig } from "../api/contracts.generated";
import { BookLibrary } from "./BookLibrary";
import { Brand, ReadingGuide } from "../Studio";
import { authEnabled, signOut } from "../auth/session";
import { Homework } from "../homework/Homework";

function workspaceFromUrl() {
  return new URLSearchParams(window.location.search).get("workspace") === "homework" ? "homework" : "books";
}

export function Reader() {
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [workspace, setWorkspace] = useState(workspaceFromUrl);
  useEffect(() => {
    const restore = () => setWorkspace(workspaceFromUrl());
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, []);

  function openWorkspace(next: typeof workspace) {
    if (workspace === next) return;
    const url = new URL(window.location.href);
    if (next === "homework") url.searchParams.set("workspace", next);
    else url.searchParams.delete("workspace");
    window.history.pushState(null, "", url);
    setWorkspace(next);
  }
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
      <a className="skip-link" href="#workspace">Skip to workspace</a>
      <header className="site-header"><div className="header-inner"><Brand /><div className="header-actions"><ReadingGuide />{authEnabled ? <button onClick={signOut}>Sign Out</button> : <span className="mode-badge">Local session</span>}</div></div></header>
      <main id="workspace" tabIndex={-1}>
        {error ? <section className="camera-card" role="alert"><span className="eyebrow">Connection interrupted</span><h1>Let’s reconnect</h1><p>{error}</p><button className="primary" onClick={() => setRetry(value => value + 1)}>Try again</button></section> : config ? <>
          <nav className="workspace-nav" aria-label="Little Pages workspaces">
            <button type="button" aria-pressed={workspace === "books"} onClick={() => openWorkspace("books")}>My books</button>
            <button type="button" aria-pressed={workspace === "homework"} onClick={() => openWorkspace("homework")}>Homework help</button>
          </nav>
          {workspace === "books" ? <BookLibrary config={config} /> : <Homework enabled={config.recognition_available} />}
        </> : <p className="status-line" role="status"><span className="spinner" />Connecting to the reader service…</p>}
      </main>
      <footer className="studio-footer"><strong>Little Pages</strong><span>{authEnabled ? "Account library" : "Temporary local library"} · AI-generated narration</span></footer>
    </>
  );
}
