// Aplica o tema antes da primeira pintura (carregado de forma síncrona no <head>).
// Padrão: grafite escuro. A escolha do operador fica salva neste navegador.
(function () {
  var saved = null;
  try { saved = localStorage.getItem("teleop-theme"); } catch (e) { /* armazenamento bloqueado */ }
  document.documentElement.dataset.theme = saved === "light" ? "light" : "dark";
})();
