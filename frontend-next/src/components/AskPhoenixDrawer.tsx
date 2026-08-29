import { useEffect, useRef, useState } from "react";
import { askPhoenix } from "../api/client";
import type { AskPhoenixMessage } from "../api/types";
import styles from "./AskPhoenixDrawer.module.css";

const SUGGESTIONS = ["Give me a hint", "Explain the schema"];

/**
 * The backend's /api/ask-phoenix returns one JSON response, not a stream --
 * the design handoff asks for streaming responses, which needs a backend
 * change (SSE or chunked transfer) this pass didn't include. Wired as a
 * normal request/response for now; swapping in a stream later only touches
 * this component's send() function, not the drawer's shape.
 */
export function AskPhoenixDrawer({
  open,
  onClose,
  problemId,
  code,
}: {
  open: boolean;
  onClose: () => void;
  problemId: string;
  code: string;
}) {
  const [messages, setMessages] = useState<AskPhoenixMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  async function send(question: string) {
    if (!question.trim() || sending) return;
    const nextMessages: AskPhoenixMessage[] = [...messages, { role: "user", content: question }];
    setMessages(nextMessages);
    setInput("");
    setSending(true);
    try {
      const res = await askPhoenix({ problemId, code, question, conversation: messages });
      setMessages([...nextMessages, { role: "assistant", content: res.answer }]);
    } catch (e) {
      setMessages([
        ...nextMessages,
        { role: "assistant", content: e instanceof Error ? e.message : "Ask Phoenix is unavailable right now." },
      ]);
    } finally {
      setSending(false);
    }
  }

  if (!open) return null;

  return (
    <div className={`${styles.drawer} fade-up`} role="dialog" aria-label="Ask Phoenix">
      <div className={styles.header}>
        <span className={styles.headerDot} aria-hidden="true" />
        <span className={styles.headerTitle}>Ask Phoenix</span>
        <button className={styles.closeBtn} onClick={onClose} aria-label="Close Ask Phoenix">
          ✕
        </button>
      </div>

      <div className={styles.messages} ref={scrollRef} aria-live="polite">
        {messages.length === 0 && (
          <div className={styles.msgAssistant}>
            Ask me anything about this problem — I'll guide you toward the approach rather than hand you the answer.
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={m.role === "user" ? styles.msgUser : styles.msgAssistant}>
            {m.content}
          </div>
        ))}
        {sending && <div className={styles.msgAssistant}>Thinking…</div>}
      </div>

      {messages.length === 0 && (
        <div className={styles.chips}>
          {SUGGESTIONS.map((s) => (
            <button key={s} className={styles.chip} onClick={() => send(s)}>
              {s}
            </button>
          ))}
        </div>
      )}

      <form
        className={styles.composer}
        onSubmit={(e) => {
          e.preventDefault();
          send(input);
        }}
      >
        <input
          className={styles.composerInput}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask a question…"
          aria-label="Message Ask Phoenix"
        />
        <button type="submit" className={styles.sendBtn} disabled={sending || !input.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}
