const { useState, useEffect, useRef, useCallback } = React;
const API = "http://localhost:8000";
const TOKEN_KEY = "caseforge_token";

// Mirrors models.VALID_TRANSITIONS; the server re-checks every transition regardless.
const TRANSITIONS = {
    NEW: ["IN_PROGRESS"],
    IN_PROGRESS: ["UNDER_REVIEW", "ESCALATED"],
    UNDER_REVIEW: ["RESOLVED", "ESCALATED", "CLOSED"],
    RESOLVED: ["REOPENED"],
    ESCALATED: ["CLOSED"],
    REOPENED: ["IN_PROGRESS"],
    CLOSED: [],
};
const ALL_STATUSES = ["NEW", "IN_PROGRESS", "UNDER_REVIEW", "RESOLVED", "ESCALATED", "CLOSED", "REOPENED"];

// -- identity ------------------------------------------------------------------------
// The token is kept per tab (sessionStorage) so two tabs can be two different people, and
// remembered (localStorage) as the default for new tabs. Never put it in the URL.
function readToken() {
    try { return sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY); }
    catch (e) { return null; }
}
function storeToken(token) {
    try { sessionStorage.setItem(TOKEN_KEY, token); localStorage.setItem(TOKEN_KEY, token); }
    catch (e) { /* storage blocked: the token lasts until reload */ }
}
function authHeaders(extra = {}) {
    const token = readToken();
    return token ? { ...extra, Authorization: `Bearer ${token}` } : { ...extra };
}

// Every request carries the user's own bearer token. With none stored, the API accepts the
// request only in single-user local mode (CF_LOCAL_USER). A 401 shows the sign-in form; it is
// an in-page form rather than window.prompt, which some embedded browsers block outright.
function apiFetch(path, opts = {}) {
    return fetch(`${API}${path}`, { ...opts, headers: authHeaders(opts.headers) }).then(resp => {
        if (resp.status === 401) window.dispatchEvent(new Event("caseforge:unauthorized"));
        return resp;
    });
}
const ifMatch = c => ({ "If-Match": `"v${c.version}"` });

// -- live feed -----------------------------------------------------------------------
// Server-Sent Events read through fetch, not EventSource, because EventSource cannot send
// an Authorization header. Reconnects with backoff and sends Last-Event-ID, so the server
// replays exactly what was missed; a `resync` event means it could not, and we reload.
function useLiveFeed(onEvent) {
    const [status, setStatus] = useState("connecting");
    const handler = useRef(onEvent);
    handler.current = onEvent;

    useEffect(() => {
        let stopped = false;
        let lastEventId = null;
        let controller = null;
        let attempt = 0;

        async function connect() {
            while (!stopped) {
                controller = new AbortController();
                try {
                    const headers = authHeaders(lastEventId ? { "Last-Event-ID": lastEventId } : {});
                    const resp = await fetch(`${API}/api/events`, { headers, signal: controller.signal });
                    if (resp.status === 401) { setStatus("unauthorized"); return; }
                    if (!resp.ok || !resp.body) throw new Error(`stream ${resp.status}`);
                    const reader = resp.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = "";
                    while (!stopped) {
                        const { value, done } = await reader.read();
                        if (done) break;
                        buffer += decoder.decode(value, { stream: true });
                        let cut;
                        while ((cut = buffer.indexOf("\n\n")) >= 0) {
                            const block = buffer.slice(0, cut);
                            buffer = buffer.slice(cut + 2);
                            let id = null, data = "";
                            for (const line of block.split("\n")) {
                                if (line.startsWith("id: ")) id = line.slice(4);
                                else if (line.startsWith("data: ")) data += line.slice(6);
                            }
                            if (!data) continue; // heartbeat or retry hint
                            const event = JSON.parse(data);
                            if (id) lastEventId = id;
                            if (event.type === "hello") { attempt = 0; setStatus("live"); }
                            handler.current(event);
                        }
                    }
                } catch (e) {
                    if (stopped) return;
                }
                if (stopped) return;
                setStatus("reconnecting");
                attempt += 1;
                const delay = Math.min(15000, 500 * 2 ** Math.min(attempt, 5)) * (0.5 + Math.random() / 2);
                await new Promise(r => setTimeout(r, delay));
            }
        }
        connect();
        return () => { stopped = true; controller && controller.abort(); };
    }, []);

    return status;
}

function describe(event) {
    const who = event.actor;
    const what = event.case?.product_id || event.product_id || "a case";
    const ch = event.change || {};
    switch (event.type) {
        case "case.created": return `${who} created ${what}`;
        case "case.deleted": return `${who} deleted ${what}`;
        case "cases.imported": return `${who} imported ${event.count} case${event.count === 1 ? "" : "s"}`;
        case "case.updated":
            if (ch.status) return `${who} moved ${what} ${ch.status[0]} → ${ch.status[1]}`;
            if (ch.note) return `${who} added a note to ${what}`;
            if (ch.fields) return `${who} edited ${ch.fields.join(", ")} on ${what}`;
            return `${who} updated ${what}`;
        default: return null;
    }
}

function SignIn({ onCancel }) {
    const [token, setToken] = useState("");
    function submit(e) {
        e.preventDefault();
        if (!token.trim()) return;
        storeToken(token.trim());
        window.location.reload();
    }
    return (
        <div className="max-w-sm mx-auto mt-32 card glow p-6 space-y-4 fade-in">
            <h1 className="text-accent font-semibold tracking-wide">CaseForge</h1>
            <p className="text-xs text-slate-400">Enter your API token. It identifies you on every request and is kept in this browser tab.</p>
            <form onSubmit={submit} className="space-y-3">
                <input type="password" autoFocus className="w-full" placeholder="API token" aria-label="API token" value={token} onChange={e => setToken(e.target.value)} />
                <div className="flex gap-2">
                    <button type="submit" className="btn-primary" disabled={!token.trim()}>Sign in</button>
                    {onCancel && <button type="button" onClick={onCancel} className="btn-ghost">Cancel</button>}
                </div>
            </form>
        </div>
    );
}

function App() {
    const [signIn, setSignIn] = useState(null); // null | "required" | "switch"
    useEffect(() => {
        const show = () => setSignIn(s => s || "required");
        window.addEventListener("caseforge:unauthorized", show);
        return () => window.removeEventListener("caseforge:unauthorized", show);
    }, []);
    if (signIn) return <SignIn onCancel={signIn === "switch" ? () => setSignIn(null) : null} />;
    return <Workspace onSwitchUser={() => setSignIn("switch")} />;
}

function Workspace({ onSwitchUser }) {
    const [view, setView] = useState("list");
    const [cases, setCases] = useState([]);
    const [stats, setStats] = useState(null);
    const [me, setMe] = useState(null);
    const [filters, setFilters] = useState({});
    const [selectedId, setSelectedId] = useState(null);
    const [selectedCase, setSelectedCase] = useState(null);
    const [selectedNotice, setSelectedNotice] = useState(null);
    const [categories, setCategories] = useState([]);
    const [online, setOnline] = useState([]);
    const [toasts, setToasts] = useState([]);
    const [flashing, setFlashing] = useState({});
    const listAsOf = useRef(0);
    const ownWrites = useRef(new Set()); // "case_id:version" this tab produced
    const meRef = useRef(null);
    const selectedRef = useRef(null);
    const filtersRef = useRef(filters);
    const timers = useRef({});
    filtersRef.current = filters;
    selectedRef.current = selectedId;

    useEffect(() => {
        apiFetch(`/api/me`).then(r => r.json()).then(m => { meRef.current = m; setMe(m); });
        apiFetch(`/api/categories`).then(r => r.json()).then(setCategories);
    }, []);

    const loadCases = useCallback(() => {
        const params = new URLSearchParams();
        Object.entries(filtersRef.current).forEach(([k, v]) => { if (v) params.set(k, v); });
        return apiFetch(`/api/cases?${params}`).then(r => r.json()).then(d => {
            listAsOf.current = d.as_of_event || 0;
            setCases(d.cases || []);
        });
    }, []);
    const loadStats = useCallback(() => apiFetch(`/api/stats`).then(r => r.json()).then(setStats), []);

    useEffect(() => { loadCases(); }, [filters]);

    // Coalesce bursts (an import, a busy minute) into one reload.
    function soon(key, fn, ms = 400) {
        clearTimeout(timers.current[key]);
        timers.current[key] = setTimeout(fn, ms);
    }
    function toast(text) {
        const id = Math.random().toString(36).slice(2);
        setToasts(t => [...t.slice(-3), { id, text }]);
        setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 5000);
    }
    function flash(caseId) {
        setFlashing(f => ({ ...f, [caseId]: true }));
        setTimeout(() => setFlashing(f => { const n = { ...f }; delete n[caseId]; return n; }), 1600);
    }

    const status = useLiveFeed(event => {
        if (event.type === "hello") {
            setOnline(event.online);
            loadCases(); loadStats(); // anything before this connection is in the fresh list
            return;
        }
        if (event.type === "presence") { setOnline(event.online); return; }
        if (event.type === "resync") { loadCases(); loadStats(); return; }

        const mine = meRef.current && event.actor === meRef.current.user;
        if (!mine) { const text = describe(event); if (text) toast(text); }
        soon("stats", loadStats);

        if (event.case_id && event.case_id === selectedRef.current) {
            if (event.type === "case.deleted") {
                setSelectedNotice(`${event.actor} deleted this case.`);
            } else if (event.case) {
                setSelectedCase(prev => (!prev || event.case.version > prev.version ? event.case : prev));
                // Anything this tab did not write is news, even your own edit from another device.
                if (!ownWrites.current.has(`${event.case_id}:${event.version}`)) setSelectedNotice(describe(event));
            }
        }

        if (event.seq && event.seq <= listAsOf.current) return; // the list already includes it
        if (event.type === "case.updated") {
            setCases(cs => cs.map(c => (c.case_id === event.case_id && event.case.version > c.version ? event.case : c)));
            if (!mine) flash(event.case_id);
        } else if (event.type === "case.deleted") {
            setCases(cs => cs.filter(c => c.case_id !== event.case_id));
        } else if (event.type === "case.created" || event.type === "cases.imported") {
            soon("list", loadCases); // the server applies the filters
        }
    });

    function canModify(c) { return me && (me.role === "admin" || c.assignee === me.user); }

    function openCase(c) {
        setSelectedId(c.case_id); setSelectedCase(c); setSelectedNotice(null); setView("detail");
        apiFetch(`/api/cases/${c.case_id}`).then(r => (r.ok ? r.json() : null)).then(fresh => fresh && setSelectedCase(fresh));
    }

    async function deleteCase(c) {
        if (!confirm(`Delete ${c.product_id}?`)) return;
        const resp = await apiFetch(`/api/cases/${c.case_id}`, { method: "DELETE", headers: ifMatch(c) });
        if (resp.status === 409) {
            toast(`${c.product_id} changed since you loaded it; not deleted. Review it and try again.`);
            loadCases();
        }
    }

    async function exportData(format) {
        const params = new URLSearchParams(filters);
        params.set("format", format);
        // A plain link cannot carry the Authorization header, so download through fetch.
        const resp = await apiFetch(`/api/export?${params}`);
        if (!resp.ok) return;
        const url = URL.createObjectURL(await resp.blob());
        const a = document.createElement("a");
        a.href = url;
        a.download = format === "json" ? "cases_export.json" : "cases_export.csv";
        a.click();
        URL.revokeObjectURL(url);
    }

    const others = online.filter(u => !me || u !== me.user);
    const liveLabel = { live: "Live", connecting: "Connecting…", reconnecting: "Reconnecting…", unauthorized: "Not signed in" }[status];

    return (
        <div className="max-w-6xl mx-auto px-6 py-10">
            <nav className="fixed top-0 left-0 right-0 z-50 nav-blur border-b border-white/5">
                <div className="max-w-6xl mx-auto px-6 h-14 flex items-center justify-between">
                    <div className="flex items-center gap-3">
                        <span className="text-accent font-semibold text-sm tracking-wide">CaseForge</span>
                        {me && <button onClick={onSwitchUser} title="Use a different token in this tab" className="text-[10px] font-mono text-slate-500 bg-white/5 px-2 py-0.5 rounded hover:text-slate-300">{me.user} ({me.role})</button>}
                        <span className={`live-dot live-${status}`} title={liveLabel}></span>
                        <span className="text-[10px] text-slate-500">{liveLabel}</span>
                        {others.length > 0 && (
                            <span className="text-[10px] text-slate-500" data-testid="online">
                                · also here: {others.map(u => <span key={u} className="presence-chip">{u}</span>)}
                            </span>
                        )}
                    </div>
                    <div className="flex gap-1">
                        {["list", "create", "import"].map(v => (
                            <button key={v} onClick={() => setView(v)} className={`text-[11px] px-3 py-1 rounded-lg transition ${view === v ? "bg-accent/10 text-accent" : "text-slate-500 hover:text-slate-300"}`}>
                                {v === "list" ? "Cases" : v === "create" ? "New" : "Import"}
                            </button>
                        ))}
                        <button onClick={() => exportData("csv")} className="text-[11px] px-3 py-1 rounded-lg text-slate-500 hover:text-slate-300">Export</button>
                    </div>
                </div>
            </nav>

            <div className="toasts" aria-live="polite">
                {toasts.map(t => <div key={t.id} className="toast fade-in">{t.text}</div>)}
            </div>

            <div className="pt-20">
                {stats && (
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6 fade-in">
                        <StatCard label="Open" value={stats.total_open} color="accent" />
                        <StatCard label="Resolved Today" value={stats.resolved_today} color="green" />
                        <StatCard label="Created Today" value={stats.created_today} color="blue" />
                        <StatCard label="High Value Open" value={stats.high_value_open} color="gold" />
                    </div>
                )}

                {view === "list" && (
                    <div className="fade-in space-y-4">
                        <SearchBar filters={filters} setFilters={setFilters} />
                        <div className="space-y-2">
                            {cases.map(c => (
                                <div key={c.case_id} data-case={c.product_id} className={`card p-4 flex items-center justify-between cursor-pointer transition ${c.gms?.is_high_value ? "high-value" : ""} ${flashing[c.case_id] ? "flash" : ""}`} onClick={() => openCase(c)}>
                                    <div className="flex items-center gap-4">
                                        <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full status-${c.status.toLowerCase()}`}>{c.status}</span>
                                        <span className="text-sm font-mono text-slate-300">{c.product_id}</span>
                                        <span className="text-xs text-slate-500">{c.category}</span>
                                    </div>
                                    <div className="flex items-center gap-4">
                                        {c.notes?.length > 0 && <span className="text-[10px] text-slate-500">{c.notes.length} note{c.notes.length === 1 ? "" : "s"}</span>}
                                        {c.gms?.is_high_value && <span className="text-[9px] font-mono text-gold bg-gold/10 px-2 py-0.5 rounded">HV ${c.gms.t4w_gms.toLocaleString()}</span>}
                                        <span className="text-[11px] text-slate-500">{c.assignee}</span>
                                        {canModify(c) && <button onClick={e => { e.stopPropagation(); deleteCase(c); }} className="text-[10px] text-red-400 hover:text-red-300">Delete</button>}
                                    </div>
                                </div>
                            ))}
                            {cases.length === 0 && <p className="text-sm text-slate-500 text-center py-8">No cases match your filters</p>}
                        </div>
                    </div>
                )}

                {view === "detail" && selectedCase && (
                    <CaseDetail
                        c={selectedCase}
                        notice={selectedNotice}
                        clearNotice={() => setSelectedNotice(null)}
                        canModify={canModify(selectedCase)}
                        onChanged={updated => {
                            ownWrites.current.add(`${updated.case_id}:${updated.version}`);
                            setSelectedCase(prev => (!prev || updated.version >= prev.version ? updated : prev));
                        }}
                        onBack={() => { setView("list"); setSelectedId(null); }}
                    />
                )}

                {view === "create" && (
                    <CreateForm categories={categories} onCreated={() => { setView("list"); loadCases(); loadStats(); }} />
                )}

                {view === "import" && <ImportView onDone={() => { setView("list"); loadCases(); loadStats(); }} />}
            </div>
        </div>
    );
}

function StatCard({ label, value, color }) {
    const colorMap = { accent: "text-accent", green: "text-green-400", blue: "text-blue-400", gold: "text-amber-400" };
    return (
        <div className="card p-4 text-center">
            <div className={`text-2xl font-bold ${colorMap[color] || "text-white"}`}>{value}</div>
            <div className="text-[10px] text-slate-500 uppercase tracking-wider mt-1">{label}</div>
        </div>
    );
}

function SearchBar({ filters, setFilters }) {
    return (
        <div className="card p-4 flex flex-wrap gap-3 items-center">
            <input placeholder="Product ID" className="bg-white/3 border border-white/8 rounded-lg px-3 py-1.5 text-xs w-32 outline-none focus:border-accent/40" onChange={e => setFilters(f => ({ ...f, product_id: e.target.value }))} />
            <input placeholder="Assignee" className="bg-white/3 border border-white/8 rounded-lg px-3 py-1.5 text-xs w-28 outline-none focus:border-accent/40" onChange={e => setFilters(f => ({ ...f, assignee: e.target.value }))} />
            <select className="bg-white/3 border border-white/8 rounded-lg px-3 py-1.5 text-xs outline-none focus:border-accent/40 text-slate-300" onChange={e => setFilters(f => ({ ...f, status: e.target.value }))}>
                <option value="">All Status</option>
                {ALL_STATUSES.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
            <label className="flex items-center gap-1 text-[11px] text-slate-400">
                <input type="checkbox" className="accent-amber-400" onChange={e => setFilters(f => ({ ...f, is_high_value: e.target.checked || undefined }))} />
                High Value Only
            </label>
        </div>
    );
}

function CaseDetail({ c, notice, clearNotice, canModify, onChanged, onBack }) {
    const [noteText, setNoteText] = useState("");
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState(c.description);
    const [conflict, setConflict] = useState(null);
    const [error, setError] = useState(null);
    const [busy, setBusy] = useState(false);
    // The version the draft is based on. Live updates keep advancing `c`, so pinning `c.version`
    // at save time would quietly accept whatever changed underneath the draft. The edit must be
    // checked against what the user saw when they started typing.
    const [baseVersion, setBaseVersion] = useState(null);

    // A change arriving while we edit is shown, never merged into the draft.
    const editingStale = editing && conflict === null && baseVersion !== null && c.version !== baseVersion;

    function startEditing() {
        setDraft(c.description); setBaseVersion(c.version); setConflict(null); setEditing(true);
    }

    // `version`: the case version this action was decided on (sent as If-Match), or null for
    // actions that cannot be invalidated by a concurrent change (adding a note).
    async function send(path, opts, { version }) {
        setBusy(true); setError(null);
        try {
            const resp = await apiFetch(path, {
                ...opts,
                headers: { "Content-Type": "application/json", ...(version !== null ? { "If-Match": `"v${version}"` } : {}) },
            });
            const body = await resp.json().catch(() => ({}));
            if (resp.status === 409 && body.detail?.current) {
                const current = body.detail.current;
                onChanged(current);
                setConflict({ message: body.detail.message, description: current.description });
                // The banner shows the latest saved text, so a second Save is an informed one.
                if (editing) setBaseVersion(current.version);
                return null;
            }
            if (!resp.ok) { setError(typeof body.detail === "string" ? body.detail : `Request failed (${resp.status})`); return null; }
            setConflict(null); clearNotice();
            const updated = body.case || body;
            onChanged(updated);
            return updated;
        } finally { setBusy(false); }
    }

    // The buttons render from the live case, so the version on screen is the one decided on.
    const transition = to => send(`/api/cases/${c.case_id}/status`, { method: "POST", body: JSON.stringify({ new_status: to }) }, { version: c.version });
    async function addNote() {
        if (!noteText.trim()) return;
        if (await send(`/api/cases/${c.case_id}/notes`, { method: "POST", body: JSON.stringify({ text: noteText }) }, { version: null })) setNoteText("");
    }
    async function saveDescription() {
        if (await send(`/api/cases/${c.case_id}`, { method: "PATCH", body: JSON.stringify({ description: draft }) }, { version: baseVersion })) {
            setEditing(false); setBaseVersion(null);
        }
    }

    return (
        <div className="fade-in space-y-4">
            <button onClick={onBack} className="text-xs text-slate-500 hover:text-accent">← Back to list</button>

            {notice && !editing && (
                <div className="banner banner-info" role="status">
                    <span>Live update: {notice}</span>
                    <button onClick={clearNotice} className="text-[10px] opacity-70 hover:opacity-100">dismiss</button>
                </div>
            )}
            {editingStale && (
                <div className="banner banner-warn" role="status">
                    <span>This case changed while you were editing (v{baseVersion} → v{c.version}){notice ? `: ${notice}` : ""}. Saving now will be refused so you can review the change first.</span>
                </div>
            )}
            {conflict && (
                <div className="banner banner-warn" role="alert">
                    <span>
                        Not saved. {conflict.message}{" "}
                        {editing
                            ? <>The latest saved description is <q className="text-slate-200">{conflict.description}</q>. Your draft is kept; save again to replace it.</>
                            : "The case below is the latest; review it and try again."}
                    </span>
                    <button onClick={() => setConflict(null)} className="text-[10px] opacity-70 hover:opacity-100">dismiss</button>
                </div>
            )}
            {error && <div className="banner banner-error" role="alert"><span>{error}</span></div>}

            <div className="card glow p-6 space-y-4">
                <div className="flex justify-between items-start">
                    <div>
                        <div className="flex items-center gap-3">
                            <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full status-${c.status.toLowerCase()}`}>{c.status}</span>
                            <h2 className="text-lg font-semibold font-mono">{c.product_id}</h2>
                            <span className="text-[10px] font-mono text-slate-600" title="Version: every change increments it">v{c.version}</span>
                        </div>
                        <p className="text-xs text-slate-500 mt-1">{c.category} · {c.marketplace?.join(", ")}</p>
                    </div>
                    {c.gms?.is_high_value && <span className="text-xs font-mono text-gold bg-gold/10 px-3 py-1 rounded-lg">HIGH VALUE — ${c.gms.t4w_gms.toLocaleString()}</span>}
                </div>

                {editing ? (
                    <div className="space-y-2">
                        <textarea className="w-full" rows={3} value={draft} onChange={e => setDraft(e.target.value)} />
                        <div className="flex gap-2">
                            <button disabled={busy} onClick={saveDescription} className="btn-primary">Save</button>
                            <button onClick={() => { setEditing(false); setBaseVersion(null); setDraft(c.description); setConflict(null); }} className="btn-ghost">Cancel</button>
                        </div>
                    </div>
                ) : (
                    <div className="flex items-start justify-between gap-4">
                        <p className="text-sm text-slate-300">{c.description}</p>
                        {canModify && <button onClick={startEditing} className="btn-ghost shrink-0">Edit</button>}
                    </div>
                )}

                <div className="grid grid-cols-3 gap-4 text-[11px]">
                    <div><span className="text-slate-500">Assignee:</span> <span className="text-slate-300">{c.assignee}</span></div>
                    <div><span className="text-slate-500">Source:</span> <span className="text-slate-300">{c.source}</span></div>
                    <div><span className="text-slate-500">Updated:</span> <span className="text-slate-300 font-mono">{c.updated_at?.slice(0, 16).replace("T", " ")}</span></div>
                </div>

                {canModify && (TRANSITIONS[c.status] || []).length > 0 && (
                    <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-white/5">
                        <span className="text-[10px] uppercase tracking-wider text-slate-500 mr-1">Move to</span>
                        {TRANSITIONS[c.status].map(to => (
                            <button key={to} disabled={busy} onClick={() => transition(to)} className={`text-[11px] px-3 py-1 rounded-lg status-${to.toLowerCase()} hover:brightness-125 disabled:opacity-40`}>{to}</button>
                        ))}
                    </div>
                )}
            </div>

            <div className="card p-5 space-y-3">
                <h3 className="text-[10px] uppercase tracking-wider text-slate-500">Notes</h3>
                {(c.notes || []).map((n, i) => (
                    <div key={i} className="bg-white/3 rounded-lg p-3">
                        <p className="text-xs text-slate-300">{n.text}</p>
                        <p className="text-[10px] text-slate-600 mt-1">{n.author} · {n.timestamp?.slice(0, 16).replace("T", " ")}</p>
                    </div>
                ))}
                <div className="flex gap-2">
                    <input className="flex-1" placeholder="Add a note (append-only)" value={noteText} onChange={e => setNoteText(e.target.value)} onKeyDown={e => e.key === "Enter" && addNote()} />
                    <button disabled={busy || !noteText.trim()} onClick={addNote} className="btn-primary">Add</button>
                </div>
            </div>

            {c.status_history?.length > 0 && (
                <div className="card p-5">
                    <h3 className="text-[10px] uppercase tracking-wider text-slate-500 mb-3">Status Timeline</h3>
                    <div className="space-y-2">
                        {c.status_history.map((h, i) => (
                            <div key={i} className="flex items-center gap-3 text-[11px]">
                                <span className="w-1.5 h-1.5 rounded-full bg-accent"></span>
                                <span className="text-slate-400">{h.from || "—"} → {h.to}</span>
                                <span className="text-slate-600 font-mono">{h.timestamp?.slice(0, 16).replace("T", " ")}</span>
                                <span className="text-slate-600">{h.changed_by}</span>
                            </div>
                        ))}
                    </div>
                </div>
            )}

            {c.edit_history?.length > 0 && (
                <div className="card p-5">
                    <h3 className="text-[10px] uppercase tracking-wider text-slate-500 mb-3">Edit History</h3>
                    <div className="space-y-2">
                        {c.edit_history.map((h, i) => (
                            <div key={i} className="text-[11px] text-slate-400">
                                <span className="text-slate-300">{h.changed_by}</span> changed <span className="font-mono">{h.field}</span>
                                <span className="text-slate-600 font-mono"> · {h.timestamp?.slice(0, 16).replace("T", " ")}</span>
                            </div>
                        ))}
                    </div>
                </div>
            )}
        </div>
    );
}

function CreateForm({ categories, onCreated }) {
    const [form, setForm] = useState({ product_id: "", category: "", marketplace: ["US"], description: "", source: "manual", gms: {} });
    const [catSearch, setCatSearch] = useState("");
    const [gmsTotal, setGmsTotal] = useState(0);

    function updateGms(field, value) {
        const gms = { ...form.gms, [field]: parseFloat(value) || 0 };
        const total = ((gms.shipped_units || 0) * (gms.unit_price || 0)) + (gms.shipping_charge || 0) + (gms.gift_wrap_charge || 0) + (gms.price_discount || 0) + (gms.shipping_discount || 0) + (gms.gift_wrap_discount || 0);
        setForm(f => ({ ...f, gms }));
        setGmsTotal(total);
    }

    async function submit() {
        const resp = await apiFetch(`/api/cases`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(form),
        });
        if (resp.ok) onCreated();
    }

    const filteredCats = categories.filter(c => c.toLowerCase().includes(catSearch.toLowerCase())).slice(0, 8);

    return (
        <div className="fade-in card glow p-6 space-y-5">
            <h2 className="text-lg font-semibold">Create Case</h2>
            <div className="grid grid-cols-2 gap-4">
                <div>
                    <label className="text-[10px] uppercase tracking-wider text-slate-500 block mb-1">Product ID *</label>
                    <input value={form.product_id} onChange={e => setForm(f => ({ ...f, product_id: e.target.value }))} className="w-full bg-white/3 border border-white/8 rounded-xl px-4 py-2.5 text-sm font-mono outline-none focus:border-accent/40" />
                </div>
                <div className="relative">
                    <label className="text-[10px] uppercase tracking-wider text-slate-500 block mb-1">Category *</label>
                    <input value={catSearch || form.category} onChange={e => { setCatSearch(e.target.value); setForm(f => ({ ...f, category: "" })); }} placeholder="Search categories..." className="w-full bg-white/3 border border-white/8 rounded-xl px-4 py-2.5 text-sm outline-none focus:border-accent/40" />
                    {catSearch && !form.category && (
                        <div className="absolute z-10 mt-1 w-full bg-[#0f1629] border border-white/10 rounded-xl overflow-hidden shadow-xl">
                            {filteredCats.map(c => (
                                <button key={c} onClick={() => { setForm(f => ({ ...f, category: c })); setCatSearch(""); }} className="block w-full text-left px-4 py-2 text-xs hover:bg-accent/10 text-slate-300">{c}</button>
                            ))}
                        </div>
                    )}
                </div>
            </div>
            <div>
                <label className="text-[10px] uppercase tracking-wider text-slate-500 block mb-1">Description *</label>
                <textarea value={form.description} onChange={e => setForm(f => ({ ...f, description: e.target.value }))} rows={3} className="w-full bg-white/3 border border-white/8 rounded-xl px-4 py-2.5 text-sm outline-none focus:border-accent/40 resize-none" />
            </div>

            <div>
                <div className="flex justify-between items-center mb-2">
                    <label className="text-[10px] uppercase tracking-wider text-slate-500">GMS Components (T4W)</label>
                    <div className="flex items-center gap-2">
                        <span className="text-sm font-mono text-slate-300">${gmsTotal.toFixed(2)}</span>
                        {gmsTotal >= 1000 && <span className="text-[9px] font-semibold text-gold bg-gold/10 px-2 py-0.5 rounded-full">HIGH VALUE</span>}
                    </div>
                </div>
                <div className="grid grid-cols-4 gap-2">
                    {[["shipped_units", "Units"], ["unit_price", "Price ($)"], ["shipping_charge", "Ship $"], ["gift_wrap_charge", "Gift $"], ["price_discount", "Price Disc"], ["shipping_discount", "Ship Disc"], ["gift_wrap_discount", "Gift Disc"]].map(([field, label]) => (
                        <div key={field}>
                            <label className="text-[9px] text-slate-600 block">{label}</label>
                            <input type="number" step="0.01" onChange={e => updateGms(field, e.target.value)} className="w-full bg-white/3 border border-white/8 rounded-lg px-2 py-1.5 text-[11px] font-mono outline-none focus:border-accent/40" />
                        </div>
                    ))}
                </div>
            </div>

            <button onClick={submit} disabled={!form.product_id || !form.category || !form.description} className="w-full bg-accent hover:bg-accent/90 text-surface font-semibold text-sm py-3 rounded-xl transition shadow-[0_0_20px_-5px_rgba(0,212,170,0.3)] disabled:opacity-40">
                Create Case
            </button>
        </div>
    );
}

function ImportView({ onDone }) {
    const [result, setResult] = useState(null);
    const fileRef = useRef(null);

    async function handleUpload(file) {
        const formData = new FormData();
        formData.append("file", file);
        const resp = await apiFetch(`/api/import`, { method: "POST", body: formData });
        const data = await resp.json();
        setResult(data);
    }

    return (
        <div className="fade-in card glow p-6 space-y-4">
            <h2 className="text-lg font-semibold">Import Cases from CSV</h2>
            <div className="text-center py-8 cursor-pointer" onClick={() => fileRef.current?.click()}>
                <div className="text-accent text-2xl mb-2">⬆</div>
                <p className="text-sm text-slate-300">Drop CSV or click to browse</p>
                <p className="text-[10px] text-slate-500 mt-1">Columns: product_id, category, marketplace, description, shipped_units, unit_price, ...</p>
            </div>
            <input ref={fileRef} type="file" accept=".csv" className="hidden" onChange={e => e.target.files[0] && handleUpload(e.target.files[0])} />
            {result && (
                <div className="bg-white/3 rounded-xl p-4">
                    <p className="text-sm text-accent">{result.imported} cases imported</p>
                    {result.errors?.length > 0 && <p className="text-xs text-red-400 mt-1">{result.errors.length} errors</p>}
                    <button onClick={onDone} className="text-xs text-accent mt-3">← Back to list</button>
                </div>
            )}
        </div>
    );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
