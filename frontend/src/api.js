export const API_URL = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/+$/, "");

// "detail" can be a string (503/404) or an array of validation errors (422).
export function formatDetail(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        const field = Array.isArray(d.loc) ? d.loc.filter((p) => p !== "body").join(".") : "";
        const msg = (d.msg || "invalid input").replace(/^Value error, /, "");
        return field ? `${field}: ${msg}` : msg;
      })
      .join("; ");
  }
  return "Something went wrong.";
}

export async function sendChat({ sessionId, message, systemPrompt, simulateFailure }) {
  const headers = { "Content-Type": "application/json" };
  if (simulateFailure && simulateFailure.trim()) headers["X-Simulate-Failure"] = simulateFailure.trim();

  const body = { session_id: sessionId, message };
  if (systemPrompt && systemPrompt.trim()) body.system_prompt = systemPrompt.trim();

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 60000);
  let res;
  try {
    res = await fetch(`${API_URL}/chat`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    if (err.name === "AbortError") {
      return { ok: false, error: "Backend ne 60 sec tak jawab nahi diya (free server so raha hoga). Ek baar aur try karo." };
    }
    return { ok: false, error: "Backend se connect nahi ho paa raha. Network ya server check karo." };
  }
  clearTimeout(timer);

  let data = null;
  try {
    data = await res.json();
  } catch {
    /* non-JSON body: handled below */
  }

  if (!res.ok) {
    if (res.status === 503) {
      return { ok: false, error: `${formatDetail(data?.detail ?? "all providers unavailable")} (503). Saari keys fail ho gayi, thodi der baad try karo.` };
    }
    if (res.status === 422) {
      return { ok: false, error: `Input theek nahi hai: ${formatDetail(data?.detail)}` };
    }
    return { ok: false, error: `Error ${res.status}: ${formatDetail(data?.detail)}` };
  }
  if (!data || typeof data.reply !== "string" || !data.metadata) {
    return { ok: false, error: "Backend ka response samajh nahi aaya." };
  }
  return { ok: true, data };
}
