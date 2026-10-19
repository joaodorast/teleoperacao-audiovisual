// Métricas reais, obtidas de RTCPeerConnection.getStats() e do relógio de quadros do vídeo.
// Nenhum valor é estimado ou simulado: se a informação não existir, o campo fica nulo.

const LOSS_WINDOW = 6; // amostras de 1 s

export class LinkStats {
  constructor() {
    this.prev = null;
    this.history = [];
    this.lastFrameAt = null;
    this.lastAudioAt = null;
  }

  update(report) {
    const now = performance.now();
    let pair = null, video = null, audio = null, transport = null;
    report.forEach((s) => {
      if (s.type === "transport") transport = s;
      else if (s.type === "inbound-rtp" && s.kind === "video") video = s;
      else if (s.type === "inbound-rtp" && s.kind === "audio") audio = s;
    });
    if (transport?.selectedCandidatePairId) pair = report.get(transport.selectedCandidatePairId);
    if (!pair) report.forEach((s) => { if (s.type === "candidate-pair" && s.nominated && s.state === "succeeded") pair = s; });

    const p = this.prev;
    const d = (cur, prev, key) => (cur && prev && cur[key] != null && prev[key] != null ? cur[key] - prev[key] : null);

    const out = {
      rttMs: pair?.currentRoundTripTime != null ? pair.currentRoundTripTime * 1000 : null,
      video: { fps: null, lossPct: null, bufferMs: null, decoded: video?.framesDecoded ?? 0 },
      audio: { lossPct: null, bufferMs: null, jitterMs: null, receiving: false },
    };

    if (video) {
      const dDecoded = d(video, p?.video, "framesDecoded");
      if (dDecoded > 0) this.lastFrameAt = now;
      if (video.framesPerSecond != null) out.video.fps = video.framesPerSecond;
      else if (dDecoded != null && p) out.video.fps = dDecoded / ((now - p.t) / 1000);
      const dDelay = d(video, p?.video, "jitterBufferDelay");
      const dEmit = d(video, p?.video, "jitterBufferEmittedCount");
      if (dEmit > 0) out.video.bufferMs = (dDelay / dEmit) * 1000;
    }
    if (audio) {
      const dRecv = d(audio, p?.audio, "packetsReceived");
      if (dRecv > 0) this.lastAudioAt = now;
      out.audio.receiving = this.lastAudioAt != null && now - this.lastAudioAt < 2000;
      const dDelay = d(audio, p?.audio, "jitterBufferDelay");
      const dEmit = d(audio, p?.audio, "jitterBufferEmittedCount");
      if (dEmit > 0) out.audio.bufferMs = (dDelay / dEmit) * 1000;
      if (audio.jitter != null) out.audio.jitterMs = audio.jitter * 1000;
    }

    this.history.push({
      vLost: video?.packetsLost ?? null, vRecv: video?.packetsReceived ?? null,
      aLost: audio?.packetsLost ?? null, aRecv: audio?.packetsReceived ?? null,
    });
    if (this.history.length > LOSS_WINDOW) this.history.shift();
    const first = this.history[0], last = this.history[this.history.length - 1];
    const loss = (lk, rk) => {
      if (this.history.length < 2 || first[rk] == null || last[rk] == null) return null;
      const lost = Math.max(0, last[lk] - first[lk]);
      const recv = last[rk] - first[rk];
      return lost + recv > 0 ? (lost / (lost + recv)) * 100 : null;
    };
    out.video.lossPct = loss("vLost", "vRecv");
    out.audio.lossPct = loss("aLost", "aRecv");
    out.msSinceFrame = this.lastFrameAt == null ? null : now - this.lastFrameAt;

    this.prev = { t: now, video, audio };
    return out;
  }
}

/**
 * Latência de vídeo fim a fim: do instante da captura na webcam do robô até a exibição
 * do quadro nesta tela.
 *
 * - O robô informa a origem do timestamp RTP e o relógio de captura ("video.clock").
 * - requestVideoFrameCallback fornece o timestamp RTP de cada quadro exibido e o horário
 *   previsto de exibição.
 * - A diferença de relógios entre as máquinas vem do ping/pong da sinalização; a
 *   incerteza da medida é ± metade do menor RTT observado.
 */
export class VideoLatency {
  constructor(video, getClockOffset) {
    this.video = video;
    this.getClockOffset = getClockOffset;
    this.videoClock = null;
    this.samples = [];
    this.framesSeen = 0;
    this.supported = "requestVideoFrameCallback" in HTMLVideoElement.prototype;
    this.rtpExposed = null;
    this._handle = null;
  }

  setVideoClock(clock) {
    const prev = this.videoClock;
    this.videoClock = clock;
    if (!prev || prev.rtp_origin !== clock.rtp_origin || prev.epoch_ms !== clock.epoch_ms) this.samples = [];
  }

  start() {
    if (!this.supported || this._handle != null) return;
    const tick = (_now, md) => {
      this.framesSeen += 1;
      if (md.rtpTimestamp === undefined) {
        if (this.framesSeen > 5) this.rtpExposed = false;
      } else {
        this.rtpExposed = true;
        this._sample(md);
      }
      this._handle = this.video.requestVideoFrameCallback(tick);
    };
    this._handle = this.video.requestVideoFrameCallback(tick);
  }

  stop() {
    if (this._handle != null && this.supported) this.video.cancelVideoFrameCallback(this._handle);
    this._handle = null;
    this.samples = [];
    this.videoClock = null;
  }

  _sample(md) {
    const vc = this.videoClock;
    const clock = this.getClockOffset();
    if (!vc || !clock) return;
    const ticks = ((md.rtpTimestamp - vc.rtp_origin) >>> 0);
    const captureMs = vc.epoch_ms + ticks / 90;
    const displayWall = performance.timeOrigin + (md.expectedDisplayTime ?? md.presentationTime ?? performance.now());
    const latency = displayWall + clock.offset - captureMs;
    if (latency < -1000 || latency > 60000) return; // mapa de relógio ainda não válido
    this.samples.push(latency);
    if (this.samples.length > 15) this.samples.shift();
  }

  /** Mediana dos últimos quadros, ou null. */
  value() {
    if (this.samples.length < 3) return null;
    const s = [...this.samples].sort((a, b) => a - b);
    const clock = this.getClockOffset();
    return { ms: Math.max(0, s[Math.floor(s.length / 2)]), uncertainty: clock ? clock.uncertainty : null };
  }

  status() {
    if (!this.supported || this.rtpExposed === false) return "unsupported";
    return this.value() ? "ok" : "waiting";
  }
}
