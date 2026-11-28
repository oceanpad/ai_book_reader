import { useRef } from "react";
import { authEnabled } from "./auth/session";

const paths = {
  book: "M3 5c3-1 6-1 9 1 3-2 6-2 9-1v14c-3-1-6-1-9 1-3-2-6-2-9-1z M12 6v14",
  plus: "M12 5v14 M5 12h14", search: "M16 16l5 5 M18 10.5a7.5 7.5 0 1 1-15 0 7.5 7.5 0 0 1 15 0",
  refresh: "M20 10a8 8 0 1 0-1 7 M20 4v6h-6", play: "M8 4l12 8-12 8z",
  close: "M6 6l12 12 M18 6L6 18", shelf: "M4 4v16 M9 4v16 M14 5l5 14 M2 21h20",
};
export function Icon({ name }: { name: keyof typeof paths }) {
  return <svg className="ui-icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d={paths[name]} /></svg>;
}
export function Brand() {
  return <a className="wordmark" href="/" aria-label="Little Pages home"><span className="brand-mark"><Icon name="book" /></span>Little Pages</a>;
}
export function ReadingGuide() {
  const dialog = useRef<HTMLDialogElement>(null);
  return <><button className="guide-trigger" onClick={() => dialog.current?.showModal()}><Icon name="book" />Reading guide</button>
    <dialog ref={dialog} className="guide-dialog" aria-labelledby="guide-heading" onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); dialog.current?.close(); } }}><div className="section-heading"><span className="eyebrow">Studio handbook</span><button className="icon-button" aria-label="Close reading guide" onClick={() => dialog.current?.close()}><Icon name="close" /></button></div>
      <h2 id="guide-heading">From paper to playback</h2>
      <ol className="guide-steps"><li><strong>Choose a book</strong>Open a book on your shelf, or create one with a name you’ll recognize.</li><li><strong>Frame a page</strong>Select Start Recording in good light. Nothing is sent until you select Submit.</li><li><strong>Listen and refine</strong>Review the narration or edit the text. Slide Preview plays pages in the order you recorded them.</li></ol>
      <div className="guide-note"><strong>About recognition & privacy</strong><p>Printed words stay in their original language. Picture-only pages receive a short description; unreadable pages need a retake.</p><p>Submitting sends the image and narration text to OpenRouter and its routed providers for recognition and AI-generated speech. Their processing and retention policies apply.</p><p>{authEnabled ? "Captures, thumbnails, and narration are saved to your account until you delete them." : "Local captures and narration are temporary and held in memory for this reading run. You can delete them sooner."}</p></div>
    </dialog></>;
}

export function ShelfArt() {
  return <svg className="shelf-art" viewBox="0 0 320 172" fill="none" aria-hidden="true">
    <circle cx="194" cy="119" r="121" stroke="currentColor" opacity=".16"/><circle cx="194" cy="119" r="95" stroke="currentColor" opacity=".14"/><circle cx="194" cy="119" r="69" stroke="currentColor" opacity=".12"/>
    <g transform="translate(54 26)"><rect x="5" y="25" width="30" height="108" rx="3" fill="#e5bf88"/><path d="M9 36h22M9 40h22M9 119h22" stroke="#876740"/><rect x="38" y="5" width="40" height="128" rx="4" fill="#a4c7b6"/><path d="M44 9v120M51 22h19M51 26h19" stroke="#548574"/><rect x="81" y="31" width="23" height="102" rx="3" fill="#f0debd"/><path d="M85 42h15M85 122h15" stroke="#a39175"/><g transform="rotate(-16 140 132)"><rect x="127" y="20" width="29" height="113" rx="3" fill="#d58b68"/><path d="M132 26v100M138 38h12M138 42h12" stroke="#915d49"/></g><path d="M-14 134h210" stroke="#e5dcca" strokeWidth="2" strokeLinecap="round"/></g>
  </svg>;
}
