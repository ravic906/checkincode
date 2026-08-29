import { SignedIn, SignedOut, SignInButton, UserButton } from "@clerk/clerk-react";
import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { fetchProgress } from "../api/client";
import { computeStreak } from "../lib/streak";
import styles from "./GlobalHeader.module.css";

export function GlobalHeader() {
  const [streak, setStreak] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchProgress()
      .then((p) => {
        if (!cancelled) setStreak(computeStreak(p.activity));
      })
      .catch(() => {
        if (!cancelled) setStreak(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <header className={styles.header}>
      <NavLink to="/" className={styles.brand} aria-label="PhoenixPrep home">
        <span className={styles.brandMark}>P</span>
        <span className={styles.brandWord}>PhoenixPrep</span>
      </NavLink>

      <nav className={styles.nav} aria-label="Main">
        <NavLink to="/" end className={({ isActive }) => `${styles.navBtn} ${isActive ? styles.navBtnActive : ""}`}>
          Practice
        </NavLink>
        <NavLink to="/mock" className={({ isActive }) => `${styles.navBtn} ${isActive ? styles.navBtnActive : ""}`}>
          Mock Interview
        </NavLink>
        <NavLink to="/progress" className={({ isActive }) => `${styles.navBtn} ${isActive ? styles.navBtnActive : ""}`}>
          Progress
        </NavLink>
      </nav>

      <div className={styles.right}>
        {streak !== null && streak > 0 && (
          <span className={styles.streak}>🔥 {streak}-day streak</span>
        )}
        <SignedOut>
          <SignInButton mode="modal">
            <button className="btn btn-secondary">Sign In</button>
          </SignInButton>
        </SignedOut>
        <SignedIn>
          <button className="btn btn-secondary">Go Pro</button>
          <UserButton afterSignOutUrl="/" />
        </SignedIn>
      </div>
    </header>
  );
}
