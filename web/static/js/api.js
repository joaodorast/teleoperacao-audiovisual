// Autenticação HTTP. O token fica em cookie HttpOnly; o JavaScript nunca o lê.

async function request(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body = null;
  try { body = await res.json(); } catch { /* sem corpo */ }
  return { ok: res.ok, status: res.status, body };
}

export async function whoAmI() {
  try {
    const r = await request("/api/me");
    return r.ok && r.body?.authenticated ? r.body : null;
  } catch {
    return undefined; // servidor inacessível
  }
}

export async function login(username, password) {
  let r;
  try {
    r = await request("/api/login", { method: "POST", body: JSON.stringify({ username, password }) });
  } catch {
    return { ok: false, message: "Não foi possível contatar o computador do robô. Verifique a rede." };
  }
  if (r.ok) return { ok: true };
  if (r.status === 401) return { ok: false, message: "Usuário ou senha inválidos." };
  if (r.status === 429) return { ok: false, message: r.body?.detail || "Muitas tentativas. Aguarde um minuto." };
  if (r.status === 422) return { ok: false, message: "Preencha usuário e senha." };
  return { ok: false, message: "Falha ao autenticar. Tente novamente." };
}

export async function logout() {
  try { await request("/api/logout", { method: "POST" }); } catch { /* ignora */ }
}
