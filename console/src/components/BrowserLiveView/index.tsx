import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent,
  type WheelEvent,
} from "react";
import {
  ArrowLeftOutlined,
  ArrowRightOutlined,
  CloseOutlined,
  LockOutlined,
  PlusOutlined,
  ReloadOutlined,
  UnlockOutlined,
} from "@ant-design/icons";
import { getApiToken, getApiUrl } from "../../api/config";
import styles from "./index.module.less";

interface TabInfo {
  page_id: string;
  url: string;
  title: string;
  active: boolean;
}

interface BrowserLiveViewProps {
  agentId?: string;
}

function parseUrlInfo(urlStr: string) {
  try {
    const u = new URL(urlStr);
    return { isSecure: u.protocol === "https:", domain: u.host };
  } catch {
    return { isSecure: false, domain: "" };
  }
}

function extractDomain(urlStr: string): string {
  try {
    return new URL(urlStr).hostname;
  } catch {
    return urlStr;
  }
}

export default function BrowserLiveView({
  agentId = "default",
}: BrowserLiveViewProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const imeRef = useRef<HTMLTextAreaElement>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const [url, setUrl] = useState("");
  const [inputUrl, setInputUrl] = useState("");
  const [connected, setConnected] = useState(false);
  const [viewport, setViewport] = useState({ width: 1280, height: 720 });
  const [tabs, setTabs] = useState<TabInfo[]>([]);

  const expectingFrameRef = useRef(false);
  const urlFocusedRef = useRef(false);
  const composingRef = useRef(false);

  const urlInfo = parseUrlInfo(url);

  const sendMessage = useCallback((data: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(data));
    }
  }, []);

  // --- WebSocket connection ---
  useEffect(() => {
    const apiUrl = getApiUrl("/browser/ws");
    const wsUrl = apiUrl
      .replace(/^http/, "ws")
      .concat(
        `?token=${encodeURIComponent(
          getApiToken(),
        )}&agent_id=${encodeURIComponent(agentId)}`,
      );

    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let closed = false;

    function handleTextMessage(msg: Record<string, unknown>) {
      const type = msg.type as string;

      if (type === "session") {
        const vp = msg.viewport as
          | { width: number; height: number }
          | undefined;
        if (vp && vp.width && vp.height) {
          setViewport(vp);
        }
        if (typeof msg.url === "string" && msg.url) {
          setUrl(msg.url);
          if (!urlFocusedRef.current) {
            setInputUrl(msg.url);
          }
        }
        if (msg.status === "stopped") {
          setTabs([]);
        }
      } else if (type === "navigation") {
        if (typeof msg.url === "string") {
          setUrl(msg.url);
          if (!urlFocusedRef.current) {
            setInputUrl(msg.url);
          }
        }
      } else if (type === "frame") {
        const w = msg.w as number | undefined;
        const h = msg.h as number | undefined;
        if (w && h) {
          setViewport({ width: w, height: h });
        }
        expectingFrameRef.current = true;
      } else if (type === "tabs") {
        const tabList = msg.tabs as TabInfo[] | undefined;
        if (Array.isArray(tabList)) {
          setTabs(tabList);
          // Update URL from active tab
          const active = tabList.find((t) => t.active);
          if (active && active.url) {
            setUrl(active.url);
            if (!urlFocusedRef.current) {
              setInputUrl(active.url);
            }
          }
        }
      }
    }

    function connect() {
      if (closed) return;

      const ws = new WebSocket(wsUrl);
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
      };

      ws.onclose = () => {
        setConnected(false);
        wsRef.current = null;
        if (!closed) {
          reconnectTimer = setTimeout(connect, 2000);
        }
      };

      ws.onerror = () => {};

      ws.onmessage = (event) => {
        if (typeof event.data === "string") {
          try {
            const msg = JSON.parse(event.data);
            handleTextMessage(msg);
          } catch {
            // ignore
          }
        } else if (event.data instanceof ArrayBuffer) {
          if (expectingFrameRef.current) {
            expectingFrameRef.current = false;
            renderFrame(event.data);
          }
        }
      };
    }

    connect();

    return () => {
      closed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [agentId]);

  function renderFrame(data: ArrayBuffer) {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const blob = new Blob([data], { type: "image/jpeg" });
    const imgUrl = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
      canvas.width = img.width;
      canvas.height = img.height;
      ctx.drawImage(img, 0, 0);
      URL.revokeObjectURL(imgUrl);
    };
    img.onerror = () => {
      URL.revokeObjectURL(imgUrl);
    };
    img.src = imgUrl;
  }

  // --- Mouse handlers ---
  function getNormalisedCoords(e: MouseEvent<HTMLCanvasElement>) {
    const rect = (e.target as HTMLCanvasElement).getBoundingClientRect();
    return {
      x: (e.clientX - rect.left) / rect.width,
      y: (e.clientY - rect.top) / rect.height,
    };
  }

  function handleClick(e: MouseEvent<HTMLCanvasElement>) {
    const { x, y } = getNormalisedCoords(e);
    sendMessage({
      type: "mouse",
      action: "click",
      x,
      y,
      button: e.button === 2 ? "right" : "left",
    });
    imeRef.current?.focus();
  }

  function handleDblClick(e: MouseEvent<HTMLCanvasElement>) {
    const { x, y } = getNormalisedCoords(e);
    sendMessage({
      type: "mouse",
      action: "dblclick",
      x,
      y,
      button: "left",
    });
    imeRef.current?.focus();
  }

  function handleWheel(e: WheelEvent<HTMLCanvasElement>) {
    const { x, y } = getNormalisedCoords(e);
    sendMessage({
      type: "mouse",
      action: "wheel",
      x,
      y,
      deltaY: e.deltaY,
    });
  }

  // --- Keyboard / IME handlers (on hidden textarea) ---
  function handleImeKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (composingRef.current) return; // don't interfere with IME
    const key = e.key;
    // Regular characters are handled by onInput; let them through
    if (key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      return;
    }
    e.preventDefault();
    let pwKey = key;
    if (e.ctrlKey && key !== "Control") pwKey = `Control+${key}`;
    if (e.metaKey && key !== "Meta") pwKey = `Meta+${key}`;
    if (e.altKey && key !== "Alt") pwKey = `Alt+${key}`;
    sendMessage({ type: "keyboard", action: "press", key: pwKey });
  }

  function handleCompositionStart() {
    composingRef.current = true;
  }

  function handleCompositionEnd(
    e: React.CompositionEvent<HTMLTextAreaElement>,
  ) {
    composingRef.current = false;
    if (e.data) {
      sendMessage({ type: "keyboard", action: "type", text: e.data });
    }
    // Clear after a tick so the trailing input event is a no-op
    requestAnimationFrame(() => {
      if (imeRef.current) imeRef.current.value = "";
    });
  }

  function handleImeInput() {
    if (composingRef.current) return;
    const el = imeRef.current;
    if (!el) return;
    const text = el.value;
    if (text) {
      sendMessage({ type: "keyboard", action: "type", text });
      el.value = "";
    }
  }

  // --- URL bar ---
  function handleUrlSubmit() {
    let navigateUrl = inputUrl.trim();
    if (!navigateUrl) return;
    if (
      !navigateUrl.startsWith("http://") &&
      !navigateUrl.startsWith("https://")
    ) {
      navigateUrl = "https://" + navigateUrl;
    }
    sendMessage({ type: "navigate", url: navigateUrl });
  }

  // --- Tab switch ---
  function handleTabClick(pageId: string) {
    sendMessage({ type: "switch_tab", page_id: pageId });
  }

  // --- New tab ---
  function handleNewTab() {
    sendMessage({ type: "new_tab" });
  }

  // --- Close tab ---
  function handleCloseTab(e: MouseEvent<HTMLSpanElement>, pageId: string) {
    e.stopPropagation();
    sendMessage({ type: "close_tab", page_id: pageId });
  }

  return (
    <div className={styles.container}>
      {/* Tab bar */}
      <div className={styles.tabBar}>
        <div className={styles.trafficLights}>
          <span className={`${styles.dot} ${styles.dotRed}`} />
          <span className={`${styles.dot} ${styles.dotYellow}`} />
          <span className={`${styles.dot} ${styles.dotGreen}`} />
        </div>
        <div className={styles.tabList}>
          {tabs.map((tab) => (
            <div
              key={tab.page_id}
              className={`${styles.tab} ${tab.active ? styles.tabActive : ""}`}
              onClick={() => handleTabClick(tab.page_id)}
              title={tab.url}
            >
              <span className={styles.tabTitle}>
                {tab.title || extractDomain(tab.url) || tab.page_id}
              </span>
              <span
                className={styles.tabClose}
                onClick={(e) => handleCloseTab(e, tab.page_id)}
                title="Close tab"
              >
                <CloseOutlined />
              </span>
            </div>
          ))}
          {tabs.length === 0 && (
            <div className={`${styles.tab} ${styles.tabActive}`}>
              <span className={styles.tabTitle}>
                {extractDomain(url) || "New Tab"}
              </span>
            </div>
          )}
        </div>
        <button
          className={styles.newTabBtn}
          onClick={handleNewTab}
          title="New tab"
        >
          <PlusOutlined />
        </button>
      </div>

      {/* Address bar */}
      <div className={styles.toolbar}>
        <button
          className={styles.navBtn}
          onClick={() => sendMessage({ type: "navigate_back" })}
          title="Back"
        >
          <ArrowLeftOutlined />
        </button>
        <button
          className={styles.navBtn}
          onClick={() => sendMessage({ type: "navigate_forward" })}
          title="Forward"
        >
          <ArrowRightOutlined />
        </button>
        <button
          className={styles.navBtn}
          onClick={() => sendMessage({ type: "reload" })}
          title="Reload"
        >
          <ReloadOutlined />
        </button>

        <div className={styles.urlBar}>
          {url && (
            <span
              className={`${styles.lockIcon} ${
                urlInfo.isSecure ? styles.lockIconSecure : ""
              }`}
            >
              {urlInfo.isSecure ? <LockOutlined /> : <UnlockOutlined />}
            </span>
          )}
          <input
            className={styles.urlInput}
            value={inputUrl}
            onChange={(e) => setInputUrl(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleUrlSubmit();
            }}
            onFocus={() => {
              urlFocusedRef.current = true;
            }}
            onBlur={() => {
              urlFocusedRef.current = false;
            }}
            placeholder="Search or enter URL..."
          />
        </div>
      </div>

      {/* Canvas */}
      <div className={styles.canvasWrapper}>
        <canvas
          ref={canvasRef}
          className={styles.canvas}
          width={viewport.width}
          height={viewport.height}
          onClick={handleClick}
          onDoubleClick={handleDblClick}
          onWheel={handleWheel}
          onContextMenu={(e) => e.preventDefault()}
        />
        {/* Hidden textarea for keyboard + IME input */}
        <textarea
          ref={imeRef}
          className={styles.imeInput}
          onKeyDown={handleImeKeyDown}
          onInput={handleImeInput}
          onCompositionStart={handleCompositionStart}
          onCompositionEnd={handleCompositionEnd}
          autoComplete="off"
          autoCorrect="off"
          spellCheck={false}
        />
        {!connected && (
          <div className={styles.overlay}>
            <span className={styles.overlayText}>Connecting...</span>
          </div>
        )}
      </div>
    </div>
  );
}
