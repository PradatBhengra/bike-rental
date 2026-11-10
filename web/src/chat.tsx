import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { api, getToken, refresh } from "./api";

type Citation = { n: number; slug: string; title: string; section_no: string; anchor: string };
type Card = { type: string; action_id: number; kind: string; preview: Record<string, string>; status: string; result?: any };
type Msg = { id?: number; role: string; content: string; citations: Citation[]; cards: Card[]; rating?: number | null };

/** Turns "[2]" into a chip that links to the cited help section; a number the server did not confirm is dropped. */
export function withCitations(text: string, citations: Citation[]): ReactNode[] {
  const byN = new Map(citations.map((c) => [c.n, c]));
  return text.split(/(\[\d+\])/).map((part, i) => {
    const match = part.match(/^\[(\d+)\]$/);
    if (!match) return part;
    const c = byN.get(Number(match[1]));
    return c ? (
      <Link key={i} to={`/help/${c.slug}#${c.anchor}`} className="chip mx-0.5 no-underline">
        {c.title} §{c.section_no}
      </Link>
    ) : null;
  });
}

/** Reads a server-sent event stream from a POST (EventSource can only GET). */
async function* events(response: Response) {
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const name = block.match(/^event: (.*)$/m)?.[1] ?? "message";
      const data = block.match(/^data: (.*)$/m)?.[1];
      if (data) yield { name, data: JSON.parse(data) };
    }
  }
}

async function post(conversationId: number, text: string, retry = true): Promise<Response> {
  const r = await fetch(`/api/v1/chat/conversations/${conversationId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify({ text }),
  });
  if (r.status === 401 && retry && (await refresh())) return post(conversationId, text, false);
  return r;
}

function ConfirmCard({ card, onChange }: { card: Card; onChange: (c: Card) => void }) {
  const [busy, setBusy] = useState(false);
  const decide = async (yes: boolean) => {
    setBusy(true);
    try {
      const r = await api(`/chat/actions/${card.action_id}/${yes ? "confirm" : "reject"}`, { method: "POST" });
      onChange({ ...card, status: r.status, result: r.result });
    } catch (e: any) {
      onChange({ ...card, status: "failed", result: { error: e.message } });
    } finally {
      setBusy(false);
    }
  };
  const title = { book: "New booking", cancel: "Cancel booking", extend: "Extend booking" }[card.kind] ?? card.kind;
  return (
    <div className="mt-2 rounded-lg border border-sky/40 bg-mist/30 p-3 text-sm">
      <div className="mb-1 font-semibold text-navy">{title}</div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
        {Object.entries(card.preview).map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-slate-500 capitalize">{k.replace("_", " ")}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
      {card.status === "proposed" ? (
        <div className="mt-2 flex gap-2">
          <button className="btn" disabled={busy} onClick={() => decide(true)}>
            Confirm
          </button>
          <button className="btn-ghost" disabled={busy} onClick={() => decide(false)}>
            Not now
          </button>
        </div>
      ) : (
        <div className="mt-2 text-xs font-medium text-navy">
          {card.status === "executed" ? "Done." : card.status}
          {card.result?.error && <span className="text-red-700"> {card.result.error}</span>}
          {card.status === "executed" && card.result?.pay && (
            <Link className="btn ml-2" to={`/bookings/${card.result.booking_id}/pay`}>
              Pay now
            </Link>
          )}
        </div>
      )}
    </div>
  );
}

export function ChatPanel({ conversationId: initial }: { conversationId?: number }) {
  const [conversationId, setConversationId] = useState<number | undefined>(initial);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setConversationId(initial);
    if (initial) api<Msg[]>(`/chat/conversations/${initial}/messages`).then(setMessages);
    else setMessages([]);
  }, [initial]);
  useEffect(() => bottom.current?.scrollIntoView({ block: "end" }), [messages, status]);

  const patchLast = (change: (m: Msg) => Msg) => setMessages((all) => [...all.slice(0, -1), change(all[all.length - 1])]);

  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    const question = text.trim();
    if (!question || busy) return;
    setText("");
    setBusy(true);
    setMessages((all) => [
      ...all,
      { role: "user", content: question, citations: [], cards: [] },
      { role: "assistant", content: "", citations: [], cards: [] },
    ]);
    try {
      let id = conversationId;
      if (!id) {
        id = (await api("/chat/conversations", { method: "POST" })).id as number;
        setConversationId(id);
      }
      const r = await post(id, question);
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? "The assistant is not available.");
      for await (const { name, data } of events(r)) {
        if (name === "status") setStatus(data.text + "…");
        if (name === "token") patchLast((m) => ({ ...m, content: m.content + data.text }));
        if (name === "card") patchLast((m) => ({ ...m, cards: [...m.cards, data] }));
        if (name === "citations") patchLast((m) => ({ ...m, citations: data }));
        if (name === "done") patchLast((m) => ({ ...m, id: data.message_id }));
        if (name === "error") patchLast((m) => ({ ...m, content: data.detail }));
      }
    } catch (err: any) {
      patchLast((m) => ({ ...m, content: err.message }));
    } finally {
      setStatus("");
      setBusy(false);
    }
  };

  const rate = async (m: Msg, rating: number) => {
    await api(`/chat/messages/${m.id}/feedback`, { method: "POST", json: { rating } });
    setMessages((all) => all.map((x) => (x === m ? { ...x, rating } : x)));
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {messages.length === 0 && (
          <div className="text-sm text-slate-500">
            Ask about your rides and spending, or about our rules, bikes and routes. Try:
            <ul className="mt-2 list-disc pl-5">
              <li>How much did I spend last month?</li>
              <li>If I cancel my Friday booking, how much do I get back?</li>
              <li>What does E02 mean on the e-bike?</li>
            </ul>
          </div>
        )}
        {messages
          .filter((m) => m.role !== "note")
          .map((m, i) => (
            <div key={i} className={m.role === "user" ? "flex justify-end" : ""}>
              <div
                className={`max-w-[90%] rounded-xl px-3 py-2 text-sm whitespace-pre-wrap ${
                  m.role === "user" ? "bg-navy text-white" : "bg-slate-100 text-ink"
                }`}
              >
                {m.role === "assistant" ? withCitations(m.content, m.citations) : m.content}
                {m.cards.map((c) => (
                  <ConfirmCard
                    key={c.action_id}
                    card={c}
                    onChange={(next) =>
                      setMessages((all) =>
                        all.map((x) =>
                          x === m ? { ...x, cards: x.cards.map((y) => (y.action_id === c.action_id ? next : y)) } : x,
                        ),
                      )
                    }
                  />
                ))}
                {m.role === "assistant" && m.id && (
                  <div className="mt-1 flex gap-2 text-xs text-slate-400">
                    <button onClick={() => rate(m, 1)} className={m.rating === 1 ? "text-navy" : ""} title="Helpful">
                      👍
                    </button>
                    <button onClick={() => rate(m, -1)} className={m.rating === -1 ? "text-red-600" : ""} title="Not helpful">
                      👎
                    </button>
                  </div>
                )}
              </div>
            </div>
          ))}
        {status && <div className="text-xs text-sky">{status}</div>}
        <div ref={bottom} />
      </div>
      <form onSubmit={send} className="flex gap-2 border-t border-slate-200 p-3">
        <input
          className="input"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Ask a question"
          maxLength={2000}
        />
        <button className="btn" disabled={busy}>
          Send
        </button>
      </form>
    </div>
  );
}

export function ChatPage() {
  const [list, setList] = useState<{ id: number; title: string }[]>([]);
  const [active, setActive] = useState<number | undefined>();
  useEffect(() => {
    api("/chat/conversations").then(setList);
  }, [active]);
  return (
    <div className="grid h-[calc(100vh-9rem)] gap-4 md:grid-cols-[220px_1fr]">
      <aside className="card overflow-y-auto p-2 text-sm">
        <button className="btn mb-2 w-full" onClick={() => setActive(undefined)}>
          New chat
        </button>
        {list.map((c) => (
          <button
            key={c.id}
            onClick={() => setActive(c.id)}
            className={`block w-full truncate rounded px-2 py-1.5 text-left ${active === c.id ? "bg-mist text-navy" : "hover:bg-slate-100"}`}
          >
            {c.title}
          </button>
        ))}
      </aside>
      <div className="card flex min-h-0 flex-col p-0">
        <ChatPanel conversationId={active} />
      </div>
    </div>
  );
}
