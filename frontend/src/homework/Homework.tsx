import { useEffect, useRef, useState } from "react";
import { deleteHomework, getHomework, homeworkHistory, submitHomework } from "../api/client";
import type { HomeworkEntry, HomeworkQuestion, HomeworkSubmission } from "../api/contracts.generated";
import { encodeWorksheet, maxPhotos } from "./photos";
import "./homework.css";

const subjects = { math: "Math", english: "English", chinese: "Chinese" };
const levels = {
  kindergarten: "Kindergarten",
  primary1: "Primary 1",
  primary2: "Primary 2",
  primary3: "Primary 3",
  primary4: "Primary 4",
  primary5: "Primary 5",
  primary6: "Primary 6",
} satisfies Record<HomeworkSubmission["level"], string>;
const statuses = { pending: "Checking", ready: "Ready", failed: "Couldn’t check" };
const message = (cause: unknown) => cause instanceof Error ? cause.message : "Something went wrong. Try again.";

function useReadOut() {
  const active = useRef<SpeechSynthesisUtterance | null>(null);
  const [speaking, setSpeaking] = useState<number>();
  const [speechError, setSpeechError] = useState(false);
  const supported = typeof window.speechSynthesis !== "undefined" && typeof window.SpeechSynthesisUtterance === "function";
  const cancel = () => {
    if (active.current) {
      active.current = null;
      window.speechSynthesis.cancel();
    }
  };
  useEffect(() => cancel, []);
  function stop() { cancel(); setSpeaking(undefined); setSpeechError(false); }
  function read(question: HomeworkQuestion, index: number) {
    stop();
    if (!supported || speaking === index) return;
    const chinese = question.language === "zh";
    const segments = [chinese ? `答案：${question.answer}` : `Answer: ${question.answer}`, ...question.steps];
    const utterance = new SpeechSynthesisUtterance(segments.join(chinese ? "。 " : ". "));
    utterance.lang = chinese ? "zh-CN" : "en";
    utterance.rate = 0.85;
    utterance.onend = () => { if (active.current === utterance) stop(); };
    utterance.onerror = () => { if (active.current === utterance) { stop(); setSpeechError(true); } };
    active.current = utterance;
    setSpeaking(index);
    try { window.speechSynthesis.speak(utterance); } catch { stop(); setSpeechError(true); }
  }
  return { supported, speaking, speechError, read };
}

export function HomeworkAnswers({ entry }: { entry: HomeworkEntry }) {
  const { supported, speaking, speechError, read } = useReadOut();
  const result = entry.result;
  return <section className="homework-results" aria-labelledby="homework-result-title">
    <h2 id="homework-result-title">Homework result</h2>
    <p className="homework-meta">{subjects[entry.subject]} · {levels[entry.level]} · {new Date(entry.created_at).toLocaleString()}</p>
    {entry.state === "pending" && <p className="status-line" role="status"><span className="spinner" />Checking your photos. You can come back from All History.</p>}
    {entry.error && <p className="error" role="alert">{entry.error}</p>}
    {result && <>
      {result.note && <p className="homework-note">{result.note}</p>}
      {!result.questions.length && !result.note && <p>No readable questions were found. Try a clearer photo.</p>}
      {result.questions.length > 0 && !supported && <p className="homework-meta">Read-aloud isn’t available in this browser.</p>}
      {speechError && <p className="error" role="alert">Couldn’t read this answer aloud. Check your device’s speech voices.</p>}
      <ol className="homework-questions">
        {result.questions.map((question, index) => <li className="camera-card homework-question" key={index}>
          <span className="homework-number" aria-label={`Question ${index + 1}`}>{index + 1}</span>
          <div lang={question.language}>
            <h3>{question.question}</h3>
            <p className="homework-answer"><strong>{question.language === "zh" ? "答案" : "Answer"}</strong> {question.answer}</p>
            {question.steps.length > 0 && <><h4>{question.language === "zh" ? "一步一步来" : "Step by step"}</h4><ol className="homework-steps">{question.steps.map((step, number) => <li key={number}>{step}</li>)}</ol></>}
          </div>
          {supported && <button type="button" onClick={() => read(question, index)}>{speaking === index ? "Stop reading" : "Read out answer"}</button>}
        </li>)}
      </ol>
      <p className="homework-meta">AI can make mistakes. Check anything surprising with a grown-up.</p>
    </>}
  </section>;
}

function mergeEntries(current: HomeworkEntry[], updates: HomeworkEntry[]) {
  const entries = new Map(current.map(entry => [entry.id, entry]));
  for (const entry of updates) {
    if (entry.state !== "pending" || !entries.has(entry.id)) entries.set(entry.id, entry);
  }
  return [...entries.values()].sort((a, b) => b.id.localeCompare(a.id));
}

export function Homework({ enabled }: { enabled: boolean }) {
  const [run] = useState(() => {
    const key = "little-pages-library-run";
    const existing = sessionStorage.getItem(key) ?? crypto.randomUUID();
    sessionStorage.setItem(key, existing);
    return existing;
  });
  const [subject, setSubject] = useState<HomeworkSubmission["subject"]>("math");
  const [level, setLevel] = useState<HomeworkSubmission["level"]>("kindergarten");
  const [files, setFiles] = useState<File[]>([]);
  const [entries, setEntries] = useState<HomeworkEntry[]>([]);
  const [selected, setSelected] = useState<string>();
  const [cursor, setCursor] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [error, setError] = useState<string>();
  const [historyError, setHistoryError] = useState<string>();
  const [pollError, setPollError] = useState<string>();
  const [pollRetry, setPollRetry] = useState(0);
  const session = useRef<AbortController>(null);
  const current = entries.find(entry => entry.id === selected);
  const pendingId = current?.state === "pending" ? current.id : undefined;
  const resultHeading = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    session.current = controller;
    void homeworkHistory(run, controller.signal).then(page => {
      if (controller.signal.aborted) return;
      setEntries(items => mergeEntries(items, page.items));
      setCursor(page.next_cursor ?? undefined);
    }).catch(cause => { if (!controller.signal.aborted) setHistoryError(message(cause)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [run]);

  useEffect(() => {
    if (!pendingId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll(id: string) {
      try {
        const entry = await getHomework(run, id, controller.signal);
        if (controller.signal.aborted) return;
        setEntries(items => mergeEntries(items, [entry]));
        if (entry.state === "pending") timer = setTimeout(() => void poll(id), 2500);
      } catch (cause) { if (!controller.signal.aborted) setPollError(message(cause)); }
    }
    timer = setTimeout(() => void poll(pendingId), 1500);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [run, pendingId, pollRetry]);

  useEffect(() => { if (selected) resultHeading.current?.focus(); }, [selected]);

  function select(id: string) {
    if (selected === id) {
      resultHeading.current?.focus();
      return;
    }
    setSelected(id);
    setPollError(undefined);
    setConfirmDelete(false);
  }

  async function loadMore(next?: string) {
    const signal = session.current?.signal;
    if (!signal || loading) return;
    setLoading(true);
    setHistoryError(undefined);
    try {
      const page = await homeworkHistory(run, signal, next);
      if (signal.aborted) return;
      setEntries(items => mergeEntries(next ? items : items.filter(item => item.id === selected), page.items));
      setCursor(page.next_cursor ?? undefined);
    } catch (cause) { if (!signal.aborted) setHistoryError(message(cause)); }
    finally { if (!signal.aborted) setLoading(false); }
  }

  async function check() {
    const signal = session.current?.signal;
    if (!signal || !files.length || busy || loading || deleting || !enabled) return;
    setBusy(true);
    setError(undefined);
    try {
      const images: string[] = [];
      for (const file of files) {
        images.push(await encodeWorksheet(file));
        if (signal.aborted) return;
      }
      const entry = await submitHomework(run, { subject, level, images: images as HomeworkSubmission["images"] }, signal);
      if (signal.aborted) return;
      setEntries(items => mergeEntries(items, [entry]));
      select(entry.id);
      setFiles([]);
    } catch (cause) { if (!signal.aborted) setError(message(cause)); }
    finally { if (!signal.aborted) setBusy(false); }
  }

  async function remove(entry: HomeworkEntry) {
    const signal = session.current?.signal;
    if (!signal || deleting) return;
    setDeleting(true);
    setHistoryError(undefined);
    try {
      await deleteHomework(run, entry.id, signal);
      if (signal.aborted) return;
      setEntries(items => items.filter(item => item.id !== entry.id));
      setSelected(id => id === entry.id ? undefined : id);
      setConfirmDelete(false);
    } catch (cause) { if (!signal.aborted) setHistoryError(message(cause)); }
    finally { if (!signal.aborted) setDeleting(false); }
  }

  return <section className="homework" aria-label="Homework help">
    <div className="shelf-banner homework-banner">
      <div><span className="eyebrow">Little Pages learning studio</span><h1>Homework help</h1><p>Big questions, little steps. Add worksheet photos to check answers and find out why.</p></div>
      <span className="homework-banner-art" aria-hidden="true">✓</span>
    </div>
    <form className="camera-card homework-form" onSubmit={event => { event.preventDefault(); void check(); }}>
      <h2>Check a worksheet</h2>
      {!enabled && <p role="status">The vision model isn’t configured. Saved results are still available below.</p>}
      <div className="homework-fields">
        <label>Subject<select disabled={busy} value={subject} onChange={e => setSubject(e.target.value as typeof subject)}>{Object.entries(subjects).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label>School level<select disabled={busy} value={level} onChange={e => setLevel(e.target.value as typeof level)}>{Object.entries(levels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      </div>
      <div className="homework-upload">
        <label htmlFor="homework-photos">Worksheet photos ({files.length}/{maxPhotos})</label>
        <input id="homework-photos" type="file" accept="image/jpeg,image/png,image/webp" multiple disabled={!enabled || busy || files.length === maxPhotos} aria-describedby="homework-upload-help" onChange={e => {
          const added = Array.from(e.currentTarget.files ?? []);
          e.currentTarget.value = "";
          if (!added.length) return;
          if (files.length + added.length > maxPhotos) { setError(`Choose up to ${maxPhotos} photos at a time.`); return; }
          setFiles([...files, ...added]); setError(undefined);
        }} />
        <p className="homework-meta" id="homework-upload-help">Add up to four clear JPEG, PNG, or WebP photos in page order. Crop out names, faces, and school details before uploading. Checking sends your photos to OpenRouter and its routed AI provider.</p>
        {files.length > 0 && <ol className="homework-files">{files.map((file, index) => <li key={index}><span>{file.name}</span><button type="button" disabled={busy} aria-label={`Remove photo ${index + 1}: ${file.name}`} onClick={() => { setFiles(items => items.filter((_, position) => position !== index)); setError(undefined); }}>Remove</button></li>)}</ol>}
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      <button className="primary" disabled={!enabled || !files.length || busy || loading || deleting}>{busy ? "Checking photos…" : "Check photos"}</button>
      {busy && <span className="sr-only" role="status">Preparing and checking your photos.</span>}
    </form>
    <div ref={resultHeading} tabIndex={-1} className="homework-result-target">
      {current && <HomeworkAnswers key={current.id} entry={current} />}
      {pendingId && pollError && <p className="error" role="alert">{pollError} <button onClick={() => { setPollError(undefined); setPollRetry(value => value + 1); }}>Check status again</button></p>}
    </div>
    <section className="camera-card homework-history" aria-labelledby="homework-history-title">
      <div className="section-heading"><h2 id="homework-history-title">All History</h2><button onClick={() => void loadMore()} disabled={loading || busy || deleting}>Refresh</button></div>
      <p className="homework-meta">Saved homework questions, answers, and explanations. Original photos aren’t part of history.</p>
      {historyError && <p className="error" role="alert">{historyError}</p>}
      {loading && <p className="status-line" role="status"><span className="spinner" />Loading history…</p>}
      {!loading && !historyError && !entries.length && <p>No homework checks yet.</p>}
      <ul>{entries.map(entry => <li key={entry.id}>
        <button className="homework-history-item" aria-pressed={entry.id === selected} onClick={() => select(entry.id)} disabled={deleting}>
          <strong>{entry.result?.questions[0]?.question ?? `${subjects[entry.subject]} worksheet`}</strong>
          <span>{subjects[entry.subject]} · {levels[entry.level]} · {new Date(entry.created_at).toLocaleString()}</span>
          <small>{statuses[entry.state]}</small>
        </button>
      </li>)}</ul>
      {cursor && <button disabled={loading || deleting} onClick={() => void loadMore(cursor)}>Load older checks</button>}
      {current && current.state !== "pending" && <div className="homework-delete">
        {confirmDelete ? <><span>Delete this homework check from history?</span> <button disabled={deleting} onClick={() => void remove(current)}>Delete check</button> <button disabled={deleting} onClick={() => setConfirmDelete(false)}>Keep check</button></> : <button disabled={busy || loading} onClick={() => setConfirmDelete(true)}>Delete selected check</button>}
      </div>}
    </section>
  </section>;
}
