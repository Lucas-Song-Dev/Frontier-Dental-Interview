import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ask,
  getCatalog,
  getJobs,
  getPipeline,
  getProduct,
  getQueries,
  getRows,
  getSettings,
  refreshSnapshot,
  saveSettings,
} from "./api.js";
import { Icon } from "./icons.jsx";

const PANEL_KEY = "desk-panels";
const SIZE_KEY = "desk-sizes";
const LINKS_KEY = "desk-links";
const CHATS_KEY = "desk-chats";
const CRAWL_ANCHOR_KEY = "desk-crawl-anchor";
const CHAT_LIMIT = 20;
const MESSAGE_LIMIT = 40;
const ROW_LIMIT = 40;
const CRAWL_LINKS = [
  { href: "https://www.safcodental.com/catalog/sutures-surgical-products", label: "Sutures seed" },
  { href: "https://www.safcodental.com/catalog/gloves", label: "Gloves seed" },
];
const POLICY_LINKS = [
  { href: "https://www.safcodental.com/robots.txt", label: "robots.txt" },
  { href: "https://www.safcodental.com/sitemap.xml", label: "Sitemap" },
];
const REQUIRED_LINKS = [...CRAWL_LINKS, ...POLICY_LINKS];
const FIELDS = [
  ["crawl_interval_minutes", "Time between crawls", "Minutes from the end of one run until the next may start."],
  ["min_delay_seconds", "Minimum delay", "Seconds between requests. Never below a robots Crawl-delay."],
  ["max_concurrency", "Max concurrency", "How many page fetches may run at once."],
  ["max_products", "Max products per run", "Stop a run after this many parent products."],
  ["max_pages_per_category", "Max pages per category", "Live ?page= links stay disallowed."],
];

function crawlIntervalMs(settings) {
  const minutes = Number(settings?.settings?.crawl_interval_minutes || 360);
  return Math.max(minutes, 15) * 60 * 1000;
}

function crawlDeadline(jobs, settings, anchor) {
  const interval = crawlIntervalMs(settings);
  const latest = jobs.find((job) => job.finished_at || job.started_at);
  const stamp = latest?.finished_at || latest?.started_at || "";
  const parsed = Date.parse(stamp);
  if (Number.isFinite(parsed)) return parsed + interval;
  if (!anchor) return null;
  return anchor + interval;
}

function formatCountdown(ms) {
  if (ms <= 0) return "Due";
  const total = Math.ceil(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const pad = (value) => String(value).padStart(2, "0");
  if (hours > 0) return `${hours}h ${pad(minutes)}m ${pad(seconds)}s`;
  if (minutes > 0) return `${minutes}m ${pad(seconds)}s`;
  return `${seconds}s`;
}

function clamp(value, min, max, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.min(max, Math.max(min, number));
}

function readSizes() {
  const base = { sidebar: 240, aux: 280, bottom: 200 };
  try {
    const saved = JSON.parse(localStorage.getItem(SIZE_KEY) || "{}");
    return {
      sidebar: clamp(saved.sidebar, 160, 560, base.sidebar),
      aux: clamp(saved.aux, 180, 640, base.aux),
      bottom: clamp(saved.bottom, 96, 640, base.bottom),
    };
  } catch {
    return base;
  }
}

function parseRobots(text) {
  const disallows = [];
  const sitemaps = [];
  for (const raw of String(text || "").split("\n")) {
    const line = raw.split("#")[0].trim();
    const match = line.match(/^(disallow|sitemap)\s*:\s*(.*)$/i);
    if (!match || !match[2].trim()) continue;
    if (match[1].toLowerCase() === "sitemap") sitemaps.push(match[2].trim());
    else disallows.push(match[2].trim());
  }
  return { disallows, sitemaps };
}

function policyLinks(robotsText) {
  const parsed = parseRobots(robotsText);
  const sitemaps = parsed.sitemaps.length ? parsed.sitemaps : POLICY_LINKS.map((item) => item.href).filter((href) => href.endsWith("sitemap.xml"));
  return [{ href: "https://www.safcodental.com/robots.txt" }, ...sitemaps.map((href) => ({ href }))];
}

function linkKey(href) {
  try {
    const url = new URL(href);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    return `${url.origin}${path}${url.search}`;
  } catch {
    return href;
  }
}

function readExtraLinks() {
  try {
    const saved = JSON.parse(localStorage.getItem(LINKS_KEY) || "[]");
    if (!Array.isArray(saved)) return [];
    const required = new Set(REQUIRED_LINKS.map((item) => linkKey(item.href)));
    return saved.filter((item) => item && typeof item.href === "string" && item.href.startsWith("https://") && !required.has(linkKey(item.href))).map((item, index) => ({
      id: typeof item.id === "string" && item.id ? item.id : `extra-${index}`,
      href: item.href,
    }));
  } catch {
    return [];
  }
}

function chatId() {
  return `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function titleFrom(who) {
  const line = String(who || "").replace(/\s+/g, " ").trim();
  if (!line) return "New chat";
  return line.length > 48 ? `${line.slice(0, 45).trimEnd()}…` : line;
}

function blankChat() {
  return { id: chatId(), title: "New chat", updatedAt: Date.now(), thread: [] };
}

function normalizeEntry(raw) {
  if (!raw || typeof raw !== "object") return null;
  const columns = Array.isArray(raw.columns) ? raw.columns.map((column) => String(column)) : [];
  const rows = Array.isArray(raw.rows)
    ? raw.rows.slice(0, ROW_LIMIT).map((row) => (Array.isArray(row) ? row.map((cell) => (cell == null ? null : String(cell))) : []))
    : [];
  return {
    who: String(raw.who || ""),
    answer: String(raw.answer || ""),
    sql: String(raw.sql || ""),
    columns,
    rows,
    query_id: String(raw.query_id || ""),
  };
}

function normalizeChat(raw) {
  if (!raw || typeof raw !== "object") return null;
  const thread = Array.isArray(raw.thread) ? raw.thread.slice(-MESSAGE_LIMIT).map(normalizeEntry).filter(Boolean) : [];
  const updatedAt = Number(raw.updatedAt);
  return {
    id: typeof raw.id === "string" && raw.id ? raw.id : chatId(),
    title: typeof raw.title === "string" && raw.title.trim() ? raw.title.trim().slice(0, 80) : "New chat",
    updatedAt: Number.isFinite(updatedAt) ? updatedAt : Date.now(),
    thread,
  };
}

function sortChats(chats) {
  return [...chats].sort((left, right) => right.updatedAt - left.updatedAt);
}

function readChats() {
  const fresh = () => {
    const chat = blankChat();
    return { activeId: chat.id, chats: [chat] };
  };
  try {
    const parsed = JSON.parse(localStorage.getItem(CHATS_KEY) || "null");
    const chats = Array.isArray(parsed?.chats) ? sortChats(parsed.chats.map(normalizeChat).filter(Boolean)).slice(0, CHAT_LIMIT) : [];
    if (!chats.length) return fresh();
    const activeId = chats.some((chat) => chat.id === parsed.activeId) ? parsed.activeId : chats[0].id;
    return { activeId, chats };
  } catch {
    return fresh();
  }
}

function persistChats(store) {
  const compact = {
    ...store,
    chats: store.chats.slice(0, 8).map((chat) => ({
      ...chat,
      thread: chat.thread.slice(-10).map((entry) => ({ ...entry, rows: [], columns: [] })),
    })),
  };
  for (const attempt of [store, compact]) {
    try {
      localStorage.setItem(CHATS_KEY, JSON.stringify(attempt));
      return;
    } catch {
      // The browser refused the larger copy. The shorter one is the fallback.
    }
  }
}

function withExchange(store, entry) {
  const exchange = normalizeEntry(entry);
  if (!exchange) return store;
  const activeId = store.chats.some((chat) => chat.id === store.activeId) ? store.activeId : store.chats[0].id;
  const chats = store.chats.map((chat) => {
    if (chat.id !== activeId) return chat;
    const thread = [...chat.thread, exchange].slice(-MESSAGE_LIMIT);
    const title = chat.thread.length === 0 ? titleFrom(exchange.who) : chat.title;
    return { ...chat, title, thread, updatedAt: Date.now() };
  });
  return { activeId, chats: sortChats(chats).slice(0, CHAT_LIMIT) };
}

function withNewChat(store) {
  const empty = store.chats.find((chat) => chat.thread.length === 0);
  if (empty) return { ...store, activeId: empty.id };
  const chat = blankChat();
  return { activeId: chat.id, chats: sortChats([chat, ...store.chats]).slice(0, CHAT_LIMIT) };
}

function withoutChat(store, id) {
  const chats = store.chats.filter((chat) => chat.id !== id);
  if (!chats.length) {
    const chat = blankChat();
    return { activeId: chat.id, chats: [chat] };
  }
  const activeId = chats.some((chat) => chat.id === store.activeId) ? store.activeId : chats[0].id;
  return { activeId, chats };
}

function ageLabel(updatedAt, now) {
  const minutes = Math.max(0, Math.floor((now - updatedAt) / 60000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

function readPanels() {
  const base = { primary: true, secondary: true, bottom: false };
  try {
    return { ...base, ...JSON.parse(localStorage.getItem(PANEL_KEY) || "{}") };
  } catch {
    return base;
  }
}

function usePath() {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const go = useCallback((next) => {
    if (next === window.location.pathname) return;
    window.history.pushState({}, "", next);
    setPath(next);
  }, []);
  return [path, go];
}

function activityFor(path) {
  if (path === "/chat") return "ask";
  if (path === "/jobs" || /^\/jobs\/\d+$/.test(path)) return "jobs";
  if (path === "/settings" || path === "/links") return "settings";
  return "catalog";
}

function titleFor(path, catalog, product, chatTitle) {
  if (path === "/chat") return chatTitle && chatTitle !== "New chat" ? chatTitle : "Ask";
  const job = path.match(/^\/jobs\/(\d+)$/);
  if (path === "/jobs") return "Jobs";
  if (job) return `Job ${job[1]}`;
  if (path === "/settings") return "Settings";
  if (path === "/links") return "Links";
  if (path === "/gate") return "Gate";
  if (path === "/pipeline") return "Pipeline";
  if (path === "/data") return "All rows";
  if (product) return product.name;
  const category = path.match(/^\/category\/([a-z]+)$/);
  if (category && catalog) {
    return catalog.categories.find((item) => item.key === category[1])?.title || "Category";
  }
  return "Overview";
}

export default function App() {
  const [path, go] = usePath();
  const [panels, setPanels] = useState(readPanels);
  const [sizes, setSizes] = useState(readSizes);
  const sizeAtDrag = useRef(sizes);
  const [catalog, setCatalog] = useState(null);
  const [queries, setQueries] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [settings, setSettings] = useState(null);
  const [product, setProduct] = useState(null);
  const [chats, setChats] = useState(readChats);
  const [output, setOutput] = useState(null);
  const [status, setStatus] = useState("");
  const [auto, setAuto] = useState(() => localStorage.getItem("desk-auto") === "on");
  const [anchor, setAnchor] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const [extraLinks, setExtraLinks] = useState(readExtraLinks);
  const [error, setError] = useState("");

  const loadDesk = useCallback(async () => {
    const [nextCatalog, nextQueries, nextJobs, nextSettings] = await Promise.all([
      getCatalog(),
      getQueries(),
      getJobs(),
      getSettings(),
    ]);
    setCatalog(nextCatalog);
    setQueries(nextQueries);
    setJobs(nextJobs.jobs || []);
    setSettings(nextSettings);
    setError("");
  }, []);

  useEffect(() => {
    loadDesk().catch((exc) => setError(exc.message));
  }, [loadDesk]);

  const slug = path.match(/^\/product\/([a-z0-9-]+)$/)?.[1] || "";
  useEffect(() => {
    if (!slug) {
      setProduct(null);
      return;
    }
    getProduct(slug).then(setProduct).catch(() => setProduct(null));
  }, [slug]);

  useEffect(() => {
    localStorage.setItem(SIZE_KEY, JSON.stringify(sizes));
  }, [sizes]);

  useEffect(() => {
    persistChats(chats);
  }, [chats]);

  function applySize(key, delta, isStart, sign) {
    if (isStart) {
      sizeAtDrag.current = sizes;
      return;
    }
    const limits = { sidebar: [160, 560], aux: [180, 640], bottom: [96, Math.max(120, window.innerHeight - 140)] };
    const [min, max] = limits[key];
    setSizes((current) => ({
      ...current,
      [key]: clamp(sizeAtDrag.current[key] + sign * delta, min, max, sizeAtDrag.current[key]),
    }));
  }

  function writePanels(next) {
    setPanels(next);
    localStorage.setItem(PANEL_KEY, JSON.stringify(next));
  }

  function togglePanel(key) {
    writePanels({ ...panels, [key]: !panels[key] });
  }

  function openPanel(key) {
    if (!panels[key]) writePanels({ ...panels, [key]: true });
  }

  async function refresh() {
    setStatus("Sending the crawl…");
    try {
      const result = await refreshSnapshot();
      const next = await getJobs();
      const rows = next.jobs || [];
      setJobs(rows);
      if (!rows.some((job) => job.status === "running")) {
        await loadDesk();
        setStatus("Fetched safcodental.com");
        return;
      }
      setStatus(`Job #${result.job_id} is running`);
      go(`/jobs/${result.job_id}`);
    } catch (exc) {
      setStatus(exc.message);
    }
  }

  const crawlRunning = jobs.some((job) => job.status === "running");
  useEffect(() => {
    if (!crawlRunning) return undefined;
    let cancelled = false;
    const timer = setInterval(() => {
      getJobs().then((next) => {
        if (cancelled) return;
        const rows = next.jobs || [];
        if (rows.some((job) => job.status === "running")) {
          setJobs(rows);
          return;
        }
        loadDesk().catch((exc) => {
          if (!cancelled) setError(exc.message);
        });
      }).catch((exc) => {
        if (!cancelled) setStatus(exc.message);
      });
    }, 1000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [crawlRunning, loadDesk]);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!settings || jobs.some((job) => job.finished_at || job.started_at)) return;
    const saved = Number(localStorage.getItem(CRAWL_ANCHOR_KEY));
    if (Number.isFinite(saved) && saved > 0) {
      setAnchor(saved);
      return;
    }
    const stamp = Date.now();
    localStorage.setItem(CRAWL_ANCHOR_KEY, String(stamp));
    setAnchor(stamp);
  }, [settings, jobs]);

  const nextCrawl = settings ? crawlDeadline(jobs, settings, anchor) : null;
  const countdown = nextCrawl === null ? "" : formatCountdown(nextCrawl - now);
  const refreshRef = useRef(refresh);
  const crawledFor = useRef(0);
  refreshRef.current = refresh;

  useEffect(() => {
    if (!auto || nextCrawl === null || now < nextCrawl || crawledFor.current === nextCrawl) return;
    crawledFor.current = nextCrawl;
    refreshRef.current();
  }, [auto, now, nextCrawl]);

  function toggleAuto() {
    const next = !auto;
    setAuto(next);
    if (!next) crawledFor.current = 0;
    localStorage.setItem("desk-auto", next ? "on" : "off");
  }

  async function runQuery(query, sku) {
    if (!query.allowed) {
      setOutput({
        title: query.title,
        answer: `${queries.level} cannot run this. ${query.level} or above can.`,
        sql: query.sql,
        columns: [],
        rows: [],
      });
      openPanel("bottom");
      return;
    }
    const reply = await ask({ query_id: query.id, sku: sku || "" });
    remember({ who: query.title, ...reply });
    if (path !== "/chat") go("/chat");
  }

  async function submitQuestion(question) {
    const reply = await ask({ question });
    remember({ who: question, ...reply });
  }

  function remember(entry) {
    setChats((current) => withExchange(current, entry));
    setOutput({ title: entry.who, ...entry });
    openPanel("bottom");
  }

  function selectChat(id) {
    setChats((current) => ({ ...current, activeId: id }));
    const chat = chats.chats.find((item) => item.id === id);
    const last = chat?.thread.at(-1);
    setOutput(last ? { title: last.who, ...last } : null);
    if (path !== "/chat") go("/chat");
  }

  function startChat() {
    setChats(withNewChat);
    setOutput(null);
    if (path !== "/chat") go("/chat");
    openPanel("primary");
  }

  function removeChat(id) {
    const next = withoutChat(chats, id);
    setChats(next);
    if (next.activeId === chats.activeId) return;
    const chat = next.chats.find((item) => item.id === next.activeId);
    const last = chat?.thread.at(-1);
    setOutput(last ? { title: last.who, ...last } : null);
  }

  const activeChat = chats.chats.find((chat) => chat.id === chats.activeId) || chats.chats[0];
  const thread = activeChat?.thread || [];
  const activity = activityFor(path);
  const tab = titleFor(path, catalog, product, activeChat?.title);

  const tree = useMemo(() => {
    if (!catalog) return [];
    if (activity === "catalog") {
      return [
        { key: "readme", label: "Readme", items: [{ href: "/", label: "Overview" }] },
        {
          key: "catalog",
          label: "Catalog",
          items: catalog.categories.map((category) => ({ href: `/category/${category.key}`, label: category.title })),
        },
        { key: "data", label: "Data", items: [{ href: "/data", label: "All rows" }, { href: "/pipeline", label: "Pipeline" }] },
        { key: "gate", label: "Gate", items: [{ href: "/gate", label: "Robots" }] },
      ];
    }
    if (activity === "ask") return [];
    if (activity === "jobs") {
      return [
        { key: "runs", label: "Runs", items: [{ href: "/jobs", label: "All runs" }] },
        {
          key: "recent",
          label: "Recent",
          items: jobs.map((job) => ({ href: `/jobs/${job.id}`, label: `#${job.id} ${job.status}` })),
        },
      ];
    }
    return [
      { key: "desk", label: "Desk", items: [{ href: "/settings", label: "Desk settings" }] },
      { key: "links", label: "Links", items: [{ href: "/links", label: "All links" }] },
    ];
  }, [activity, catalog, jobs]);

  function writeLinks(next) {
    setExtraLinks(next);
    localStorage.setItem(LINKS_KEY, JSON.stringify(next));
  }

  function knownLinkKeys() {
    const policy = policyLinks(catalog?.gate?.robots || "");
    return new Set([...CRAWL_LINKS, ...policy, ...extraLinks].map((item) => linkKey(item.href)));
  }

  function addLink(raw) {
    const href = normalizeHref(raw);
    if (!href) return "Enter an https URL.";
    if (knownLinkKeys().has(linkKey(href))) return "Already added.";
    writeLinks([...extraLinks, { id: `extra-${Date.now()}`, href }]);
    return "";
  }

  function updateLink(id, raw) {
    const href = normalizeHref(raw);
    if (!href) return false;
    const current = extraLinks.find((item) => item.id === id);
    if (!current) return false;
    const taken = [...CRAWL_LINKS, ...policyLinks(catalog?.gate?.robots || ""), ...extraLinks].some((item) => linkKey(item.href) === linkKey(href) && item.id !== id);
    if (taken) return false;
    writeLinks(extraLinks.map((item) => (item.id === id ? { ...item, href } : item)));
    return true;
  }

  function removeLink(id) {
    writeLinks(extraLinks.filter((item) => item.id !== id));
  }

  function onActivity(id, href) {
    if (activity === id) {
      togglePanel("primary");
      return;
    }
    go(href);
    openPanel("primary");
  }

  return (
    <div
      className={panels.bottom ? "workbench has-panel" : "workbench"}
      style={panels.bottom ? { gridTemplateRows: `35px minmax(0, 1fr) ${sizes.bottom}px 22px` } : undefined}
    >
      <header className="titlebar">
        <strong className="mark">Catalog desk</strong>
        <span className="title-path">{tab}</span>
        <div className="layout-controls">
          {countdown ? (
            <span className="crawl-countdown" title={nextCrawl ? `Next crawl ${new Date(nextCrawl).toLocaleString()}` : undefined} aria-label={`Next crawl ${countdown}`}>
              {countdown}
            </span>
          ) : null}
          <IconButton className="text-btn" label="Fetch allowed Safco URLs" tip="below" onClick={refresh}>Refresh</IconButton>
          <IconButton className="icon-btn" label="Primary side bar" tip="below" pressed={panels.primary} onClick={() => togglePanel("primary")}>
            <Icon name="left" />
          </IconButton>
          <IconButton className="icon-btn" label="Panel" tip="below" pressed={panels.bottom} onClick={() => togglePanel("bottom")}>
            <Icon name="bottom" />
          </IconButton>
          <IconButton className="icon-btn" label="Secondary side bar" tip="below" pressed={panels.secondary} onClick={() => togglePanel("secondary")}>
            <Icon name="right" />
          </IconButton>
        </div>
      </header>
      <div className="shell">
        <nav className="activity" aria-label="Activity">
          <IconButton label="Catalog" current={activity === "catalog"} onClick={() => onActivity("catalog", "/")}>
            <Icon name="files" />
          </IconButton>
          <IconButton label="Ask" current={activity === "ask"} onClick={() => onActivity("ask", "/chat")}>
            <Icon name="comment" />
          </IconButton>
          <IconButton label="Jobs" current={activity === "jobs"} onClick={() => onActivity("jobs", "/jobs")}>
            <Icon name="history" />
          </IconButton>
          <IconButton label="Settings" current={activity === "settings"} onClick={() => onActivity("settings", "/settings")}>
            <Icon name="gear" />
          </IconButton>
        </nav>
        {panels.primary ? (
          <aside className="sidebar" style={{ width: sizes.sidebar }}>
            {activity === "ask" ? (
              <ChatList chats={chats.chats} activeId={activeChat?.id || ""} now={now} onOpen={selectChat} onNew={startChat} onDelete={removeChat} />
            ) : (
              <div className="tree">
                {tree.map((group) => (
                  <div className="tree-section" key={group.key}>
                    {group.label ? <div className="tree-label">{group.label}</div> : null}
                    {group.items.map((item) => (
                      <a key={item.href} href={item.href} className={path === item.href ? "is-current" : ""} onClick={(event) => { event.preventDefault(); go(item.href); }}>
                        {item.label}
                      </a>
                    ))}
                  </div>
                ))}
              </div>
            )}
          </aside>
        ) : null}
        {panels.primary ? (
          <Sash orientation="vertical" label="Resize primary side bar" onResize={(delta, isStart) => applySize("sidebar", delta, isStart, 1)} />
        ) : null}
        <main className="editor">
          <div className="editor-body">
            {error ? <p className="page-note">{error}</p> : null}
            {!catalog ? <p className="page-note">Loading snapshot…</p> : (
              <Editor
                path={path}
                catalog={catalog}
                product={product}
                jobs={jobs}
                settings={settings}
                thread={thread}
                chatId={activeChat?.id || ""}
                go={go}
                onAsk={submitQuestion}
                links={extraLinks}
                onAddLink={addLink}
                onUpdateLink={updateLink}
                onRemoveLink={removeLink}
                onSaved={async () => { await loadDesk(); setStatus("Settings saved"); }}
              />
            )}
          </div>
        </main>
        {panels.secondary ? (
          <Sash orientation="vertical" label="Resize secondary side bar" onResize={(delta, isStart) => applySize("aux", delta, isStart, -1)} />
        ) : null}
        {panels.secondary ? (
          <aside className="aux" style={{ width: sizes.aux }}>
            <div className="side-title">Queries</div>
            <QueryList queries={queries} onRun={runQuery} />
          </aside>
        ) : null}
      </div>
      {panels.bottom ? (
        <section className="panel" aria-label="Panel">
          <Sash orientation="horizontal" label="Resize panel" onResize={(delta, isStart) => applySize("bottom", delta, isStart, -1)} />
          <div className="side-title">Output</div>
          <div className="panel-body">
            <Output output={output} />
          </div>
        </section>
      ) : null}
      <footer className="statusbar">
        <span>Snapshot {catalog?.snapshot || "…"}</span>
        <span>{catalog?.cadence || ""}</span>
        <span className="status-level">{catalog ? catalog.level : ""}</span>
        <span className="status-gap" />
        <span>{status}</span>
        <button type="button" className="status-auto" aria-pressed={auto} onClick={toggleAuto}>
          Auto {auto ? "on" : "off"}
        </button>
      </footer>
    </div>
  );
}

function normalizeHref(raw) {
  let href = raw.trim();
  if (!href) return "";
  if (!/^https?:\/\//i.test(href)) href = `https://${href}`;
  try {
    const url = new URL(href);
    if (url.protocol !== "https:") return "";
    return url.toString();
  } catch {
    return "";
  }
}

function IconButton({ label, tip = "right", pressed, current, className = "", children, ...props }) {
  return (
    <button
      type="button"
      className={`has-tip tip-${tip}${className ? ` ${className}` : ""}`}
      aria-label={label}
      aria-pressed={pressed}
      aria-current={current ? "page" : undefined}
      {...props}
    >
      {children}
      <span className="tip" aria-hidden="true">{label}</span>
    </button>
  );
}

function Sash({ orientation, label, onResize }) {
  function down(event) {
    event.preventDefault();
    const handle = event.currentTarget;
    try {
      handle.setPointerCapture(event.pointerId);
    } catch {
      /* Capture can fail for a pointer the browser did not start. */
    }
    const origin = orientation === "vertical" ? event.clientX : event.clientY;
    onResize(0, true);
    document.body.classList.add(orientation === "vertical" ? "is-col-resize" : "is-row-resize");
    function move(ev) {
      const point = orientation === "vertical" ? ev.clientX : ev.clientY;
      onResize(point - origin, false);
    }
    function up() {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      document.body.classList.remove("is-col-resize", "is-row-resize");
    }
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }
  return (
    <button
      type="button"
      className={orientation === "vertical" ? "sash sash-col" : "sash sash-row"}
      aria-orientation={orientation}
      aria-label={label}
      onPointerDown={down}
    />
  );
}

function Editor({ path, catalog, product, jobs, settings, thread, chatId, links, go, onAsk, onSaved, onAddLink, onUpdateLink, onRemoveLink }) {
  if (path === "/chat") return <AskView key={chatId} thread={thread} catalog={catalog} onAsk={onAsk} />;
  const jobMatch = path.match(/^\/jobs\/(\d+)$/);
  if (path === "/jobs" || jobMatch) {
    return <JobsView jobs={jobs} jobId={jobMatch ? Number(jobMatch[1]) : null} go={go} />;
  }
  if (path.startsWith("/jobs/")) return <p className="page-note">That run is not in the snapshot.</p>;
  if (path === "/settings") return <SettingsView pack={settings} onSaved={onSaved} />;
  if (path === "/links") {
    return (
      <LinksView
        catalog={catalog}
        links={links}
        onAdd={onAddLink}
        onUpdate={onUpdateLink}
        onRemove={onRemoveLink}
      />
    );
  }
  if (path === "/gate") return <GateView catalog={catalog} />;
  if (path === "/pipeline") return <PipelineView />;
  if (path === "/data") return <DataView go={go} stamp={`${catalog.snapshot}:${catalog.products.length}`} />;
  const categoryKey = path.match(/^\/category\/([a-z]+)$/)?.[1];
  if (categoryKey) {
    const category = catalog.categories.find((item) => item.key === categoryKey);
    if (!category) return <p className="page-note">That category is not in the snapshot.</p>;
    return <CategoryView category={category} catalog={catalog} go={go} />;
  }
  if (path.startsWith("/product/")) {
    if (!product) return <p className="page-note">Loading product…</p>;
    return <ProductView product={product} go={go} />;
  }
  return <Overview catalog={catalog} go={go} />;
}

function Overview({ catalog, go }) {
  const products = catalog.products.length;
  const variants = catalog.products.reduce((sum, item) => sum + item.variants, 0);
  return (
    <div className="page readme">
      <h2>Catalog desk</h2>
      <p>Local desk for the Safco snapshot. It stores the two seed categories, the product sheets fetched from allowed URLs, and the robots gate. Listing <span className="mono">?page=</span> is not requested. Child SKUs come from <span className="mono">window.masterData</span> on each product page.</p>
      <p>This snapshot has {products} products and {variants} child SKUs. Open Data for the table, or Pipeline for the extraction run.</p>
      <h2>Tutorial</h2>
      <ol>
        <li>
          Open <a href="/data" onClick={(event) => { event.preventDefault(); go("/data"); }}>All rows</a>. The table lists every child SKU.
        </li>
        <li>Type in the filter to narrow by SKU, name, stock, or price. Use the category and stock menus the same way. Click a column heading to sort.</li>
        <li>Open the queries bar with the right-hand layout icon. Your level decides which listed query can run. Results open in the bottom panel as a table.</li>
        <li>Refresh fetches the two seed URLs and the product URLs on those pages. Auto repeats that on the crawl interval from Settings.</li>
        <li>Settings switches viewer, operator, and admin. Operator and admin unlock the gate and job queries.</li>
        <li>
          <a href="/gate" onClick={(event) => { event.preventDefault(); go("/gate"); }}>Robots</a> shows the saved robots.txt. A person still has to write crawl allowed as yes or no.
        </li>
      </ol>
    </div>
  );
}

const DATA_COLUMNS = [
  ["sku", "SKU"],
  ["variant_name", "Name"],
  ["brand", "Brand"],
  ["pack_size", "Pack"],
  ["item_group", "Group"],
  ["mfr_number", "Mfr #"],
  ["stock", "Stock"],
  ["variant_price", "Price"],
  ["product_name", "Product"],
  ["parent_sku", "Parent SKU"],
  ["category_path", "Category path"],
  ["availability", "Availability"],
];

function money(value) {
  if (value === null || value === undefined || value === "") return "";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return `$${number.toFixed(2)}`;
}

function cellText(row, key) {
  if (key === "variant_price" || key === "parent_price") return money(row[key]);
  return row[key] == null ? "" : String(row[key]);
}

function DataView({ go, stamp }) {
  const [pack, setPack] = useState(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [stock, setStock] = useState("");
  const [sort, setSort] = useState({ key: "product_name", dir: 1 });

  useEffect(() => {
    let cancel = false;
    getRows()
      .then((payload) => { if (!cancel) setPack(payload); })
      .catch((exc) => { if (!cancel) setError(exc.message); });
    return () => { cancel = true; };
  }, [stamp]);

  const rows = pack?.rows || [];
  const categories = [...new Set(rows.map((row) => row.category_title))].sort();
  const stocks = [...new Set(rows.map((row) => row.stock))].sort();
  const needle = query.trim().toLowerCase();
  const shown = rows.filter((row) => {
    if (category && row.category_title !== category) return false;
    if (stock && row.stock !== stock) return false;
    if (!needle) return true;
    return DATA_COLUMNS.some(([key]) => cellText(row, key).toLowerCase().includes(needle));
  });
  const sorted = [...shown].sort((left, right) => {
    const a = left[sort.key];
    const b = right[sort.key];
    if (sort.key === "variant_price") {
      const an = a == null ? Number.POSITIVE_INFINITY : Number(a);
      const bn = b == null ? Number.POSITIVE_INFINITY : Number(b);
      return (an - bn) * sort.dir;
    }
    return String(a ?? "").localeCompare(String(b ?? "")) * sort.dir;
  });

  function toggleSort(key) {
    setSort((current) => (current.key === key ? { key, dir: current.dir * -1 } : { key, dir: 1 }));
  }

  if (error) return <p className="page-note">{error}</p>;
  if (!pack) return <p className="page-note">Loading rows…</p>;
  return (
    <div className="data-view">
      <form className="data-toolbar" onSubmit={(event) => event.preventDefault()}>
        <label>
          <span className="sr">Filter</span>
          <input value={query} placeholder="Filter SKU, name, stock, price" onChange={(event) => setQuery(event.target.value)} />
        </label>
        <label>
          <span className="sr">Category</span>
          <select value={category} onChange={(event) => setCategory(event.target.value)}>
            <option value="">All categories</option>
            {categories.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label>
          <span className="sr">Stock</span>
          <select value={stock} onChange={(event) => setStock(event.target.value)}>
            <option value="">All stock</option>
            {stocks.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <span className="meta">{sorted.length} of {rows.length} rows</span>
        <span className="export-links">
          <a href="/api/export.csv">CSV</a>
          <a href="/api/export.json">JSON</a>
          <a href="/api/export.xlsx">Excel</a>
        </span>
      </form>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {DATA_COLUMNS.map(([key, label]) => (
                <th key={key} aria-sort={sort.key === key ? (sort.dir > 0 ? "ascending" : "descending") : "none"}>
                  <button type="button" onClick={() => toggleSort(key)}>{label}</button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => (
              <tr key={row.sku}>
                {DATA_COLUMNS.map(([key]) => (
                  <td key={key} className={key === "sku" || key === "mfr_number" || key === "parent_sku" ? "mono" : ""}>
                    {key === "product_name" ? (
                      <a href={`/product/${row.product_slug}`} onClick={(event) => { event.preventDefault(); go(`/product/${row.product_slug}`); }}>{cellText(row, key)}</a>
                    ) : cellText(row, key)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CategoryView({ category, catalog, go }) {
  const extra = catalog.products.filter((item) => item.category === category.key && !category.cards.some((card) => card.slug === item.slug));
  return (
    <div className="page">
      <p className="lead">{category.stats} on the saved listing. Further pages use ?page=, which robots.txt disallows.</p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Product</th>
              <th>Manufacturer</th>
              <th>Price</th>
              <th>Image</th>
              <th>Sheet</th>
            </tr>
          </thead>
          <tbody>
            {category.cards.map((card) => {
              const sheet = catalog.products.find((item) => item.slug === card.slug);
              const image = sheet?.image || "";
              return (
                <tr key={card.slug}>
                  <td>{card.saved ? <a href={`/product/${card.slug}`} onClick={(event) => { event.preventDefault(); go(`/product/${card.slug}`); }}>{card.name}</a> : card.name}</td>
                  <td>{card.manufacturer}</td>
                  <td>{card.price}</td>
                  <td>{image ? <img className="thumb" src={image} alt="" /> : ""}</td>
                  <td>{card.saved ? "Saved" : ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {extra.length ? (
        <>
          <h2>Also saved from this category</h2>
          <ul className="plain">
            {extra.map((item) => (
              <li key={item.slug}><a href={`/product/${item.slug}`} onClick={(event) => { event.preventDefault(); go(`/product/${item.slug}`); }}>{item.name}</a></li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}

function ProductView({ product, go }) {
  const grouped = product.variants.some((row) => row.group);
  const images = product.images || [];
  const alternatives = product.alternatives || [];
  return (
    <div className="page">
      <p className="crumbs">
        <a href="/" onClick={(event) => { event.preventDefault(); go("/"); }}>Overview</a>
        <span> / </span>
        <a href={`/category/${product.category}`} onClick={(event) => { event.preventDefault(); go(`/category/${product.category}`); }}>{product.category_title}</a>
        <span> / {product.name}</span>
      </p>
      {images[0] ? <img className="product-photo" src={images[0]} alt="" /> : null}
      <dl className="kv">
        <div><dt>Brand</dt><dd>{product.brand || ""}</dd></div>
        <div><dt>Parent SKU</dt><dd className="mono">{product.sku}</dd></div>
        <div><dt>Category</dt><dd>{product.category_path || product.category_title}</dd></div>
        <div><dt>URL</dt><dd>{product.url ? <a href={product.url}>{product.url}</a> : ""}</dd></div>
        <div><dt>Price</dt><dd>{product.price}</dd></div>
        <div><dt>Availability</dt><dd>{product.availability}</dd></div>
        <div><dt>Child SKUs</dt><dd>{product.variants.length}</dd></div>
      </dl>
      {product.description ? <p className="prose">{product.description}</p> : null}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>SKU</th>
              <th>Name</th>
              {grouped ? <th>Group</th> : null}
              <th>Mfr #</th>
              <th>Pack</th>
              <th>Stock</th>
              <th>Price</th>
            </tr>
          </thead>
          <tbody>
            {product.variants.map((row) => (
              <tr key={row.sku}>
                <td className="mono">{row.sku}</td>
                <td>{row.name}</td>
                {grouped ? <td>{row.group}</td> : null}
                <td className="mono">{row.mfr}</td>
                <td>{row.pack_size}</td>
                <td>{row.stock}</td>
                <td>{row.price}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {alternatives.length ? (
        <>
          <h2>Alternatives</h2>
          <ul className="plain">
            {alternatives.map((item) => (
              <li key={item.url}><a href={item.url}>{item.name}</a></li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}

function PipelineView() {
  const [pack, setPack] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancel = false;
    getPipeline()
      .then((payload) => { if (!cancel) setPack(payload); })
      .catch((exc) => { if (!cancel) setError(exc.message); });
    return () => { cancel = true; };
  }, []);
  if (error) return <p className="page-note">{error}</p>;
  if (!pack) return <p className="page-note">Loading pipeline…</p>;
  return (
    <div className="page">
      <p className="lead">The run that filled this snapshot. Listing pagination stays disallowed. Extraction uses the page HTML, not a model.</p>
      <div className="export-links">
        {pack.exports.map((href) => <a key={href} href={href}>{href.split(".").pop().toUpperCase()}</a>)}
      </div>
      <h2>Stages</h2>
      <dl className="kv">
        {pack.stages.map((stage) => (
          <div key={stage.id}><dt>{stage.id}</dt><dd>{stage.role}</dd></div>
        ))}
      </dl>
      <h2>Latest run</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Stage</th>
              <th>Outcome</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {pack.steps.map((step, index) => (
              <tr key={`${step.stage}-${index}`}>
                <td>{step.stage}</td>
                <td>{step.outcome}</td>
                <td className="wrap">{step.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h2>Production</h2>
      <dl className="kv">
        {pack.production.map((item) => (
          <div key={item.title}><dt>{item.title}</dt><dd>{item.body}</dd></div>
        ))}
      </dl>
      <h2>Schema</h2>
      <p className="lead">One row per child SKU. The same columns are in the database view and the three export files.</p>
      <dl className="kv">
        {pack.schema.map((column) => (
          <div key={column.name}><dt className="mono">{column.name}</dt><dd>{column.description}</dd></div>
        ))}
      </dl>
    </div>
  );
}

function GateView({ catalog }) {
  return (
    <div className="page">
      <p className="lead">{catalog.gate.terms}</p>
      <dl className="kv">
        <div><dt>Recommendation</dt><dd>{catalog.gate.recommendation}</dd></div>
        <div><dt>crawl allowed</dt><dd className="blank">Waiting for a person to write yes or no.</dd></div>
        <div><dt>Sitemap</dt><dd>{catalog.gate.sitemap_products.toLocaleString()} product URLs, {catalog.gate.sitemap_categories} category URLs.</dd></div>
      </dl>
      <h2>robots.txt</h2>
      <pre className="code">{catalog.gate.robots}</pre>
    </div>
  );
}

function ChatList({ chats, activeId, now, onOpen, onNew, onDelete }) {
  return (
    <div className="tree chat-list">
      <div className="chat-head">
        <span className="tree-label">Ask</span>
        <button type="button" className="chat-new" aria-label="New chat" onClick={onNew}>+</button>
      </div>
      {chats.map((chat) => (
        <div key={chat.id} className={chat.id === activeId ? "chat-row is-current" : "chat-row"}>
          <button
            type="button"
            className="chat-open"
            aria-current={chat.id === activeId ? "true" : undefined}
            onClick={() => onOpen(chat.id)}
          >
            <span className="chat-title">{chat.title}</span>
            <span className="chat-age">{ageLabel(chat.updatedAt, now)}</span>
          </button>
          <IconButton className="chat-delete" label="Delete chat" tip="left" onClick={() => onDelete(chat.id)}>
            <Icon name="trash" />
          </IconButton>
        </div>
      ))}
    </div>
  );
}

function AskView({ thread, catalog, onAsk }) {
  const [question, setQuestion] = useState("");
  const scroller = useRef(null);
  useEffect(() => {
    const node = scroller.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [thread.length]);
  return (
    <div className="ask">
      <div className="thread" ref={scroller}>
        <p className="lead">
          {catalog.level} can run the queries in the right bar. {catalog.claude ? "Claude may choose among those." : "Add CLAUDE_API_KEY to .env to let Claude choose one."}
        </p>
        {thread.map((entry, index) => (
          <article key={`${entry.who}-${index}`} className="exchange">
            <p className="who">You</p>
            <p>{entry.who}</p>
            <p className="who">Desk</p>
            <p className="desk">{entry.answer}</p>
            <ResultTable columns={entry.columns} rows={entry.rows} />
          </article>
        ))}
      </div>
      <form className="composer" onSubmit={(event) => { event.preventDefault(); const value = question.trim(); if (!value) return; setQuestion(""); onAsk(value); }}>
        <label className="sr" htmlFor="question">Question</label>
        <textarea
          id="question"
          rows={2}
          value={question}
          placeholder="Ask about the snapshot"
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== "Enter" || event.nativeEvent.isComposing) return;
            event.preventDefault();
            if (event.shiftKey) {
              const field = event.currentTarget;
              const start = field.selectionStart ?? field.value.length;
              const end = field.selectionEnd ?? start;
              const next = `${field.value.slice(0, start)}\n${field.value.slice(end)}`;
              setQuestion(next);
              const cursor = start + 1;
              requestAnimationFrame(() => {
                field.selectionStart = cursor;
                field.selectionEnd = cursor;
              });
              return;
            }
            event.currentTarget.form?.requestSubmit();
          }}
        />
        <button type="submit">Ask</button>
      </form>
    </div>
  );
}

function jobProgress(job) {
  if (job.status !== "running" || !job.message) return "";
  const line = String(job.message).split(". ")[0];
  return line.length > 96 ? `${line.slice(0, 93)}…` : line;
}

function JobsView({ jobs, jobId, go }) {
  if (!jobs.length) return <p className="page-note">No jobs yet. Refresh the snapshot to record one.</p>;
  const missing = jobId !== null && !jobs.some((job) => job.id === jobId);
  const running = jobs.some((job) => job.status === "running");
  return (
    <div className="table-wrap">
      {running ? <p className="page-note">This list updates while the crawl is running.</p> : null}
      {missing ? <p className="page-note">That run is not in the snapshot.</p> : null}
      <table>
        <thead>
          <tr>
            <th>Finished</th>
            <th>Status</th>
            <th>Products</th>
            <th>Variants</th>
            <th>Cards</th>
          </tr>
        </thead>
        {jobs.map((job) => {
          const open = job.id === jobId;
          const href = open ? "/jobs" : `/jobs/${job.id}`;
          return (
            <tbody key={job.id}>
              <tr
                className={open ? "job-row is-open" : "job-row"}
                role="button"
                tabIndex={0}
                aria-expanded={open}
                onClick={() => go(href)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    go(href);
                  }
                }}
              >
                <td>{(job.finished_at || job.started_at || "").replace("T", " ").replace("+00:00", " UTC")}</td>
                <td>
                  {job.status}
                  {jobProgress(job) ? <div className="job-progress">{jobProgress(job)}</div> : null}
                </td>
                <td>{job.products}</td>
                <td>{job.variants}</td>
                <td>{job.cards}</td>
              </tr>
              {open ? (
                <tr className="job-detail">
                  <td className="wrap" colSpan={5}>
                    <JobMore job={job} />
                  </td>
                </tr>
              ) : null}
            </tbody>
          );
        })}
      </table>
    </div>
  );
}

function JobMore({ job }) {
  const urls = job.message.match(/https?:\/\/[^\s]+/g) || [];
  const text = job.message.replace(/https?:\/\/[^\s]+/g, " ").replace(/\s+/g, " ").trim();
  return (
    <div>
      <p>{text || "No details were recorded."}</p>
      {urls.length ? (
        <ul className="plain">
          {urls.map((url) => (
            <li key={url}><a href={url} target="_blank" rel="noreferrer">{url}</a></li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function LinksView({ catalog, links, onAdd, onUpdate, onRemove }) {
  const parsed = parseRobots(catalog?.gate?.robots || "");
  const policy = policyLinks(catalog?.gate?.robots || "");
  const products = catalog?.gate?.sitemap_products;
  const categories = catalog?.gate?.sitemap_categories;
  const parsedLine = parsed.disallows.length
    ? `${parsed.disallows.length} disallow rules. Sitemap lists ${Number(products || 0).toLocaleString()} product URLs and ${categories || 0} category URLs.`
    : "";
  return (
    <div className="data-view links-view">
      <LinkCreate onAdd={onAdd} />
      <section className="link-section">
        <h2>Crawl during jobs</h2>
        <p className="lead">Refresh fetches these seed URLs, then each allowed /product/ URL on page 1. Listing ?page= and ?p= URLs stay unfetched.</p>
        <LinkTable>
          {CRAWL_LINKS.map((item) => (
            <FixedLinkRow key={item.href} item={item} />
          ))}
          {links.map((item) => (
            <LinkRow key={item.id} item={item} onUpdate={onUpdate} onRemove={onRemove} />
          ))}
        </LinkTable>
      </section>
      <section className="link-section">
        <h2>Robots and sitemaps</h2>
        {parsedLine ? <p className="lead">{parsedLine}</p> : null}
        <LinkTable>
          {policy.map((item) => (
            <FixedLinkRow key={item.href} item={item} />
          ))}
        </LinkTable>
      </section>
    </div>
  );
}

function LinkTable({ children }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>URL</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

function FixedLinkRow({ item }) {
  return (
    <tr>
      <td className="mono">{item.href}</td>
      <td>
        <a href={item.href} target="_blank" rel="noreferrer">View</a>
      </td>
    </tr>
  );
}

function LinkRow({ item, onUpdate, onRemove }) {
  const [href, setHref] = useState(item.href);
  useEffect(() => {
    setHref(item.href);
  }, [item.href]);
  function commit() {
    if (!onUpdate(item.id, href)) setHref(item.href);
  }
  return (
    <tr>
      <td>
        <input aria-label="URL" value={href} onChange={(event) => setHref(event.target.value)} onBlur={commit} />
      </td>
      <td className="link-actions">
        <a href={item.href} target="_blank" rel="noreferrer">View</a>
        <button type="button" onClick={() => onRemove(item.id)}>Remove</button>
      </td>
    </tr>
  );
}

function LinkCreate({ onAdd }) {
  const [href, setHref] = useState("");
  const [note, setNote] = useState("");
  return (
    <div className="links-add">
      <form onSubmit={(event) => {
        event.preventDefault();
        const message = onAdd(href);
        setNote(message);
        if (!message) setHref("");
      }}>
        <label>
          <span className="sr">URL</span>
          <input value={href} placeholder="https://" required onChange={(event) => setHref(event.target.value)} />
        </label>
        <button type="submit">Add link</button>
      </form>
      <p className="link-note">robots.txt and the sitemap are fetched and parsed automatically.</p>
      {note ? <p className="link-note">{note}</p> : null}
    </div>
  );
}

function SettingsView({ pack, onSaved }) {
  const [form, setForm] = useState(pack?.settings || null);
  const [message, setMessage] = useState("");
  useEffect(() => { setForm(pack?.settings || null); }, [pack]);
  if (!form || !pack) return <p className="page-note">Loading settings…</p>;
  function update(key, value) {
    setForm({ ...form, [key]: value });
  }
  async function switchLevel(level) {
    const next = { ...form, user_level: level };
    setForm(next);
    try {
      await saveSettings(next);
      setMessage(`Level is ${level}.`);
      await onSaved();
    } catch (exc) {
      setMessage(exc.message);
    }
  }
  async function onSubmit(event) {
    event.preventDefault();
    try {
      await saveSettings(form);
      setMessage("Settings saved.");
      await onSaved();
    } catch (exc) {
      setMessage(exc.message);
    }
  }
  return (
    <form className="page settings" onSubmit={onSubmit}>
      <p className="lead">The server reads the user level before it runs a listed query. Viewer, operator, and admin each unlock a different set.</p>
      {message ? <p className="meta">{message}</p> : null}
      <div className="field">
        <span>User level</span>
        <div className="level-switch" role="radiogroup" aria-label="User level">
          {pack.levels.map((level) => (
            <button
              key={level}
              type="button"
              aria-pressed={form.user_level === level}
              onClick={() => switchLevel(level)}
            >
              {level}
            </button>
          ))}
        </div>
      </div>
      {FIELDS.map(([key, label, hint]) => (
        <label className="field" key={key}>
          <span>{label}</span>
          <small>{hint} {pack.bounds[key].min}–{pack.bounds[key].max}.</small>
          <input type="number" min={pack.bounds[key].min} max={pack.bounds[key].max} value={form[key]} onChange={(event) => update(key, event.target.value)} required />
        </label>
      ))}
      <button type="submit">Save settings</button>
    </form>
  );
}

function QueryList({ queries, onRun }) {
  const [sku, setSku] = useState("PFPJK");
  if (!queries) return <p className="page-note">Loading queries…</p>;
  const allowed = queries.queries.filter((item) => item.allowed);
  const held = queries.queries.filter((item) => !item.allowed);
  return (
    <div className="tree query-tree">
      <div className="tree-label">Allowed for {queries.level}</div>
      {allowed.map((item) => (
        <div key={item.id} className="query-row">
          <button type="button" title={item.phrase} onClick={() => onRun(item, sku)}>{item.title}</button>
          {item.needs_sku ? (
            <input aria-label="SKU" value={sku} onChange={(event) => setSku(event.target.value)} />
          ) : null}
        </div>
      ))}
      <div className="tree-label">Not allowed</div>
      {held.map((item) => (
        <button type="button" key={item.id} className="held" title={item.sql} onClick={() => onRun(item, sku)}>
          {item.title}
          <span>{item.level}</span>
        </button>
      ))}
    </div>
  );
}

function ResultTable({ columns, rows }) {
  if (!columns?.length || !rows?.length) return null;
  if (columns.length === 1 && columns[0] === "matches") return null;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell ?? ""}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Output({ output }) {
  if (!output) return <p className="page-note">Run a listed query. The panel stays open until you toggle it.</p>;
  return (
    <div className="output">
      <p>{output.answer}</p>
      <ResultTable columns={output.columns} rows={output.rows} />
    </div>
  );
}
