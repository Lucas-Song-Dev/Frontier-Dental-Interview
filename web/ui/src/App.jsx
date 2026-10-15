import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ask,
  getCatalog,
  getJobs,
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
const REQUIRED_LINKS = [
  { href: "https://www.safcodental.com/catalog/sutures-surgical-products", label: "Sutures seed" },
  { href: "https://www.safcodental.com/catalog/gloves", label: "Gloves seed" },
  { href: "https://www.safcodental.com/robots.txt", label: "robots.txt" },
  { href: "https://www.safcodental.com/sitemap.xml", label: "Sitemap" },
];
const FIELDS = [
  ["crawl_interval_minutes", "Time between crawls", "Minutes from the end of one run until the next may start."],
  ["min_delay_seconds", "Minimum delay", "Seconds between requests. Never below a robots Crawl-delay."],
  ["max_concurrency", "Max concurrency", "How many page fetches may run at once."],
  ["max_products", "Max products per run", "Stop a run after this many parent products."],
  ["max_pages_per_category", "Max pages per category", "Live ?page= links stay disallowed."],
];

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

function readExtraLinks() {
  try {
    const saved = JSON.parse(localStorage.getItem(LINKS_KEY) || "[]");
    if (!Array.isArray(saved)) return [];
    const required = new Set(REQUIRED_LINKS.map((item) => item.href));
    return saved.filter((item) => item && typeof item.href === "string" && item.href.startsWith("https://") && !required.has(item.href));
  } catch {
    return [];
  }
}

function linkLabel(href) {
  try {
    const url = new URL(href);
    const last = url.pathname.split("/").filter(Boolean).pop();
    return last || url.hostname;
  } catch {
    return href;
  }
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
  if (path === "/settings") return "settings";
  return "catalog";
}

function titleFor(path, catalog, product) {
  if (path === "/chat") return "Query agent";
  const job = path.match(/^\/jobs\/(\d+)$/);
  if (path === "/jobs") return "Jobs";
  if (job) return `Job ${job[1]}`;
  if (path === "/settings") return "Settings";
  if (path === "/gate") return "Gate";
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
  const [thread, setThread] = useState([]);
  const [output, setOutput] = useState(null);
  const [status, setStatus] = useState("");
  const [auto, setAuto] = useState(() => localStorage.getItem("desk-auto") === "on");
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
    setStatus("Fetching allowed Safco URLs…");
    try {
      await refreshSnapshot();
      await loadDesk();
      setStatus("Fetched safcodental.com");
    } catch (exc) {
      setStatus(exc.message);
    }
  }

  useEffect(() => {
    if (!auto) return undefined;
    const minutes = Number(settings?.settings?.crawl_interval_minutes || 360);
    const timer = setInterval(() => {
      refresh();
    }, Math.max(minutes, 15) * 60 * 1000);
    return () => clearInterval(timer);
  }, [auto, loadDesk, settings]);

  function toggleAuto() {
    const next = !auto;
    setAuto(next);
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
    const entry = { who: query.title, ...reply };
    setThread((current) => [...current, entry]);
    setOutput({ title: query.title, ...reply });
    openPanel("bottom");
    if (path !== "/chat") go("/chat");
  }

  async function submitQuestion(question) {
    const reply = await ask({ question });
    setThread((current) => [...current, { who: question, ...reply }]);
    setOutput({ title: question, ...reply });
    openPanel("bottom");
  }

  const activity = activityFor(path);
  const tab = titleFor(path, catalog, product);

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
        { key: "data", label: "Data", items: [{ href: "/data", label: "All rows" }] },
        { key: "gate", label: "Gate", items: [{ href: "/gate", label: "Robots" }] },
        {
          key: "links",
          label: "Links",
          items: [
            ...REQUIRED_LINKS.map((item) => ({ ...item, external: true })),
            ...extraLinks.map((item) => ({ ...item, external: true, removable: true })),
          ],
        },
      ];
    }
    if (activity === "ask") {
      return [{ key: "agent", label: "", items: [{ href: "/chat", label: "Query agent" }] }];
    }
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
    return [{ key: "desk", label: "", items: [{ href: "/settings", label: "Desk settings" }] }];
  }, [activity, catalog, extraLinks, jobs]);

  function addLink(raw) {
    let href = raw.trim();
    if (!href) return false;
    if (!/^https?:\/\//i.test(href)) href = `https://${href}`;
    let url;
    try {
      url = new URL(href);
    } catch {
      return false;
    }
    if (url.protocol !== "https:") return false;
    href = url.toString();
    const known = new Set([...REQUIRED_LINKS, ...extraLinks].map((item) => item.href));
    if (known.has(href)) return false;
    const next = [...extraLinks, { href, label: linkLabel(href) }];
    setExtraLinks(next);
    localStorage.setItem(LINKS_KEY, JSON.stringify(next));
    return true;
  }

  function removeLink(href) {
    const next = extraLinks.filter((item) => item.href !== href);
    setExtraLinks(next);
    localStorage.setItem(LINKS_KEY, JSON.stringify(next));
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
          <IconButton label="Query agent" current={activity === "ask"} onClick={() => onActivity("ask", "/chat")}>
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
            <div className="tree">
              {tree.map((group) => (
                <div className="tree-section" key={group.key}>
                  {group.label ? <div className="tree-label">{group.label}</div> : null}
                  {group.items.map((item) => (
                    item.external ? (
                      <span className="link-row" key={item.href}>
                        <a href={item.href} title={item.href} target="_blank" rel="noreferrer">{item.label}</a>
                        {item.removable ? (
                          <button type="button" aria-label={`Remove ${item.label}`} onClick={() => removeLink(item.href)}>×</button>
                        ) : null}
                      </span>
                    ) : (
                      <a key={item.href} href={item.href} className={path === item.href ? "is-current" : ""} onClick={(event) => { event.preventDefault(); go(item.href); }}>
                        {item.label}
                      </a>
                    )
                  ))}
                  {group.key === "links" ? <LinkAdd onAdd={addLink} /> : null}
                </div>
              ))}
            </div>
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
                go={go}
                onAsk={submitQuestion}
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

function LinkAdd({ onAdd }) {
  const [value, setValue] = useState("");
  return (
    <form className="link-add" onSubmit={(event) => {
      event.preventDefault();
      if (onAdd(value)) setValue("");
    }}>
      <label className="sr" htmlFor="new-link">Add a link</label>
      <input id="new-link" value={value} placeholder="Add a link" onChange={(event) => setValue(event.target.value)} />
    </form>
  );
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

function Editor({ path, catalog, product, jobs, settings, thread, go, onAsk, onSaved }) {
  if (path === "/chat") return <AskView thread={thread} catalog={catalog} onAsk={onAsk} />;
  const jobMatch = path.match(/^\/jobs\/(\d+)$/);
  if (path === "/jobs" || jobMatch) {
    return <JobsView jobs={jobs} jobId={jobMatch ? Number(jobMatch[1]) : null} go={go} />;
  }
  if (path.startsWith("/jobs/")) return <p className="page-note">That run is not in the snapshot.</p>;
  if (path === "/settings") return <SettingsView pack={settings} onSaved={onSaved} />;
  if (path === "/gate") return <GateView catalog={catalog} />;
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
      <p>Local desk for the Safco snapshot. It stores the two seed categories, the product sheets fetched from allowed URLs, and the robots gate. Listing <span className="mono">?page=</span> is not requested.</p>
      <p>This snapshot has {products} products and {variants} child SKUs. Open Data for the full table.</p>
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
  ["item_group", "Group"],
  ["mfr_number", "Mfr #"],
  ["stock", "Stock"],
  ["variant_price", "Price"],
  ["product_name", "Product"],
  ["parent_sku", "Parent SKU"],
  ["category_title", "Category"],
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
              <th>Sheet</th>
            </tr>
          </thead>
          <tbody>
            {category.cards.map((card) => (
              <tr key={card.slug}>
                <td>{card.saved ? <a href={`/product/${card.slug}`} onClick={(event) => { event.preventDefault(); go(`/product/${card.slug}`); }}>{card.name}</a> : card.name}</td>
                <td>{card.manufacturer}</td>
                <td>{card.price}</td>
                <td>{card.saved ? "Saved" : ""}</td>
              </tr>
            ))}
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
  return (
    <div className="page">
      <p className="crumbs">
        <a href="/" onClick={(event) => { event.preventDefault(); go("/"); }}>Overview</a>
        <span> / </span>
        <a href={`/category/${product.category}`} onClick={(event) => { event.preventDefault(); go(`/category/${product.category}`); }}>{product.category_title}</a>
        <span> / {product.name}</span>
      </p>
      <dl className="kv inline">
        <div><dt>Parent SKU</dt><dd>{product.sku}</dd></div>
        <div><dt>Price</dt><dd>{product.price}</dd></div>
        <div><dt>Availability</dt><dd>{product.availability}</dd></div>
        <div><dt>Child SKUs</dt><dd>{product.variants.length}</dd></div>
      </dl>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>SKU</th>
              <th>Name</th>
              {grouped ? <th>Group</th> : null}
              <th>Mfr #</th>
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
                <td>{row.stock}</td>
                <td>{row.price}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
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

function AskView({ thread, catalog, onAsk }) {
  const [question, setQuestion] = useState("");
  return (
    <div className="ask">
      <div className="thread">
        <p className="lead">
          {catalog.level} can run the queries in the right bar. {catalog.claude ? "Claude may choose among those." : "Add CLAUDE_API_KEY to .env to let Claude choose one."}
        </p>
        {thread.map((entry, index) => (
          <article key={`${entry.who}-${index}`} className="exchange">
            <p className="who">You</p>
            <p>{entry.who}</p>
            <p className="who">Desk</p>
            <p>{entry.answer}</p>
            <ResultTable columns={entry.columns} rows={entry.rows} />
          </article>
        ))}
      </div>
      <form className="composer" onSubmit={(event) => { event.preventDefault(); const value = question.trim(); if (!value) return; setQuestion(""); onAsk(value); }}>
        <label className="sr" htmlFor="question">Question</label>
        <textarea id="question" rows={2} value={question} placeholder="Ask about the snapshot" onChange={(event) => setQuestion(event.target.value)} />
        <button type="submit">Ask</button>
      </form>
    </div>
  );
}

function JobsView({ jobs, jobId, go }) {
  if (!jobs.length) return <p className="page-note">No jobs yet. Refresh the snapshot to record one.</p>;
  const missing = jobId !== null && !jobs.some((job) => job.id === jobId);
  return (
    <div className="table-wrap">
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
                <td>{job.status}</td>
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
