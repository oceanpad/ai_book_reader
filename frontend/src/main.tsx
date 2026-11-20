import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Reader } from "./reader/Reader";
import { AuthGate } from "./auth/AuthGate";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("The reader root element is missing.");
createRoot(root).render(<StrictMode><AuthGate><Reader /></AuthGate></StrictMode>);
