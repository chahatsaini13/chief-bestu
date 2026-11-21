import { useEffect, useRef, useState } from "react";
import { sendChat } from "./api.js";

// A fresh session on every page load.
const newSessionId = () => "s-" + Math.random().toString(36).slice(2, 10);

const STARTERS = [
  "Ghee khatam, padhai nahi ho rahi",
  "Mera din CEO ki tarah plan kar",
  "What is the capital of France?",
];

let nextId = 1;

export default function App() {
  const [sessionId] = useState(newSessionId);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [simulate, setSimulate] = useState("");
  const [showConsole, setShowConsole] = useState(false);
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, loading]);

  async function send(textOverride) {
    const text = (textOverride ?? input).trim();
    if (!text || loading) return;

    setInput(""); // chat-input is empty right after sending
    setMessages((m) => [...m, { id: nextId++, role: "user", text }]);
    setLoading(true);

    const result = await sendChat({ sessionId, message: text, systemPrompt, simulateFailure: simulate });

    setMessages((m) => [
      ...m,
      result.ok
        ? { id: nextId++, role: "assistant", text: result.data.reply, meta: result.data.metadata }
        : { id: nextId++, role: "error", text: result.error },
    ]);
    setLoading(false);
  }

  function onKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  return (
    <div className="shell">
      <header className="top">
        <div className="brand">
          <span className="logo" aria-hidden="true">👔</span>
          <div>
            <h1>Chief Bestu</h1>
            <p className="tag">CEO jo kaam karwata hai. Bestfriend jo saath rehta hai.</p>
          </div>
        </div>
        <div className="chips">
          <span className="chip" title="New session on every page load">session {sessionId}</span>
          <button className="chip btn" onClick={() => setShowConsole((v) => !v)} aria-expanded={showConsole}>
            {showConsole ? "hide" : "boardroom"} settings
          </button>
        </div>
      </header>

      {showConsole && (
        <section className="console" aria-label="Boardroom settings">
          <label>
            <span>System prompt (optional, applies to this session)</span>
            <textarea
              data-testid="system-prompt-input"
              rows={3}
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              placeholder="Leave empty to use Chief Bestu's default personality"
            />
          </label>
          <label>
            <span>X-Simulate-Failure (demo only, e.g. key_1=429 or *=429)</span>
            <input
              data-testid="simulate-failure-input"
              value={simulate}
              onChange={(e) => setSimulate(e.target.value)}
              placeholder="key_1=429"
              spellCheck={false}
            />
          </label>
        </section>
      )}

      <main className="feed" aria-live="polite">
        {messages.length === 0 && (
          <div className="empty">
            <h2>Boss ka cabin khula hai.</h2>
            <p>Kuch bhi pooch. Pehle jawab milega, phir masti.</p>
            <div className="starters">
              {STARTERS.map((s) => (
                <button key={s} className="starter" onClick={() => send(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m) => {
          if (m.role === "user") {
            return (
              <div key={m.id} className="row user">
                <div className="bubble user-bubble">{m.text}</div>
              </div>
            );
          }
          if (m.role === "error") {
            return (
              <div key={m.id} className="row">
                <div className="bubble error-bubble" role="alert">
                  <strong>Oops.</strong> {m.text}
                </div>
              </div>
            );
          }
          const meta = m.meta;
          const cached = meta.resolution_source === "LLM_CACHE";
          return (
            <div key={m.id} className="row">
              <div className="bubble bot-bubble" data-testid="assistant-message">
                <div className="reply">{m.text}</div>
                <div className="meta" aria-label="Request metadata">
                  <span className={"pill " + (cached ? "cache" : "live")}>
                    <span className="dot" />
                    <span data-testid="meta-source">{meta.resolution_source}</span>
                  </span>
                  <span className="kv">
                    <i>model</i>
                    <b data-testid="meta-model">{meta.model}</b>
                  </span>
                  <span className="kv">
                    <i>latency</i>
                    <b data-testid="meta-latency">{meta.latency_ms} ms</b>
                  </span>
                  <span className="kv">
                    <i>in</i>
                    <b data-testid="meta-input-tokens">{meta.input_tokens}</b>
                  </span>
                  <span className="kv">
                    <i>out</i>
                    <b data-testid="meta-output-tokens">{meta.output_tokens}</b>
                  </span>
                  <span className="kv faint">
                    <i>key</i>
                    <b>{meta.key_id ?? "none"}</b>
                  </span>
                  <span className="kv faint">
                    <i>fallbacks</i>
                    <b>{meta.fallback_attempts}</b>
                  </span>
                </div>
              </div>
            </div>
          );
        })}

        {loading && (
          <div className="row">
            <div className="bubble bot-bubble typing" aria-label="Chief Bestu is thinking">
              Chief soch raha hai<span className="dots"><i>.</i><i>.</i><i>.</i></span>
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </main>

      <footer className="composer">
        <textarea
          data-testid="chat-input"
          rows={1}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Boss se kuch bol... (Enter = send, Shift+Enter = new line)"
          maxLength={4000}
        />
        <button data-testid="send-button" onClick={() => send()} disabled={loading || !input.trim()}>
          {loading ? "..." : "Send"}
        </button>
      </footer>
    </div>
  );
}
