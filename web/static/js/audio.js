// Microfone do operador e medidores de nível (WebAudio). Os medidores leem o sinal real
// das trilhas; não há animação decorativa.

let ctx = null;

/** Deve ser chamado dentro de um gesto do usuário para liberar o áudio no navegador. */
export function unlockAudio() {
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return null;
  if (!ctx) ctx = new AC({ latencyHint: "interactive" });
  if (ctx.state === "suspended") ctx.resume().catch(() => {});
  return ctx;
}

export class LevelMeter {
  constructor(barEl) {
    this.bar = barEl;
    this.source = null;
    this.analyser = null;
    this.buf = null;
    this.level = 0;
    this._raf = null;
  }

  attach(track) {
    this.detach();
    if (!ctx || !track) return;
    this.source = ctx.createMediaStreamSource(new MediaStream([track]));
    this.analyser = ctx.createAnalyser();
    this.analyser.fftSize = 512;
    this.buf = new Float32Array(this.analyser.fftSize);
    this.source.connect(this.analyser);
    const loop = () => {
      this.analyser.getFloatTimeDomainData(this.buf);
      let peak = 0;
      for (let i = 0; i < this.buf.length; i++) peak = Math.max(peak, Math.abs(this.buf[i]));
      this.level = Math.max(peak, this.level * 0.86);
      // escala perceptiva aproximada
      const pct = Math.min(100, Math.sqrt(this.level) * 115);
      this.bar.style.width = `${pct.toFixed(1)}%`;
      this._raf = requestAnimationFrame(loop);
    };
    this._raf = requestAnimationFrame(loop);
  }

  detach() {
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = null;
    try { this.source?.disconnect(); } catch { /* ignora */ }
    this.source = this.analyser = null;
    this.level = 0;
    this.bar.style.width = "0%";
  }
}

const MIC_ERRORS = {
  NotAllowedError: { state: "denied", message: "Permissão do microfone negada. Libere o acesso ao microfone nas configurações do site e tente de novo." },
  SecurityError: { state: "denied", message: "O navegador bloqueou o microfone. Libere o acesso nas configurações do site." },
  NotFoundError: { state: "unavailable", message: "Nenhum microfone encontrado neste computador. Conecte um microfone e tente de novo." },
  NotReadableError: { state: "unavailable", message: "O microfone está em uso por outro aplicativo ou falhou ao iniciar." },
  OverconstrainedError: { state: "unavailable", message: "O microfone não atende às configurações de áudio exigidas." },
  AbortError: { state: "unavailable", message: "A captura do microfone foi interrompida. Tente de novo." },
};

export class OperatorMic extends EventTarget {
  constructor() {
    super();
    this.track = null;
    this.state = "off"; // off | requesting | on | denied | unavailable
  }

  get available() { return !!navigator.mediaDevices?.getUserMedia; }

  _set(state, message = null) {
    this.state = state;
    this.dispatchEvent(new CustomEvent("state", { detail: { state, message } }));
  }

  async enable() {
    if (this.track) return this.track;
    if (!this.available) {
      this._set("unavailable", window.isSecureContext
        ? "Este navegador não permite capturar o microfone."
        : "O navegador só libera o microfone em conexão segura (HTTPS). Acesse o robô pelo endereço https://.");
      return null;
    }
    this._set("requesting");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
        video: false,
      });
      this.track = stream.getAudioTracks()[0];
      this.track.addEventListener("ended", () => {
        this.track = null;
        this._set("unavailable", "O microfone foi desconectado.");
      });
      this._set("on");
      return this.track;
    } catch (err) {
      const e = MIC_ERRORS[err?.name] || { state: "unavailable", message: "Não foi possível acessar o microfone." };
      this._set(e.state, e.message);
      return null;
    }
  }

  disable() {
    if (this.track) this.track.stop();
    this.track = null;
    if (this.state !== "denied" && this.state !== "unavailable") this._set("off");
  }
}
