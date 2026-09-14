import React, { useState } from "react";
import ReactDOM from "react-dom/client";
import { Analytics } from "@vercel/analytics/react";
import App from "./App";
import LandingPage from "./components/LandingPage";
import IntroLoader from "./components/landing/IntroLoader";
import { AuthProvider, useAuth } from "./context/AuthContext";
import "./index.css";

function Root() {
  const { user, loading } = useAuth();
  // The intro curtain plays once per page load, over whichever surface is
  // about to appear, and lifts on its own.
  const [intro, setIntro] = useState(true);

  if (loading) {
    return (
      <div className="h-screen bg-bg flex items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-accent/30 border-t-accent rounded-full animate-spin" />
          <p className="text-muted text-xs">Loading...</p>
        </div>
      </div>
    );
  }

  return (
    <>
      {user ? <App /> : <LandingPage />}
      {intro && <IntroLoader onReveal={() => window.setTimeout(() => setIntro(false), 800)} />}
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AuthProvider>
      <Root />
      <Analytics />
    </AuthProvider>
  </React.StrictMode>
);
