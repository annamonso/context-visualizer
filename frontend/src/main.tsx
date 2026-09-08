import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App";

// `?theme=` wins over the stored preference and is persisted, mirroring the
// `?tab=` deep link App reads. It exists so a demo or screen recording can open
// straight into a palette without a first-paint flash of the other one.
const params = new URLSearchParams(window.location.search);
const requested = params.get("theme");
const stored = localStorage.getItem("theme");
const pick = (v: string | null) => (v === "light" || v === "dark" ? v : null);
const initialTheme = pick(requested) ?? pick(stored) ?? "dark";
document.documentElement.dataset.theme = initialTheme;
if (pick(requested)) localStorage.setItem("theme", initialTheme);

const root = document.getElementById("root");
if (!root) throw new Error("Root element not found");
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>
);
