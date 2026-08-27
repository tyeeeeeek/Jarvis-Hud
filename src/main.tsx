import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import ArcReactor from "./components/ArcReactor";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ArcReactor />
  </StrictMode>,
);
