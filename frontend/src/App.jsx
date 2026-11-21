import { useEffect, useRef, useState } from "react";
import { sendChat } from "./api.js";

// A fresh session on every page load.
const newSessionId = () => "s-" + Math.random().toString(36).slice(2, 10);

const STARTERS = [
  "I'm procrastinating on studying",
  "Plan my day like a CEO",
  "What is the capital of France?",
];

const HEART = "\u2665";
const STAR = "\u2726";

const THEMES = [["pink", "Pink"], ["lavender", "Lavender"], ["mint", "Mint"], ["night", "Night"]];
const FONTS = [["pixel", "Pixel"], ["clean", "Easy to read"]];

function loadPref(key, fallback, allowed) {
  try {
    const v = localStorage.getItem(key);
    return allowed.some(([id]) => id === v) ? v : fallback;
  } catch {
    return fallback;
  }
}

let nextId = 1;

export default function App() {
  const [sessionId] = useState(newSessionId);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [simulate, setSimulate] = useState("");
  const [showConsole, setShowConsole] = useState(false);
  const [showStyle, setShowStyle] = useState(false);
  const [theme, setTheme] = useState(() => loadPref("cb-theme", "pink", THEMES));
  const [font, setFont] = useState(() => loadPref("cb-font", "pixel", FONTS));
  const bottomRef = useRef(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.font = font;
    try {
      localStorage.setItem("cb-theme", theme);
      localStorage.setItem("cb-font", font);
    } catch {
      /* storage blocked: preference just won't persist */
    }
  }, [theme, font]);

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
    <div className="window">
      <div className="titlebar">
        <span className="title">
          <span className="sparkle">{STAR}</span> chat.exe
        </span>
        <span className="winbtns" aria-hidden="true">
          <i>_</i>
          <i>□</i>
          <i>x</i>
        </span>
      </div>

      <div className="statusbar">
        <span className="status">
          <span className="led" /> Chief Bestu {"\u00b7"} online {HEART}
        </span>
        <span className="chips">
          <span className="chip" title="New session on every page load">
            {sessionId}
          </span>
          <button className="chip btn" onClick={() => setShowStyle((v) => !v)} aria-expanded={showStyle}>
            style
          </button>
          <button className="chip btn" onClick={() => setShowConsole((v) => !v)} aria-expanded={showConsole}>
            {showConsole ? "hide" : "boardroom"} settings
          </button>
        </span>
      </div>

      {showStyle && (
        <section className="stylebar" aria-label="Look and feel">
          <div className="opt">
            <span>theme</span>
            {THEMES.map(([id, label]) => (
              <button key={id} className={"chip btn" + (theme === id ? " on" : "")} onClick={() => setTheme(id)}>
                {label}
              </button>
            ))}
          </div>
          <div className="opt">
            <span>font</span>
            {FONTS.map(([id, label]) => (
              <button key={id} className={"chip btn" + (font === id ? " on" : "")} onClick={() => setFont(id)}>
                {label}
              </button>
            ))}
          </div>
        </section>
      )}

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
            <div className="big-heart">{HEART}</div>
            <h2>hi bestie!</h2>
            <p>Ask me anything. Answers first, fun after.</p>
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
                  <span className="kv">
                    <i>key</i>
                    <b>{meta.key_id ?? "none"}</b>
                  </span>
                  <span className="kv">
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
            <div className="bubble bot-bubble typing" aria-label="Chief Bestu is typing">
              typing<span className="dots"><i>.</i><i>.</i><i>.</i></span>
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
          placeholder="type a message..."
          maxLength={4000}
        />
        <button data-testid="send-button" onClick={() => send()} disabled={loading || !input.trim()}>
          {loading ? "..." : "Send"}
        </button>
      </footer>
    </div>
  );
}