import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { endMockSession, parseResume, startMockSession, submitMockTurn, transcribeAudio } from "../api/client";
import type { InterviewFeedbackReport } from "../api/types";
import styles from "./MockRoom.module.css";

interface TranscriptEntry {
  who: "Interviewer" | "You";
  text: string;
}

type Phase = "setup" | "active" | "ended";

function formatClock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const mm = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

/**
 * Real audio (mic permission/denial, streaming STT, TTS playback,
 * reconnect handling) is explicitly flagged in the design handoff README
 * as absent from the mock and something to confirm before designing.
 * Scoped down for this pass: mic recording is best-effort via the actual
 * MediaRecorder API (real permission prompt, real upload to
 * /api/interview/stt), but there's no TTS and no reconnect handling yet,
 * and a typed-answer fallback always works even if the mic is denied or
 * unavailable. The mock's "Live signal" score bars and fixed 4-round list
 * aren't reproduced here because the backend has no live per-turn scoring
 * or fixed-round structure to back them with real numbers -- inventing
 * those would misrepresent actual performance.
 */
export function MockRoom() {
  const navigate = useNavigate();
  const [phase, setPhase] = useState<Phase>("setup");
  const [starting, setStarting] = useState(false);
  const [resumeFile, setResumeFile] = useState<File | null>(null);
  const [startError, setStartError] = useState<string | null>(null);

  const [sessionId, setSessionId] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [topic, setTopic] = useState("");
  const [remaining, setRemaining] = useState(0);
  const [duration, setDuration] = useState(1);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [answer, setAnswer] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const [micOn, setMicOn] = useState(false);
  const [micError, setMicError] = useState<string | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  const [feedback, setFeedback] = useState<InterviewFeedbackReport | null>(null);

  useEffect(() => {
    if (phase !== "active" || remaining <= 0) return;
    const id = setInterval(() => setRemaining((r) => Math.max(0, r - 1)), 1000);
    return () => clearInterval(id);
  }, [phase, remaining]);

  async function start(mode: "generic" | "personalized") {
    setStarting(true);
    setStartError(null);
    try {
      let resumeText: string | undefined;
      if (mode === "personalized" && resumeFile) {
        const parsed = await parseResume(resumeFile);
        resumeText = parsed.resume_text;
      }
      const res = await startMockSession({ mode, resumeText });
      setSessionId(res.session_id);
      setQuestion(res.question);
      setTopic(res.topic);
      setRemaining(res.remaining_seconds);
      setDuration(res.duration_seconds);
      setTranscript([{ who: "Interviewer", text: res.question }]);
      setPhase("active");
    } catch (e) {
      setStartError(e instanceof Error ? e.message : "Couldn't start the interview.");
    } finally {
      setStarting(false);
    }
  }

  async function toggleMic() {
    if (micOn) {
      mediaRecorderRef.current?.stop();
      setMicOn(false);
      return;
    }
    setMicError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      chunksRef.current = [];
      const recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (e) => chunksRef.current.push(e.data);
      recorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunksRef.current, { type: "audio/webm" });
        try {
          const { text } = await transcribeAudio(blob);
          setAnswer((prev) => (prev ? `${prev} ${text}` : text));
        } catch {
          setMicError("Couldn't transcribe that recording — you can type your answer instead.");
        }
      };
      recorder.start();
      mediaRecorderRef.current = recorder;
      setMicOn(true);
    } catch {
      setMicError("Microphone access was denied — you can type your answer instead.");
    }
  }

  async function submitAnswer() {
    if (!sessionId || !answer.trim() || submitting) return;
    setSubmitting(true);
    const answerText = answer.trim();
    setTranscript((t) => [...t, { who: "You", text: answerText }]);
    setAnswer("");
    try {
      const res = await submitMockTurn({ sessionId, answerText });
      setRemaining(res.remaining_seconds);
      if (res.time_up || !res.question) {
        await finish(sessionId);
      } else {
        setQuestion(res.question);
        setTopic(res.topic || topic);
        setTranscript((t) => [...t, { who: "Interviewer", text: res.question! }]);
      }
    } catch (e) {
      setTranscript((t) => [
        ...t,
        { who: "Interviewer", text: e instanceof Error ? e.message : "Something went wrong — try again." },
      ]);
    } finally {
      setSubmitting(false);
    }
  }

  async function finish(id: string) {
    try {
      const res = await endMockSession(id);
      setFeedback(res.feedback);
    } finally {
      setPhase("ended");
    }
  }

  if (phase === "setup") {
    return (
      <div className={styles.setup}>
        <div className={styles.setupH1}>Mock Interview</div>
        <p className={styles.setupSub}>
          A 45-minute spoken SQL interview with an adaptive virtual interviewer. Answer out loud or type — either
          works.
        </p>
        {startError && <p style={{ color: "var(--color-fail-text)" }}>{startError}</p>}
        <div className={styles.setupActions}>
          <button className="btn btn-primary" onClick={() => start("generic")} disabled={starting}>
            {starting ? "Starting…" : "Start a generic round"}
          </button>
        </div>
        <div>
          <label className="visually-hidden" htmlFor="resume-upload">
            Upload resume for a personalized round
          </label>
          <input
            id="resume-upload"
            type="file"
            accept=".pdf,.docx,.txt"
            onChange={(e) => setResumeFile(e.target.files?.[0] ?? null)}
          />
          <button
            className="btn btn-secondary"
            style={{ marginLeft: 8 }}
            onClick={() => start("personalized")}
            disabled={starting || !resumeFile}
          >
            Start personalized round
          </button>
        </div>
        <button className="btn btn-ghost" onClick={() => navigate("/")} style={{ alignSelf: "flex-start" }}>
          ← Back to practice
        </button>
      </div>
    );
  }

  if (phase === "ended") {
    return (
      <div className={styles.feedback}>
        <button className={styles.leave} onClick={() => navigate("/")}>
          ← Back to practice
        </button>
        <h1>Interview feedback</h1>
        {feedback ? (
          <>
            {feedback.score !== null && <div className={styles.scoreLine}>{feedback.score} / 100</div>}
            <p>{feedback.overall_summary}</p>
            {feedback.strengths.length > 0 && (
              <div>
                <h3>Strengths</h3>
                <ul className={styles.list}>
                  {feedback.strengths.map((s, i) => (
                    <li key={i}>{s}</li>
                  ))}
                </ul>
              </div>
            )}
            {feedback.weaknesses.length > 0 && (
              <div>
                <h3>To work on</h3>
                <ul className={styles.list}>
                  {feedback.weaknesses.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              </div>
            )}
            {feedback.topics_to_study.length > 0 && (
              <div>
                <h3>Topics to study</h3>
                <ul className={styles.list}>
                  {feedback.topics_to_study.map((t, i) => (
                    <li key={i}>{t}</li>
                  ))}
                </ul>
              </div>
            )}
          </>
        ) : (
          <p>Couldn't generate a feedback report for this session.</p>
        )}
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <button className={styles.leave} onClick={() => sessionId && finish(sessionId)}>
          ← Leave session
        </button>
        <span className="tag-pro">PRO · BETA</span>
        <div className={styles.headerRight}>
          <span className={styles.topicLabel}>{topic}</span>
          <span className={styles.clock}>{formatClock(remaining)}</span>
        </div>
      </header>

      <div className={styles.body}>
        <section className={styles.main}>
          <div>
            <div className={styles.kicker}>Interviewer</div>
            <p className={styles.question}>{question}</p>
          </div>

          <div className={styles.micRow}>
            <button className={`${styles.micBtn} ${micOn ? styles.micBtnOn : ""}`} onClick={toggleMic}>
              {micOn ? "Mic on" : "Mic muted"}
            </button>
            <div className={styles.bars} aria-hidden="true">
              {Array.from({ length: 14 }, (_, i) => (
                <span
                  key={i}
                  className={`${styles.bar} ${micOn ? styles.barOn : ""}`}
                  style={{ height: 10 + ((i * 7) % 16), animationDelay: `${i * 0.06}s` }}
                />
              ))}
            </div>
            <span className={styles.micHint}>
              {micError || (micOn ? "Listening — speak your answer out loud" : "Unmute to speak, or type below")}
            </span>
          </div>

          <div className={styles.answerRow}>
            <textarea
              className={styles.answerInput}
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
              placeholder="Your answer…"
            />
          </div>

          <div
            className={styles.transcript}
            role="log"
            aria-live="polite"
            aria-label="Interview transcript"
          >
            <div className={styles.transcriptKicker}>Transcript</div>
            {transcript.map((t, i) => (
              <div key={i} className={`${styles.entry} ${t.who === "You" ? styles.entryUser : ""} fade-up`}>
                <div className={styles.entryWho}>{t.who}</div>
                <div className={styles.entryText}>{t.text}</div>
              </div>
            ))}
          </div>

          <div className={styles.bottomActions}>
            <button className="btn btn-primary" onClick={submitAnswer} disabled={submitting || !answer.trim()}>
              {submitting ? "Sending…" : "Submit answer"}
            </button>
            <button className="btn btn-secondary" onClick={() => sessionId && finish(sessionId)}>
              End & get feedback
            </button>
          </div>
        </section>

        <aside className={styles.rail}>
          <div className={styles.railSection}>
            <div className={styles.railKicker}>Session</div>
            <div className={styles.railCard}>
              Round time: {formatClock(duration)} total · {formatClock(remaining)} remaining
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
