// Canal WebSocket de sinalização e controle, com reconexão automática.
// Também mede o tempo de ida e volta (ping/pong) e estima a diferença de relógio
// entre este computador e o robô, usada na medição de latência de vídeo fim a fim.

const PING_INTERVAL_MS = 2000;
const MAX_BACKOFF_MS = 8000;

export const wallNow = () => performance.timeOrigin + performance.now();

export class Signaling extends EventTarget {
  constructor(url) {
    super();
    this.url = url;
    this.ws = null;
    this.state = "idle"; // idle | connecting | open | reconnecting | closed
    this.attempt = 0;
    this.pingId = 0;
    this.pending = new Map();
    this.samples = []; // { rtt, offset }
    this.lastRtt = null;
    this._timer = null;
    this._pingTimer = null;
    this._stopped = false;
  }

  connect() {
    this._stopped = false;
    this._open();
  }

  close() {
    this._stopped = true;
    clearTimeout(this._timer);
    clearInterval(this._pingTimer);
    if (this.ws) this.ws.close(1000);
    this._setState("closed");
  }

  send(msg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
      return true;
    }
    return false;
  }

  /** Diferença (robô − local) em ms e incerteza (meio RTT da melhor amostra). */
  clock() {
    if (!this.samples.length) return null;
    const best = this.samples.reduce((a, b) => (b.rtt < a.rtt ? b : a));
    return { offset: best.offset, uncertainty: best.rtt / 2 };
  }

  _setState(state, detail = {}) {
    this.state = state;
    this.dispatchEvent(new CustomEvent("state", { detail: { state, ...detail } }));
  }

  _open() {
    clearTimeout(this._timer);
    this._setState(this.attempt === 0 ? "connecting" : "reconnecting", { attempt: this.attempt });
    let ws;
    try {
      ws = new WebSocket(this.url);
    } catch {
      this._scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.samples = [];
      this._setState("open");
      clearInterval(this._pingTimer);
      this._ping();
      this._pingTimer = setInterval(() => this._ping(), PING_INTERVAL_MS);
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "pong") return this._onPong(msg);
      this.dispatchEvent(new CustomEvent("message", { detail: msg }));
    };
    ws.onclose = (ev) => {
      clearInterval(this._pingTimer);
      this.pending.clear();
      if (this.ws !== ws) return;
      this.ws = null;
      if (ev.code === 4401) {
        this._stopped = true;
        this._setState("closed", { reason: "unauthorized" });
        return;
      }
      if (ev.code === 4403) {
        this._stopped = true;
        this._setState("closed", { reason: "forbidden" });
        return;
      }
      if (!this._stopped) this._scheduleReconnect();
    };
  }

  _scheduleReconnect() {
    this.attempt += 1;
    const delay = Math.min(MAX_BACKOFF_MS, 500 * 2 ** (this.attempt - 1)) * (0.85 + Math.random() * 0.3);
    this._setState("reconnecting", { attempt: this.attempt, delay });
    this._timer = setTimeout(() => this._open(), delay);
  }

  _ping() {
    const id = ++this.pingId;
    const t = wallNow();
    this.pending.set(id, t);
    if (this.pending.size > 10) this.pending.delete(this.pending.keys().next().value);
    this.send({ type: "ping", id, t });
  }

  _onPong(msg) {
    const t0 = this.pending.get(msg.id);
    if (t0 === undefined) return;
    this.pending.delete(msg.id);
    const t1 = wallNow();
    const rtt = t1 - t0;
    this.lastRtt = rtt;
    this.samples.push({ rtt, offset: msg.server_time - (t0 + t1) / 2 });
    if (this.samples.length > 15) this.samples.shift();
  }
}
