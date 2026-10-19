// МедСоцПлатформа — минимальный SPA без сборки. API — тот же источник (/api/v1).
// Безопасность: весь пользовательский текст выводится через textContent (никакого innerHTML
// с данными), скрипты только из файла (CSP default-src 'self'). Токены — в localStorage:
// компромисс простоты; CSRF неприменим (токен в заголовке Authorization, не в cookie).
"use strict";

const API = "/api/v1";
const store = {
  get access() { return localStorage.getItem("access_token"); },
  get refresh() { return localStorage.getItem("refresh_token"); },
  save(t) { localStorage.setItem("access_token", t.access_token); localStorage.setItem("refresh_token", t.refresh_token); },
  clear() { localStorage.removeItem("access_token"); localStorage.removeItem("refresh_token"); },
};
const $ = (sel, root = document) => root.querySelector(sel);
const app = $("#app");
let categoriesCache = null;

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = isError ? "error" : "";
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 4000);
}

class ApiError extends Error {
  constructor(status, body) {
    super((body && body.error && body.error.message) || `HTTP ${status}`);
    this.status = status;
  }
}

async function tryRefresh() {
  if (!store.refresh) return false;
  const r = await fetch(`${API}/auth/refresh`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: store.refresh }),
  });
  if (!r.ok) { store.clear(); return false; }
  store.save(await r.json());
  return true;
}

async function api(path, { method = "GET", body, auth = true, retry = true } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (auth && store.access) headers.Authorization = `Bearer ${store.access}`;
  const r = await fetch(`${API}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (r.status === 401 && auth && retry && await tryRefresh()) return api(path, { method, body, auth, retry: false });
  if (r.status === 204) return null;
  const data = await r.json().catch(() => null);
  if (!r.ok) {
    if (r.status === 401) { store.clear(); location.hash = "#/login"; }
    throw new ApiError(r.status, data);
  }
  return data;
}

function render(tplId) {
  const node = $(`#${tplId}`).content.cloneNode(true);
  app.replaceChildren(node);
  return app;
}

function text(el, value) { el.textContent = value == null ? "" : String(value); return el; }
function fmtDate(iso) { return new Date(iso).toLocaleString("ru-RU"); }

async function categories() {
  if (!categoriesCache) categoriesCache = await api("/categories", { auth: false });
  return categoriesCache;
}

function fillSelect(select, items, withEmpty = false) {
  for (const c of items) {
    const o = document.createElement("option");
    o.value = c.id; o.textContent = c.name;
    select.append(o);
  }
  if (!withEmpty && items.length) select.value = items[0].id;
}

// ── экраны ──────────────────────────────────────────────────────────────────────────────
function viewAuth() {
  render("tpl-auth");
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      store.save(await api("/auth/login", { method: "POST", auth: false, body: { email: f.get("email"), password: f.get("password") } }));
      location.hash = "#/feed";
    } catch (err) { toast(err.message, true); }
  });
  $("#register-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      store.save(await api("/auth/register", { method: "POST", auth: false, body: {
        email: f.get("email"), password: f.get("password"), display_name: f.get("display_name"), role: f.get("role") } }));
      toast("Регистрация выполнена");
      location.hash = "#/feed";
    } catch (err) { toast(err.message, true); }
  });
}

function postItem(p) {
  const li = $("#tpl-post-item").content.cloneNode(true).firstElementChild;
  const a = $(".title", li);
  a.href = `#/post/${p.id}`;
  text(a, p.title);
  text($(".meta", li), `${p.author_name} · ${p.category_name} · ${fmtDate(p.created_at)} · 💬 ${p.comments_count} · ❤ ${p.reactions_count}`);
  text($(".excerpt", li), p.body.length > 240 ? `${p.body.slice(0, 240)}…` : p.body);
  return li;
}

async function viewFeed(params) {
  render("tpl-feed");
  const list = $("#posts"), more = $("#more"), filter = $("#category-filter");
  fillSelect(filter, await categories(), true);
  const query = new URLSearchParams(params || "");
  if (query.get("category_id")) filter.value = query.get("category_id");
  filter.addEventListener("change", () => { location.hash = filter.value ? `#/feed?category_id=${filter.value}` : "#/feed"; });
  $("#search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    location.hash = `#/search?q=${encodeURIComponent(new FormData(e.target).get("q"))}`;
  });
  let lastId = null;
  async function load() {
    const qs = new URLSearchParams();
    if (filter.value) qs.set("category_id", filter.value);
    if (lastId) qs.set("before_id", lastId);
    const posts = await api(`/posts?${qs}`);
    posts.forEach((p) => list.append(postItem(p)));
    if (posts.length) lastId = posts[posts.length - 1].id;
    more.hidden = posts.length === 0;
  }
  more.addEventListener("click", () => load().catch((err) => toast(err.message, true)));
  await load();
}

async function viewSearch(params) {
  render("tpl-feed");
  const q = new URLSearchParams(params).get("q") || "";
  text($("#feed-title"), `Поиск: ${q}`);
  $("#search-form").q.value = q;
  $("#search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    location.hash = `#/search?q=${encodeURIComponent(new FormData(e.target).get("q"))}`;
  });
  fillSelect($("#category-filter"), await categories(), true);
  $("#category-filter").disabled = true;
  const hits = await api(`/search?q=${encodeURIComponent(q)}`);
  hits.forEach((p) => $("#posts").append(postItem(p)));
  if (!hits.length) text($("#posts"), "Ничего не найдено");
}

async function viewPost(id) {
  const p = await api(`/posts/${id}`);
  render("tpl-post");
  text($(".title"), p.title);
  text($(".meta"), `${p.author_name} (${p.author_role}) · ${p.category_name} · ${fmtDate(p.created_at)} · статус: ${p.status}`);
  text($(".body"), p.body);
  const count = $(".count");
  const showReaction = (kind, n) => {
    text(count, `реакций: ${n}${kind ? ` · ваша: ${kind}` : ""}`);
    document.querySelectorAll(".reactions button[data-kind]").forEach((b) => b.classList.toggle("active", b.dataset.kind === kind));
  };
  showReaction(p.my_reaction, p.reactions_count);
  document.querySelectorAll(".reactions button").forEach((b) => b.addEventListener("click", async () => {
    try {
      if (b.dataset.kind) {
        const r = await api(`/posts/${id}/reaction`, { method: "PUT", body: { kind: b.dataset.kind } });
        showReaction(r.kind, r.reactions_count);
      } else {
        await api(`/posts/${id}/reaction`, { method: "DELETE" });
        viewPost(id);
      }
    } catch (err) { toast(err.message, true); }
  }));
  const ul = $(".comments");
  for (const c of p.comments) {
    const li = document.createElement("li");
    const who = document.createElement("strong");
    text(who, `${c.author_name}: `);
    li.append(who, document.createTextNode(c.body));
    ul.append(li);
  }
  $("#comment-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api(`/posts/${id}/comments`, { method: "POST", body: { body: new FormData(e.target).get("body") } });
      toast(r.status === "approved" ? "Комментарий опубликован" : `Комментарий на модерации (${r.status})`);
      viewPost(id);
    } catch (err) { toast(err.message, true); }
  });
}

async function viewNew() {
  render("tpl-new");
  fillSelect($("select[name=category_id]"), await categories());
  $("#post-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const r = await api("/posts", { method: "POST", body: { title: f.get("title"), body: f.get("body"), category_id: Number(f.get("category_id")) } });
      toast(r.status === "approved" ? "Опубликовано" : `Отправлено на модерацию: ${r.status}`);
      location.hash = `#/post/${r.id}`;
    } catch (err) { toast(err.message, true); }
  });
}

async function viewMe() {
  const me = await api("/users/me");
  render("tpl-me");
  const dl = $(".profile");
  const rows = [["Имя", me.display_name], ["Роль", me.role], ["Город", me.city], ["Год рождения", me.birth_year],
    ["Специальность", me.specialty_name], ["О себе", me.bio], ["С нами с", fmtDate(me.member_since)]];
  for (const [k, v] of rows) {
    dl.append(text(document.createElement("dt"), k), text(document.createElement("dd"), v ?? "—"));
  }
  const form = $("#profile-form");
  form.display_name.value = me.display_name; form.city.value = me.city;
  form.bio.value = me.bio; form.birth_year.value = me.birth_year ?? "";
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(form);
    const body = { display_name: f.get("display_name"), city: f.get("city"), bio: f.get("bio") };
    if (f.get("birth_year")) body.birth_year = Number(f.get("birth_year"));
    try { await api("/users/me", { method: "PATCH", body }); toast("Профиль сохранён"); viewMe(); }
    catch (err) { toast(err.message, true); }
  });
}

async function viewStats() {
  const s = await api("/stats");
  render("tpl-stats");
  text($(".window"), s.window_days);
  const dl = $(".totals");
  for (const [k, v] of [["Публикаций", s.posts_total], ["Комментариев", s.comments_total],
    ["Реакций", s.reactions_total], ["Новых пользователей", s.new_users]]) {
    dl.append(text(document.createElement("dt"), k), text(document.createElement("dd"), v));
  }
  for (const c of s.top_categories) $(".top").append(text(document.createElement("li"), `${c.name} — ${c.posts}`));
  const tbody = $(".by-day tbody");
  for (const d of s.posts_by_day) {
    const tr = document.createElement("tr");
    for (const v of [d.day, d.status, d.count]) tr.append(text(document.createElement("td"), v));
    tbody.append(tr);
  }
}

// ── маршрутизация ───────────────────────────────────────────────────────────────────────
async function route() {
  const [path, params] = (location.hash.slice(1) || "/feed").split("?");
  const loggedIn = Boolean(store.access);
  $("#nav").hidden = !loggedIn;
  if (!loggedIn && path !== "/login") { location.hash = "#/login"; return; }
  try {
    if (path === "/login") viewAuth();
    else if (path === "/feed") await viewFeed(params);
    else if (path === "/search") await viewSearch(params);
    else if (path.startsWith("/post/")) await viewPost(Number(path.split("/")[2]));
    else if (path === "/new") await viewNew();
    else if (path === "/me") await viewMe();
    else if (path === "/stats") await viewStats();
    else location.hash = "#/feed";
  } catch (err) { toast(err.message, true); }
}

$("#logout").addEventListener("click", async () => {
  if (store.refresh) await api("/auth/logout", { method: "POST", auth: false, body: { refresh_token: store.refresh } }).catch(() => {});
  store.clear();
  location.hash = "#/login";
});
window.addEventListener("hashchange", route);
route();
