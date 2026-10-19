// Conexão WebRTC do lado do operador.
// Vídeo: somente recepção. Áudio: recebe o ambiente do robô e envia a voz do operador
// (a trilha do microfone é trocada com replaceTrack, sem renegociar).

const ICE_GATHER_TIMEOUT_MS = 2500;

export class OperatorPeer extends EventTarget {
  constructor() {
    super();
    this.pc = new RTCPeerConnection({ iceServers: [], bundlePolicy: "max-bundle" });
    this.stream = new MediaStream();
    this.videoTransceiver = this.pc.addTransceiver("video", { direction: "recvonly" });
    this.audioTransceiver = this.pc.addTransceiver("audio", { direction: "sendrecv" });
    this.closed = false;

    this.pc.ontrack = (ev) => {
      // Ajuste fino: o vídeo é exibido assim que decodificado, sem atraso extra de buffer.
      if (ev.receiver && "playoutDelayHint" in ev.receiver && ev.track.kind === "video") {
        try { ev.receiver.playoutDelayHint = 0; } catch { /* opcional */ }
      }
      this.stream.addTrack(ev.track);
      this.dispatchEvent(new CustomEvent("track", { detail: ev.track }));
    };
    this.pc.onconnectionstatechange = () => {
      this.dispatchEvent(new CustomEvent("state", { detail: this.pc.connectionState }));
    };
  }

  get state() { return this.pc.connectionState; }

  async createOffer(micTrack) {
    if (micTrack) await this.audioTransceiver.sender.replaceTrack(micTrack);
    const offer = await this.pc.createOffer();
    await this.pc.setLocalDescription(offer);
    await this._waitIceGathering();
    return this.pc.localDescription.sdp;
  }

  _waitIceGathering() {
    if (this.pc.iceGatheringState === "complete") return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => {
        clearTimeout(timer);
        this.pc.removeEventListener("icegatheringstatechange", check);
        resolve();
      };
      const check = () => { if (this.pc.iceGatheringState === "complete") done(); };
      const timer = setTimeout(done, ICE_GATHER_TIMEOUT_MS);
      this.pc.addEventListener("icegatheringstatechange", check);
    });
  }

  async applyAnswer(sdp) {
    await this.pc.setRemoteDescription({ type: "answer", sdp });
  }

  async setMicTrack(track) {
    if (this.closed) return;
    await this.audioTransceiver.sender.replaceTrack(track || null);
  }

  getStats() { return this.pc.getStats(); }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.pc.ontrack = null;
    this.pc.onconnectionstatechange = null;
    for (const t of this.stream.getTracks()) t.stop();
    try { this.pc.close(); } catch { /* ignora */ }
  }
}
