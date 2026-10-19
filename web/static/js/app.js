// Controlador da interface do operador: estados reais vindos do backend, do WebRTC e do
// navegador. Nenhum estado positivo é exibido antes da confirmação correspondente.

import { login, logout, whoAmI } from "./api.js";
import { LevelMeter, OperatorMic, unlockAudio } from "./audio.js";
import { LinkStats, VideoLatency } from "./metrics.js";
import { OperatorPeer } from "./rtc.js";
import { Signaling } from "./signaling.js";

const $ = (id) => document.getElementById(id);
const nf0 = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });

const MAX_MEDIA_ATTEMPTS = 6;
const ANSWER_TIMEOUT_MS = 12000;
const DISCONNECTED_GRACE_MS = 3500;
const STALE_FRAME_MS = 3000;
const LATENCY_TARGET_MS = 300;

const END_REASONS = {
  operator: "Sessão encerrada. Os dispositivos do robô foram liberados.",
  signaling_lost: "O robô encerrou a sessão após perder a conexão com esta janela.",
  connection_lost: "O robô encerrou a sessão após perder a conexão de mídia.",
  server_shutdown: "O serviço do robô foi encerrado.",
  negotiation_failed: "Não foi possível negociar a conexão de mídia com o robô.",
  resume_rejected: "A sessão anterior já havia sido encerrada pelo robô.",
};

const S = {
  auth: "unknown",
  username: null,
  signal: "idle",
  signalReason: null,
  config: null,
  session: { state: "idle", session_id: null, started_at_ms: null },
  ownedElsewhere: false,
  wantSession: false,
  mySessionId: null,
  starting: false,
  stopping: false,
  media: "none", // none | negotiating | connecting | connected | reconnecting | lost
  mediaAttempts: 0,
  devices: null,
  robotStats: null,
  link: null,
  endedMessage: null,
};

const el = {
  body: document.body,
  main: $("main"),
  stage: $("stage"),
  video: $("video"),
  audio: $("robot-audio"),
  overlayIcon: $("overlay-icon"),
  overlayTitle: $("overlay-title"),
  overlayText: $("overlay-text"),
  overlayAction: $("overlay-action"),
  hudLive: $("hud-live"),
  hudCap: $("hud-cap"),
  hudRecv: $("hud-recv"),
  btnSession: $("btn-session"),
  sessionHint: $("session-hint"),
  btnRobotAudio: $("btn-robot-audio"),
  robotAudioState: $("robot-audio-state"),
  vol: $("vol-robot"),
  btnMic: $("btn-mic"),
  ctlMic: $("ctl-mic"),
  micState: $("mic-state"),
  pillConn: $("pill-connection"),
  pillSession: $("pill-session"),
  btnLogout: $("btn-logout"),
  notice: $("notice"),
  noticeText: $("notice-text"),
  authLayer: $("auth-layer"),
  loginForm: $("login-form"),
  loginUser: $("login-user"),
  loginPass: $("login-pass"),
  loginError: $("login-error"),
  loginSubmit: $("login-submit"),
  confirm: $("confirm-stop"),
  shortcuts: $("dlg-shortcuts"),
  stageCol: $("stage-col"),
  hudTimer: $("hud-timer"),
  hudLat: $("hud-lat"),
  hudLatVal: $("hud-lat-val"),
  hudQuality: $("hud-quality"),
  hudQualityText: $("hud-quality-text"),
  btnFullscreen: $("btn-fullscreen"),
  btnSnapshot: $("btn-snapshot"),
  btnTheme: $("btn-theme"),
  volValue: $("vol-value"),
  userChip: $("user-chip"),
  userName: $("user-name"),
  latTag: $("lat-tag"),
  spark: $("spark"),
};

const signaling = new Signaling(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
const mic = new OperatorMic();
const robotMeter = new LevelMeter($("meter-robot"));
const micMeter = new LevelMeter($("meter-mic"));
const latency = new VideoLatency(el.video, () => signaling.clock());
let peer = null;
let linkStats = new LinkStats();
let answerTimer = null;
let disconnectTimer = null;
let retryTimer = null;
let micState = { state: "off", message: null };

// ---------------------------------------------------------------- utilidades
function setIcon(container, id) {
  container.querySelector("use")?.setAttribute("href", `#${id}`);
}
function setPill(pill, tone, text) {
  pill.dataset.tone = tone;
  pill.querySelector(".pill-text").textContent = text;
}
function setStateCell(id, tone, text) {
  const dd = $(id);
  dd.dataset.tone = tone;
  dd.querySelector("span:last-child").textContent = text;
}
function setMetric(id, text, cls = "") {
  const dd = $(id);
  dd.textContent = text;
  dd.className = cls;
}
const ms = (v) => (v == null ? null : `${v < 10 ? nf1.format(v) : nf0.format(v)} ms`);
const pct = (v) => (v == null ? null : `${v < 10 ? nf1.format(v) : nf0.format(v)}%`);
function elapsed(fromMs) {
  const s = Math.max(0, Math.floor((Date.now() - fromMs) / 1000));
  const h = String(Math.floor(s / 3600)).padStart(2, "0");
  const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  return `${h}:${m}:${String(s % 60).padStart(2, "0")}`;
}

const NOTICE_ICONS = { err: "i-alert", warn: "i-alert", info: "i-check" };
const NOTICE_AUTO_HIDE_MS = 7000;
let noticeTimer = null;

/** Exibe um aviso flutuante. Avisos informativos somem sozinhos; erros e alertas ficam até serem dispensados. */
function showNotice(text, tone = "err") {
  clearTimeout(noticeTimer);
  el.noticeText.textContent = text;
  el.notice.dataset.tone = tone;
  setIcon(el.notice, NOTICE_ICONS[tone] || "i-alert");
  // Reinicia a animação de entrada e a barra de tempo quando um aviso substitui outro.
  el.notice.hidden = true;
  void el.notice.offsetWidth;
  el.notice.dataset.auto = String(tone === "info");
  el.notice.hidden = false;
  if (tone === "info") noticeTimer = setTimeout(hideNotice, NOTICE_AUTO_HIDE_MS);
}
function hideNotice() {
  clearTimeout(noticeTimer);
  el.notice.hidden = true;
}
$("notice-close").addEventListener("click", hideNotice);

// ---------------------------------------------------------------- autenticação
function setAuth(state) {
  S.auth = state;
  el.body.dataset.auth = state;
  const locked = state !== "ok";
  el.authLayer.hidden = state !== "required";
  el.main.inert = locked;
  el.btnLogout.hidden = state !== "ok";
  el.userChip.hidden = state !== "ok" || !S.username;
  el.userName.textContent = S.username || "";
  if (state === "required") setTimeout(() => (el.loginUser.value ? el.loginPass : el.loginUser).focus(), 30);
  render();
}

for (const input of [el.loginUser, el.loginPass]) {
  input.addEventListener("input", () => input.removeAttribute("aria-invalid"));
}

// Mostrar/ocultar senha e aviso de Caps Lock no campo de senha.
const btnShowPass = $("btn-show-pass");
btnShowPass.addEventListener("click", () => {
  const show = el.loginPass.type === "password";
  el.loginPass.type = show ? "text" : "password";
  btnShowPass.setAttribute("aria-pressed", String(show));
  btnShowPass.setAttribute("aria-label", show ? "Ocultar senha" : "Mostrar senha");
  btnShowPass.title = btnShowPass.getAttribute("aria-label");
  setIcon(btnShowPass, show ? "i-eye-off" : "i-eye");
  el.loginPass.focus();
});
for (const type of ["keydown", "keyup"]) {
  el.loginPass.addEventListener(type, (ev) => {
    if (typeof ev.getModifierState === "function") $("login-caps").hidden = !ev.getModifierState("CapsLock");
  });
}
el.loginPass.addEventListener("blur", () => { $("login-caps").hidden = true; });

el.loginForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const username = el.loginUser.value.trim();
  const password = el.loginPass.value;
  el.loginUser.setAttribute("aria-invalid", String(!username));
  el.loginPass.setAttribute("aria-invalid", String(!password));
  if (!username || !password) {
    el.loginError.textContent = "Preencha usuário e senha.";
    el.loginError.hidden = false;
    return;
  }
  el.loginSubmit.disabled = true;
  el.loginSubmit.dataset.busy = "true";
  el.loginError.hidden = true;
  const res = await login(username, password);
  el.loginSubmit.disabled = false;
  el.loginSubmit.dataset.busy = "false";
  if (!res.ok) {
    el.loginError.textContent = res.message;
    el.loginError.hidden = false;
    el.loginPass.value = "";
    el.loginPass.setAttribute("aria-invalid", "true");
    el.loginPass.focus();
    return;
  }
  el.loginPass.value = "";
  el.loginPass.type = "password";
  setIcon(btnShowPass, "i-eye");
  btnShowPass.setAttribute("aria-pressed", "false");
  S.username = username;
  setAuth("ok");
  signaling.connect();
});

el.btnLogout.addEventListener("click", async () => {
  if (S.wantSession) {
    showNotice("Encerre a sessão antes de sair do sistema.", "warn");
    return;
  }
  signaling.close();
  await logout();
  setAuth("required");
});

// ---------------------------------------------------------------- sinalização
signaling.addEventListener("state", (ev) => {
  const { state, reason } = ev.detail;
  S.signal = state;
  S.signalReason = reason || null;
  if (state === "closed" && reason === "unauthorized") {
    // Token expirado ou ausente: volta à autenticação sem perder a sessão do robô
    // (que permanece pelo período de tolerância e pode ser assumida após o login).
    teardownLocal();
    setAuth("required");
  }
  render();
});

signaling.addEventListener("message", (ev) => onMessage(ev.detail));

function onMessage(msg) {
  switch (msg.type) {
    case "hello":
      S.config = msg.config;
      S.devices = msg.devices;
      applySessionSnapshot(msg.session, msg.session_owned);
      if (msg.config?.video_width && msg.config?.video_height) {
        el.stage.style.setProperty("--aspect", `${msg.config.video_width} / ${msg.config.video_height}`);
      }
      onSignalReady(msg.session);
      break;
    case "session.state":
      onSessionState(msg);
      break;
    case "session.answer":
      onAnswer(msg);
      break;
    case "transport.state":
      // O robô detectou falha na conexão de mídia atual: renegocia sem esperar o
      // navegador perceber (ele só percebe após o timeout de consentimento ICE).
      if (S.wantSession && peer && (msg.state === "failed" || msg.state === "closed") &&
          S.media !== "negotiating" && S.media !== "reconnecting") {
        scheduleRenegotiation(0);
      }
      break;
    case "devices":
      S.devices = { camera: msg.camera, microphone: msg.microphone, speaker: msg.speaker };
      break;
    case "robot.stats":
      S.robotStats = msg.stats;
      S.devices = msg.devices;
      break;
    case "video.clock":
      latency.setVideoClock(msg);
      break;
    case "error":
      onServerError(msg);
      break;
  }
  render();
}

function applySessionSnapshot(snap, owned) {
  S.session = { state: snap.state, session_id: snap.session_id, started_at_ms: snap.started_at_ms };
  S.ownedElsewhere = !!owned && snap.session_id !== S.mySessionId;
}

function onSignalReady(snap) {
  if (!S.wantSession) return;
  if (snap.state === "active" && snap.session_id && snap.session_id === S.mySessionId) {
    // Sinalização voltou: retoma a mesma sessão; renegocia a mídia se ela também caiu.
    S.ownedElsewhere = false;
    signaling.send({ type: "session.resume", session_id: S.mySessionId });
    if (S.media !== "connected") scheduleRenegotiation(0);
  } else if (S.mySessionId) {
    endLocally("resume_rejected");
  } else if (S.starting) {
    negotiate();
  }
}

function onSessionState(msg) {
  const wasActive = S.session.state === "active";
  S.session = { state: msg.state, session_id: msg.session_id, started_at_ms: msg.started_at_ms };
  if (msg.state === "idle") {
    S.ownedElsewhere = false;
    if (S.stopping) {
      S.stopping = false;
      S.endedMessage = END_REASONS.operator;
      showNotice(END_REASONS.operator, "info");
    } else if (S.wantSession && (wasActive || msg.reason)) {
      endLocally(msg.reason || "connection_lost");
    }
  }
}

function onAnswer(msg) {
  clearTimeout(answerTimer);
  if (!peer || !S.wantSession) return;
  S.mySessionId = msg.session_id;
  S.ownedElsewhere = false;
  S.media = "connecting";
  peer.applyAnswer(msg.sdp).then(() => {
    S.starting = false;
    render();
  }).catch(() => {
    S.starting = false;
    scheduleRenegotiation();
  });
}

function onServerError(msg) {
  clearTimeout(answerTimer);
  const text = msg.message || "Ocorreu um erro no robô.";
  if (msg.code === "invalid_message" || msg.code === "too_large") {
    showNotice(text);
    return;
  }
  if (S.starting && !S.mySessionId) {
    // Falha ao iniciar: volta ao estado inicial com o motivo.
    teardownLocal();
    if (msg.code === "session_in_use") S.ownedElsewhere = true;
    showNotice(text, msg.code === "session_in_use" ? "warn" : "err");
    return;
  }
  if (S.wantSession && (msg.code === "negotiation_failed" || msg.code === "busy")) {
    scheduleRenegotiation();
    return;
  }
  showNotice(text);
}

// ---------------------------------------------------------------- mídia / sessão
async function negotiate() {
  if (!S.wantSession || signaling.state !== "open") return;
  clearTimeout(disconnectTimer);
  if (peer) peer.close();
  linkStats = new LinkStats();
  peer = new OperatorPeer();
  const thisPeer = peer;
  S.media = S.mySessionId ? "reconnecting" : "negotiating";
  render();

  thisPeer.addEventListener("track", (ev) => {
    if (thisPeer !== peer) return;
    if (ev.detail.kind === "audio") robotMeter.attach(ev.detail);
  });
  thisPeer.addEventListener("state", (ev) => onPeerState(thisPeer, ev.detail));

  el.video.srcObject = thisPeer.stream;
  el.audio.srcObject = thisPeer.stream;
  el.audio.play().catch(() => {});
  el.video.play().catch(() => {});
  latency.stop();
  latency.start();

  try {
    const sdp = await thisPeer.createOffer(mic.track);
    if (thisPeer !== peer) return;
    if (!signaling.send({ type: "session.start", sdp })) throw new Error("signaling closed");
    clearTimeout(answerTimer);
    answerTimer = setTimeout(() => {
      if (thisPeer === peer && S.media !== "connected") {
        if (!S.mySessionId) {
          teardownLocal();
          showNotice("O robô não respondeu ao pedido de sessão. Verifique se o serviço está em execução e tente de novo.");
          render();
        } else {
          scheduleRenegotiation();
        }
      }
    }, ANSWER_TIMEOUT_MS);
  } catch {
    if (thisPeer === peer) scheduleRenegotiation();
  }
}

function onPeerState(p, state) {
  if (p !== peer) return;
  clearTimeout(disconnectTimer);
  if (state === "connected") {
    S.media = "connected";
    S.mediaAttempts = 0;
  } else if (state === "disconnected") {
    S.media = "reconnecting";
    // Quedas curtas costumam se recuperar sozinhas; só renegocia se persistir.
    disconnectTimer = setTimeout(() => { if (p === peer && p.state !== "connected") scheduleRenegotiation(0); }, DISCONNECTED_GRACE_MS);
  } else if (state === "failed" || (state === "closed" && !p.closed)) {
    scheduleRenegotiation(0);
  }
  render();
}

function scheduleRenegotiation(delayOverride) {
  if (!S.wantSession) return;
  clearTimeout(retryTimer);
  S.mediaAttempts += 1;
  if (S.mediaAttempts > MAX_MEDIA_ATTEMPTS) {
    S.media = "lost";
    if (peer) { peer.close(); peer = null; }
    render();
    return;
  }
  S.media = "reconnecting";
  const delay = delayOverride ?? Math.min(8000, 1000 * 2 ** (S.mediaAttempts - 1));
  retryTimer = setTimeout(() => {
    if (signaling.state === "open") negotiate();
    // se a sinalização estiver fora, onSignalReady renegocia quando ela voltar
  }, delay);
  render();
}

function startSession() {
  if (!canStart()) return;
  unlockAudio();
  hideNotice();
  S.endedMessage = null;
  S.wantSession = true;
  S.starting = true;
  S.mediaAttempts = 0;
  S.mySessionId = null;
  S.robotStats = null;
  el.audio.volume = el.vol.value / 100;
  negotiate();
}

function requestStop() {
  if (!S.wantSession || S.stopping) return;
  if (typeof el.confirm.showModal === "function") {
    el.confirm.returnValue = "";
    el.confirm.showModal();
  } else if (window.confirm("Encerrar a sessão de teleoperação?")) {
    stopSession();
  }
}
el.confirm.addEventListener("close", () => { if (el.confirm.returnValue === "stop") stopSession(); });

function stopSession() {
  if (!S.wantSession) return;
  const sent = signaling.send({ type: "session.stop" });
  teardownLocal();
  if (sent) {
    S.stopping = true;
    // Se o robô não confirmar, a interface não fica presa no estado "Encerrando".
    setTimeout(() => { if (S.stopping) { S.stopping = false; render(); } }, 6000);
  } else {
    showNotice("Sem conexão com o robô: a mídia foi encerrada neste computador e o robô liberará os dispositivos automaticamente.", "warn");
  }
  render();
}

function endLocally(reason) {
  teardownLocal();
  S.endedMessage = END_REASONS[reason] || "A sessão foi encerrada.";
  showNotice(S.endedMessage, reason === "operator" ? "info" : "warn");
}

/** Libera tudo o que é local: conexão, microfone, medidores, temporizadores. */
function teardownLocal() {
  clearTimeout(answerTimer);
  clearTimeout(disconnectTimer);
  clearTimeout(retryTimer);
  if (peer) { peer.close(); peer = null; }
  mic.disable();
  robotMeter.detach();
  micMeter.detach();
  latency.stop();
  el.video.srcObject = null;
  el.audio.srcObject = null;
  S.wantSession = false;
  S.starting = false;
  S.mySessionId = null;
  S.media = "none";
  S.mediaAttempts = 0;
  S.link = null;
  S.robotStats = null;
}

function canStart() {
  return S.auth === "ok" && signaling.state === "open" && !S.wantSession && !S.stopping &&
    !S.ownedElsewhere && (S.session.state === "idle" || S.session.state === "active");
}

el.btnSession.addEventListener("click", () => (S.wantSession ? requestStop() : startSession()));
el.overlayAction.addEventListener("click", () => {
  if (S.media === "lost") {
    S.mediaAttempts = 0;
    hideNotice();
    negotiate();
  } else if (S.signal === "closed" && S.auth === "ok") {
    signaling.attempt = 0;
    signaling.connect();
  }
});

// ---------------------------------------------------------------- áudio
el.btnRobotAudio.addEventListener("click", () => {
  el.audio.muted = !el.audio.muted;
  render();
});
el.vol.addEventListener("input", () => {
  el.audio.volume = el.vol.value / 100;
  el.vol.style.setProperty("--val", `${el.vol.value}%`);
  el.volValue.textContent = `${el.vol.value}%`;
  if (el.audio.muted && el.vol.value > 0) el.audio.muted = false;
  render();
});

mic.addEventListener("state", (ev) => {
  micState = ev.detail;
  if (ev.detail.message) showNotice(ev.detail.message, ev.detail.state === "denied" ? "warn" : "err");
  render();
});

async function toggleMic() {
  if (!S.wantSession || micState.state === "requesting") return;
  if (mic.track) {
    mic.disable();
    micMeter.detach();
    await peer?.setMicTrack(null);
  } else {
    unlockAudio();
    const track = await mic.enable();
    if (!track) return;
    if (!S.wantSession) { mic.disable(); return; }
    micMeter.attach(track);
    await peer?.setMicTrack(track);
  }
  render();
}
el.btnMic.addEventListener("click", toggleMic);

// ---------------------------------------------------------------- vídeo: tela cheia e captura
function toggleFullscreen() {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else el.stageCol.requestFullscreen?.().catch(() => showNotice("O navegador não permitiu a tela cheia.", "warn"));
}
document.addEventListener("fullscreenchange", () => {
  const fs = !!document.fullscreenElement;
  setIcon(el.btnFullscreen, fs ? "i-minimize" : "i-maximize");
  el.btnFullscreen.setAttribute("aria-label", fs ? "Sair da tela cheia" : "Tela cheia");
  el.btnFullscreen.title = `${el.btnFullscreen.getAttribute("aria-label")} (F)`;
});
el.btnFullscreen.addEventListener("click", toggleFullscreen);

/** Salva o quadro exibido no momento como PNG, na resolução original do vídeo. */
function saveSnapshot() {
  const v = el.video;
  if (el.stage.dataset.view !== "live" || !v.videoWidth) return;
  const canvas = document.createElement("canvas");
  canvas.width = v.videoWidth;
  canvas.height = v.videoHeight;
  canvas.getContext("2d").drawImage(v, 0, 0);
  canvas.toBlob((blob) => {
    if (!blob) return;
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `robo-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.png`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }, "image/png");
  const flash = document.createElement("span");
  flash.className = "stage-flash";
  el.stage.append(flash);
  flash.addEventListener("animationend", () => flash.remove());
  showNotice(`Quadro salvo (${v.videoWidth}×${v.videoHeight}).`, "info");
}
el.btnSnapshot.addEventListener("click", saveSnapshot);

// ---------------------------------------------------------------- tema
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  setIcon(el.btnTheme, theme === "dark" ? "i-sun" : "i-moon");
  el.btnTheme.setAttribute("aria-label", theme === "dark" ? "Usar tema claro" : "Usar tema escuro");
  el.btnTheme.title = `${el.btnTheme.getAttribute("aria-label")} (T)`;
}
function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  try { localStorage.setItem("teleop-theme", next); } catch { /* armazenamento bloqueado */ }
  applyTheme(next);
}
el.btnTheme.addEventListener("click", toggleTheme);
applyTheme(document.documentElement.dataset.theme === "light" ? "light" : "dark");

$("btn-shortcuts").addEventListener("click", () => el.shortcuts.showModal?.());

// ---------------------------------------------------------------- atalhos de teclado
const SHORTCUTS = {
  m: () => toggleMic(),
  f: () => toggleFullscreen(),
  p: () => saveSnapshot(),
  t: () => toggleTheme(),
  "?": () => el.shortcuts.showModal?.(),
};
document.addEventListener("keydown", (ev) => {
  if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.repeat) return;
  const action = SHORTCUTS[ev.key.toLowerCase()];
  if (!action) return;
  const tag = document.activeElement?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || el.confirm.open || el.shortcuts.open || S.auth !== "ok") return;
  ev.preventDefault();
  action();
});

// ---------------------------------------------------------------- estatísticas
// Histórico de latência fim a fim (1 amostra/s, últimos 60 s) para o gráfico do painel.
// Segundos sem medição ficam nulos e aparecem como lacunas, nunca como valores inventados.
const SPARK_POINTS = 60;
const latHistory = [];

setInterval(async () => {
  if (peer && S.media !== "none") {
    try {
      S.link = linkStats.update(await peer.getStats());
    } catch { /* conexão sendo fechada */ }
  }
  if (S.wantSession) {
    const v = S.media === "connected" && S.link ? latency.value() : null;
    latHistory.push(v ? v.ms : null);
    if (latHistory.length > SPARK_POINTS) latHistory.shift();
  } else {
    latHistory.length = 0;
  }
  render();
}, 1000);

// ---------------------------------------------------------------- renderização
function render() {
  renderHeader();
  renderStage();
  renderControls();
  renderRail();
}

function renderHeader() {
  let tone = "neutral", text = "Aguardando conexão";
  if (S.auth === "required") { tone = "neutral"; text = "Autenticação necessária"; }
  else if (S.signal === "connecting") { tone = "busy"; text = "Conectando ao robô"; }
  else if (S.signal === "reconnecting") { tone = "warn"; text = "Reconectando ao robô"; }
  else if (S.signal === "closed") { tone = "err"; text = S.signalReason === "forbidden" ? "Origem não autorizada" : "Desconectado"; }
  else if (S.signal === "open") {
    if (S.media === "connected") { tone = "ok"; text = "Conectado ao robô"; }
    else if (S.media === "reconnecting") { tone = "warn"; text = "Reconectando mídia"; }
    else if (S.media === "lost") { tone = "err"; text = "Conexão perdida"; }
    else if (S.media === "negotiating" || S.media === "connecting") { tone = "busy"; text = "Conectando mídia"; }
    else { tone = "ok"; text = "Robô disponível"; }
  }
  setPill(el.pillConn, tone, text);

  const st = S.session.state;
  if (S.stopping || st === "stopping") setPill(el.pillSession, "busy", "Encerrando sessão");
  else if (S.wantSession && st === "active" && S.session.started_at_ms) setPill(el.pillSession, "live", `Sessão ativa ${elapsed(S.session.started_at_ms)}`);
  else if (S.starting || st === "starting") setPill(el.pillSession, "busy", "Iniciando sessão");
  else if (st === "active") setPill(el.pillSession, "warn", S.ownedElsewhere ? "Sessão em uso" : "Sessão sem operador");
  else setPill(el.pillSession, "neutral", "Sessão inativa");
}

function overlay(view, tone, icon, title, text, action = null) {
  el.stage.dataset.view = view;
  el.stage.dataset.tone = tone;
  setIcon(el.overlayIcon, icon);
  el.overlayTitle.textContent = title;
  el.overlayText.textContent = text;
  el.overlayAction.hidden = !action;
  if (action) el.overlayAction.textContent = action;
}

function renderStage() {
  const cam = S.devices?.camera;
  if (S.auth === "required") return overlay("idle", "", "i-lock", "Autenticação necessária", "Entre com as credenciais de operador para acessar o robô.");
  if (S.auth === "unknown") return overlay("loading", "", "i-video", "Carregando", "Verificando o acesso ao sistema.");

  if (!S.wantSession) {
    if (S.signal === "connecting") return overlay("loading", "", "i-video", "Conectando ao robô", "Aguardando o canal de controle.");
    if (S.signal === "reconnecting") return overlay("loading", "warn", "i-unplug", "Sem conexão com o robô", "Tentando reconectar automaticamente. Verifique a rede e o serviço no computador do robô.");
    if (S.signal === "closed") return overlay("idle", "err", "i-unplug", "Desconectado do robô", S.signalReason === "forbidden" ? "Este endereço não está autorizado a controlar o robô." : "O canal de controle foi fechado.", S.signalReason === "forbidden" ? null : "Reconectar");
    if (S.stopping) return overlay("loading", "", "i-video-off", "Encerrando sessão", "Liberando a webcam, o microfone e o alto-falante do robô.");
    if (S.ownedElsewhere) return overlay("idle", "warn", "i-video-off", "Sessão em uso", "Outra janela está controlando o robô. Encerre a sessão nela para operar daqui.");
    if (S.session.state === "active") return overlay("idle", "warn", "i-video-off", "Sessão aberta sem operador", "A conexão anterior caiu. Assuma a sessão para continuar a operação.");
    if (S.endedMessage) return overlay("idle", "", "i-video-off", "Sessão encerrada", S.endedMessage);
    return overlay("idle", "", "i-video", "Nenhuma sessão ativa", "Inicie a sessão para receber o vídeo e o áudio do robô.");
  }

  if (S.media === "lost") return overlay("idle", "err", "i-unplug", "Conexão perdida", "Não foi possível restabelecer a mídia com o robô após várias tentativas.", "Tentar novamente");
  if (S.signal !== "open" && S.media !== "connected") return overlay("loading", "warn", "i-unplug", "Tentando reconectar", "O canal de controle caiu. A sessão continua reservada no robô por alguns segundos.");
  if (S.media === "reconnecting") return overlay("loading", "warn", "i-refresh", "Tentando reconectar", `Restabelecendo a mídia com o robô (tentativa ${Math.max(1, S.mediaAttempts)} de ${MAX_MEDIA_ATTEMPTS}).`);
  if (S.media === "negotiating" || S.media === "connecting") return overlay("loading", "", "i-video", "Conectando", "Negociando vídeo e áudio com o robô.");

  // mídia conectada
  if (cam?.state === "unavailable") return overlay("idle", "warn", "i-video-off", "Webcam do robô indisponível", cam.message || "Verifique a conexão da webcam no robô.");
  const sinceFrame = S.link?.msSinceFrame;
  if (S.link == null || S.link.video.decoded === 0 || sinceFrame == null) {
    return overlay("loading", "", "i-video", "Aguardando o primeiro quadro", cam?.state === "starting" ? "Abrindo a webcam do robô." : "A conexão foi estabelecida; recebendo o vídeo.");
  }
  if (sinceFrame > STALE_FRAME_MS) return overlay("idle", "warn", "i-video-off", "Sem quadros recentes", "A conexão está ativa, mas o vídeo parou de chegar. Erro na transmissão ou na webcam do robô.");
  el.stage.dataset.view = "live";
  el.stage.dataset.tone = "";
  el.hudLive.classList.toggle("stale", sinceFrame > 1200);
}

function renderControls() {
  const b = el.btnSession;
  let mode = "start", label = "Iniciar sessão", icon = "i-play", disabled = !canStart(), hint = "";
  if (S.stopping) { mode = "busy"; label = "Encerrando…"; disabled = true; hint = "Aguardando confirmação do robô."; }
  else if (S.wantSession && S.starting) { mode = "busy"; label = "Iniciando…"; disabled = true; hint = "Abrindo dispositivos e negociando a mídia."; }
  else if (S.wantSession) { mode = "stop"; label = "Encerrar sessão"; icon = "i-stop"; disabled = false; hint = S.media === "connected" ? "Sessão em andamento." : "A sessão continua ativa enquanto a conexão é restabelecida."; }
  else {
    if (S.auth !== "ok") hint = "Faça login para operar.";
    else if (S.signal !== "open") hint = "Aguardando conexão com o robô.";
    else if (S.ownedElsewhere) hint = "Sessão em uso em outra janela.";
    else if (S.session.state === "active") { label = "Assumir sessão"; hint = "Retoma a sessão que ficou sem operador."; }
    else hint = "Webcam, microfone e alto-falante do robô serão abertos.";
  }
  b.dataset.mode = mode;
  b.disabled = disabled;
  setIcon(b, icon);
  b.querySelector(".btn-label").textContent = label;
  el.sessionHint.textContent = hint;

  // Áudio do robô
  const live = S.wantSession && S.media === "connected";
  const robotMic = S.devices?.microphone;
  el.btnRobotAudio.disabled = !S.wantSession;
  el.vol.disabled = !S.wantSession;
  el.btnRobotAudio.setAttribute("aria-pressed", String(S.wantSession && !el.audio.muted));
  setIcon(el.btnRobotAudio, el.audio.muted ? "i-volume-x" : "i-volume");
  el.btnRobotAudio.setAttribute("aria-label", el.audio.muted ? "Ativar o áudio do robô" : "Silenciar o áudio do robô");
  el.btnRobotAudio.title = el.btnRobotAudio.getAttribute("aria-label");
  let ra = "Sem sessão";
  if (S.wantSession) {
    if (robotMic?.state === "unavailable") ra = "Microfone do robô indisponível";
    else if (!live) ra = "Aguardando conexão";
    else if (el.audio.muted) ra = "Silenciado neste computador";
    else if (S.link?.audio.receiving) ra = "Recebendo áudio do ambiente";
    else ra = "Aguardando áudio";
  }
  el.robotAudioState.textContent = ra;

  // Microfone do operador
  const on = !!mic.track;
  el.btnMic.disabled = !S.wantSession || micState.state === "requesting";
  el.btnMic.setAttribute("aria-pressed", String(on));
  setIcon(el.btnMic, on ? "i-mic" : "i-mic-off");
  el.btnMic.setAttribute("aria-label", on ? "Parar de falar pelo robô" : "Falar pelo alto-falante do robô");
  el.btnMic.title = `${el.btnMic.getAttribute("aria-label")} (M)`;
  const spk = S.devices?.speaker;
  let ms_ = "Microfone desligado", mstate = "off";
  if (micState.state === "requesting") { ms_ = "Aguardando permissão do navegador"; mstate = "busy"; }
  else if (on) {
    mstate = "on";
    if (spk?.state === "unavailable") { ms_ = "Alto-falante do robô indisponível"; mstate = "error"; }
    else if (!live) ms_ = "Microfone ligado, aguardando conexão";
    else if (spk?.metrics?.receiving_voice) ms_ = "Sua voz está tocando no robô";
    else ms_ = "Transmitindo para o robô";
  } else if (micState.state === "denied") { ms_ = "Permissão do microfone negada"; mstate = "error"; }
  else if (micState.state === "unavailable") { ms_ = "Microfone indisponível"; mstate = "error"; }
  else if (!S.wantSession) ms_ = "Disponível durante a sessão";
  el.micState.textContent = ms_;
  el.ctlMic.dataset.state = mstate;
}

function renderRail() {
  const L = S.link;
  const live = S.wantSession && S.media === "connected" && L;

  // Latência principal: fim a fim do vídeo, quando mensurável; senão, indisponível.
  const e2e = live ? latency.value() : null;
  const status = latency.status();
  const main = $("lat-main"), unit = $("lat-unit"), cap = $("lat-caption");
  if (e2e) {
    const zone = e2e.ms <= LATENCY_TARGET_MS ? "good" : "over";
    main.textContent = nf0.format(e2e.ms);
    unit.textContent = "ms";
    cap.textContent = `Vídeo fim a fim, da captura no robô à exibição${e2e.uncertainty != null ? ` (± ${ms(e2e.uncertainty)} de sincronização de relógio)` : ""}`;
    el.latTag.dataset.zone = zone;
    el.latTag.textContent = zone === "good" ? "Dentro da meta" : "Acima da meta";
    el.hudLat.dataset.zone = zone;
    el.hudLatVal.textContent = `${nf0.format(e2e.ms)} ms`;
  } else {
    main.textContent = "—";
    unit.textContent = "";
    el.latTag.dataset.zone = "";
    el.latTag.textContent = "—";
    el.hudLat.dataset.zone = "";
    el.hudLatVal.textContent = "—";
    if (!S.wantSession) cap.textContent = "Disponível durante a sessão";
    else if (live && status === "unsupported") cap.textContent = "Medição fim a fim indisponível neste navegador; use Chrome ou Edge. Veja a latência de rede abaixo.";
    else cap.textContent = "Aguardando medição";
  }

  const show = (id, v, fmt) => { const f = live ? fmt(v) : null; setMetric(id, f ?? (live ? "Indisponível" : "—"), f ? "" : "na"); };
  show("m-rtt", L?.rttMs, ms);
  show("m-vbuf", L?.video.bufferMs, ms);
  const aRecv = !!L?.audio.receiving;
  show("m-abuf", aRecv ? L.audio.bufferMs : null, ms);
  show("m-jitter", aRecv ? L.audio.jitterMs : null, ms);
  const wsRtt = signaling.state === "open" ? signaling.lastRtt : null;
  setMetric("m-ws", wsRtt != null ? ms(wsRtt) : "—", wsRtt != null ? "" : "na");

  const lossCls = (v) => (v != null && v >= 2 ? "warn" : "");
  const lossCell = (id, v) => { const f = live ? pct(v) : null; setMetric(id, f ?? (live ? "Aguardando medição" : "—"), f ? lossCls(v) : "na"); };
  lossCell("m-vloss", L?.video.lossPct);
  if (live && S.devices?.microphone?.state === "unavailable") setMetric("m-aloss", "Sem áudio do robô", "na");
  else lossCell("m-aloss", aRecv ? L.audio.lossPct : null);
  const op = S.robotStats?.operator_audio_in;
  const opLoss = op?.loss_ratio != null ? op.loss_ratio * 100 : null;
  if (live && mic.track) lossCell("m-oloss", opLoss);
  else setMetric("m-oloss", live ? "Microfone desligado" : "—", "na");

  // Estado da conexão
  const sig = { open: ["ok", "Conectado"], connecting: ["busy", "Conectando"], reconnecting: ["warn", "Reconectando"], closed: ["err", "Desconectado"], idle: ["neutral", "—"] }[S.signal] || ["neutral", "—"];
  setStateCell("s-signal", ...sig);
  const med = { none: ["neutral", "Sem sessão"], negotiating: ["busy", "Negociando"], connecting: ["busy", "Conectando"], connected: ["ok", "Conectado"], reconnecting: ["warn", "Reconectando"], lost: ["err", "Perdida"] }[S.media];
  setStateCell("s-media", ...med);
  const st = S.session.state;
  if (S.wantSession && st === "active") setStateCell("s-session", "live", "Ativa");
  else if (st === "starting" || S.starting) setStateCell("s-session", "busy", "Iniciando");
  else if (st === "stopping" || S.stopping) setStateCell("s-session", "busy", "Encerrando");
  else if (st === "active") setStateCell("s-session", "warn", S.ownedElsewhere ? "Em uso" : "Sem operador");
  else setStateCell("s-session", "neutral", "Inativa");

  // Dispositivos
  const dev = S.devices || {};
  const camM = dev.camera?.metrics || {};
  devRow("d-camera", dev.camera, {
    active: camM.measured_fps ? `Capturando a ${nf1.format(camM.measured_fps)} FPS` : "Capturando",
    idle: "Desligada", starting: "Abrindo…", unavailable: "Indisponível",
  });
  devRow("d-microphone", dev.microphone, { active: "Captando", idle: "Desligado", starting: "Abrindo…", unavailable: "Indisponível" });
  devRow("d-speaker", dev.speaker, {
    active: dev.speaker?.metrics?.receiving_voice ? "Reproduzindo sua voz" : "Pronto",
    idle: "Desligado", starting: "Abrindo…", unavailable: "Indisponível",
  });

  renderSpark();

  // HUD do vídeo: cronômetro da sessão e qualidade do link
  el.hudTimer.textContent = S.wantSession && S.session.started_at_ms ? elapsed(S.session.started_at_ms) : "00:00:00";
  const q = live ? linkQuality(e2e?.ms, L) : null;
  el.hudQuality.dataset.q = q ? String(q.level) : "none";
  el.hudQualityText.textContent = q ? q.label : "—";

  // HUD do vídeo: taxa de captura (robô) separada da taxa recebida (este computador)
  const conf = camM.configured_fps ?? S.config?.video_fps;
  const meas = camM.measured_fps;
  el.hudCap.textContent = conf ? `${nf0.format(conf)} FPS configurado${meas ? `, ${nf1.format(meas)} medido` : ""}` : "—";
  el.hudRecv.textContent = L?.video.fps != null ? `${nf1.format(L.video.fps)} FPS` : "—";
}

/**
 * Classifica o link a partir de valores medidos: latência fim a fim (ou, sem ela, o RTT
 * da rede) e a maior perda de pacotes recente. Sem medição suficiente, retorna null.
 */
function linkQuality(e2eMs, L) {
  const delay = e2eMs ?? L?.rttMs;
  if (delay == null) return null;
  const loss = Math.max(L?.video.lossPct ?? 0, L?.audio.lossPct ?? 0);
  if (delay <= 150 && loss < 1) return { level: 3, label: "Excelente" };
  if (delay <= LATENCY_TARGET_MS && loss < 2) return { level: 2, label: "Boa" };
  if (delay <= 600 && loss < 8) return { level: 1, label: "Instável" };
  return { level: 0, label: "Ruim" };
}

// ---------------------------------------------------------------- gráfico de latência
const SPARK_W = 280, SPARK_H = 84, SPARK_PAD = 6;
let sparkHover = null; // índice da amostra sob o ponteiro, ou null

function sparkScale() {
  const vals = latHistory.filter((v) => v != null);
  const max = Math.max(LATENCY_TARGET_MS * 1.6, ...vals.map((v) => v * 1.15));
  const x = (i) => SPARK_W - (latHistory.length - 1 - i) * (SPARK_W / (SPARK_POINTS - 1));
  const y = (v) => SPARK_H - SPARK_PAD - (v / max) * (SPARK_H - SPARK_PAD * 2);
  return { x, y, hasData: vals.length > 0 };
}

function renderSpark() {
  const { x, y, hasData } = sparkScale();
  // Linha com lacunas: cada trecho contínuo de medições vira um subcaminho.
  let line = "", area = "", seg = [];
  const flush = () => {
    if (!seg.length) return;
    line += seg.map(([i, v], k) => `${k ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
    const first = x(seg[0][0]).toFixed(1), last = x(seg[seg.length - 1][0]).toFixed(1);
    area += `M${first},${SPARK_H}` + seg.map(([i, v]) => `L${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("") + `L${last},${SPARK_H}Z`;
    seg = [];
  };
  latHistory.forEach((v, i) => (v == null ? flush() : seg.push([i, v])));
  flush();
  $("spark-line").setAttribute("d", line);
  $("spark-area").setAttribute("d", area);
  const ty = y(LATENCY_TARGET_MS).toFixed(1);
  $("spark-target").setAttribute("y1", ty);
  $("spark-target").setAttribute("y2", ty);
  el.spark.dataset.hasData = String(hasData);

  // Ponto em destaque: a amostra sob o ponteiro ou, sem hover, a mais recente medida.
  let idx = sparkHover;
  if (idx == null) for (let i = latHistory.length - 1; i >= 0; i--) if (latHistory[i] != null) { idx = i; break; }
  const dot = $("spark-dot"), tip = $("spark-tip");
  const v = idx != null ? latHistory[idx] : null;
  if (v != null) {
    const px = `${(x(idx) / SPARK_W) * 100}%`;
    dot.style.left = px;
    dot.style.top = `${y(v)}px`;
    dot.hidden = false;
    tip.style.left = px;
    const ago = latHistory.length - 1 - idx;
    tip.textContent = `${nf0.format(v)} ms · ${ago === 0 ? "agora" : `há ${ago} s`}`;
  } else {
    dot.hidden = true;
    tip.textContent = sparkHover != null ? "Sem medição" : "";
    if (sparkHover != null) tip.style.left = `${(x(sparkHover) / SPARK_W) * 100}%`;
  }
  const cross = $("spark-cross");
  if (sparkHover != null) {
    const cx = x(sparkHover).toFixed(1);
    cross.setAttribute("x1", cx);
    cross.setAttribute("x2", cx);
  }
  el.spark.dataset.hover = String(sparkHover != null && latHistory.length > 0);
}

const sparkSvg = $("spark-svg");
sparkSvg.addEventListener("pointermove", (ev) => {
  if (!latHistory.length) return;
  const r = sparkSvg.getBoundingClientRect();
  const fromRight = (r.right - ev.clientX) / r.width * (SPARK_POINTS - 1);
  sparkHover = Math.max(0, Math.min(latHistory.length - 1, latHistory.length - 1 - Math.round(fromRight)));
  renderSpark();
});
sparkSvg.addEventListener("pointerleave", () => { sparkHover = null; renderSpark(); });

function devRow(id, d, labels) {
  const li = $(id);
  const state = d?.state || "idle";
  li.dataset.state = state;
  li.querySelector(".dev-state").textContent = labels[state] || "—";
  li.title = d?.message || "";
}

// ---------------------------------------------------------------- início
window.addEventListener("beforeunload", (ev) => {
  if (S.wantSession) { ev.preventDefault(); ev.returnValue = ""; }
});

(async function boot() {
  el.vol.style.setProperty("--val", `${el.vol.value}%`);
  el.volValue.textContent = `${el.vol.value}%`;
  render();
  const me = await whoAmI();
  if (me === undefined) {
    setAuth("ok");
    signaling.connect(); // servidor inacessível: a sinalização tenta reconectar
    return;
  }
  if (!me) return setAuth("required");
  S.username = me.username;
  setAuth("ok");
  signaling.connect();
})();
