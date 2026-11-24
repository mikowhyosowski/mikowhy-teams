/**
 * Klient WebSocket gateway (ws://<api-host>:<ws-port>/).
 * Singleton z auto-reconnect (backoff 1s→5s) i prostym dispatchingiem po `type`.
 * Fallback: gdy WS niedostępny, ekrany zostają przy REST (polling).
 */

const WS_PORT = process.env.EXPO_PUBLIC_WS_PORT ?? "8767";

function wsUrl(): string {
  const apiBase = process.env.EXPO_PUBLIC_API_URL ?? "";
  try {
    const u = new URL(apiBase || (typeof location !== "undefined" ? location.origin : "http://localhost:8081"));
    u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
    u.port = WS_PORT;
    u.pathname = "/";
    u.search = "";
    return u.toString();
  } catch {
    return `ws://localhost:${WS_PORT}/`;
  }
}

type Handler = (msg: any) => void;

class WsClient {
  private ws: WebSocket | null = null;
  private handlers = new Map<string, Set<Handler>>();
  private anyHandlers = new Set<Handler>();
  private reconnectDelay = 1000;
  private closedByUser = false;
  private connectTimer: ReturnType<typeof setTimeout> | null = null;

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  on(type: string, h: Handler): () => void {
    let set = this.handlers.get(type);
    if (!set) {
      set = new Set();
      this.handlers.set(type, set);
    }
    set.add(h);
    return () => set.delete(h);
  }

  onAny(h: Handler): () => void {
    this.anyHandlers.add(h);
    return () => this.anyHandlers.delete(h);
  }

  send(obj: unknown): boolean {
    if (!this.connected) return false;
    this.ws!.send(JSON.stringify(obj));
    return true;
  }

  connect(): void {
    if (this.closedByUser) return;
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) return;
    try {
      this.ws = new WebSocket(wsUrl());
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws.onopen = () => {
      this.reconnectDelay = 1000;
    };
    this.ws.onmessage = (ev) => {
      let msg: any;
      try {
        msg = JSON.parse(String(ev.data));
      } catch {
        return;
      }
      const set = msg?.type ? this.handlers.get(msg.type) : undefined;
      if (set) for (const h of set) h(msg);
      for (const h of this.anyHandlers) h(msg);
    };
    this.ws.onclose = () => this.scheduleReconnect();
    this.ws.onerror = () => {
      try {
        this.ws?.close();
      } catch {
        /* noop */
      }
    };
  }

  private scheduleReconnect(): void {
    if (this.closedByUser || this.connectTimer) return;
    this.connectTimer = setTimeout(() => {
      this.connectTimer = null;
      this.connect();
    }, this.reconnectDelay);
    this.reconnectDelay = Math.min(5000, this.reconnectDelay * 2);
  }

  close(): void {
    this.closedByUser = true;
    if (this.connectTimer) clearTimeout(this.connectTimer);
    try {
      this.ws?.close();
    } catch {
      /* noop */
    }
  }
}

export const ws = new WsClient();
ws.connect();
