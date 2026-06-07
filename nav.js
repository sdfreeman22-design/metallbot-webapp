/* ───────────────────────────────────────────────────────────────────────────
   nav.js — единая кнопка «Назад» для всех модулей мини-аппа.

   Зачем: модули (Кооперация / Калькулятор / Документы / Металл) — отдельные
   страницы, плюс внутри есть подэкраны (карточка, редактирование, фото).
   Раньше из них можно было выйти только закрыв весь мини-апп. Теперь —
   нативная Telegram BackButton делает «шаг назад»:
     1) если на странице открыт подэкран (карточка/модалка/фото) — закрыть его;
     2) иначе — вернуться на предыдущий модуль (history.back);
     3) если возвращаться некуда (точка входа) — кнопка скрыта (остаётся ✕).

   Подключается на каждой странице: <script src="/nav.js"></script>
   (после telegram-web-app.js).

   Опциональные хуки страницы (если есть свои подэкраны):
     window.__navHasOverlay = () => boolean   // открыт ли подэкран
     window.__navBack        = () => boolean   // закрыть верхний подэкран; true=закрыл
   После открытия/закрытия подэкрана вызывайте window.NavBack.refresh().
─────────────────────────────────────────────────────────────────────────── */
(function () {
  var tg = (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp : null;
  if (tg) { try { tg.ready(); } catch (e) {} }

  // ── Глубина навигации внутри мини-аппа ────────────────────────────────────
  // Считаем «шаги» внутри нашего приложения через history.state + sessionStorage,
  // игнорируя «фантомные» записи истории, которые иногда оставляет Telegram.
  // Точка входа → depth 0 (возвращаться некуда). Каждый переход по ссылке → +1.
  // Возврат (history.back) восстанавливает уже помеченную запись → depth не растёт.
  var navDepth = null;   // null = sessionStorage недоступен → fallback на history.length
  try {
    var st = (history.state && typeof history.state === 'object') ? history.state : {};
    if (typeof st.navDepth !== 'number') {
      var cur = parseInt(sessionStorage.getItem('navCursor'), 10);
      navDepth = (isNaN(cur) ? -1 : cur) + 1;
      st.navDepth = navDepth;
      try { history.replaceState(st, ''); } catch (e) {}
    } else {
      navDepth = st.navDepth;
    }
    sessionStorage.setItem('navCursor', String(navDepth));
  } catch (e) { navDepth = null; }

  function canGoBack() {
    if (navDepth !== null) return navDepth > 0;
    try { return window.history.length > 1; } catch (e) { return false; }
  }
  function hasOverlay() {
    try { return typeof window.__navHasOverlay === 'function' && !!window.__navHasOverlay(); }
    catch (e) { return false; }
  }
  function refresh() {
    if (!tg || !tg.BackButton) return;
    try {
      if (hasOverlay() || canGoBack()) tg.BackButton.show();
      else tg.BackButton.hide();
    } catch (e) {}
  }
  function handleBack() {
    // 1) закрыть открытый подэкран этой страницы
    try {
      if (typeof window.__navBack === 'function' && window.__navBack() === true) { refresh(); return; }
    } catch (e) {}
    // 2) вернуться на предыдущий модуль
    if (canGoBack()) { try { window.history.back(); return; } catch (e) {} }
    // 3) идти некуда — закрыть мини-апп (на всякий случай; обычно кнопка тут скрыта)
    if (tg) { try { tg.close(); } catch (e) {} }
  }

  window.NavBack = { refresh: refresh, handle: handleBack, canGoBack: canGoBack };

  // ── Чистим «залипшую» MainButton ──────────────────────────────────────────
  // MainButton в Telegram — глобальная на весь webview: если её показал один
  // модуль (Калькулятор металла «РАССЧИТАТЬ»), при переходе на другой модуль
  // она «висит» с мёртвым обработчиком. Прячем её на каждой странице, КРОМЕ тех,
  // что ею реально пользуются (ставят window.__usesMainButton = true).
  // Делаем это на 'pageshow' (а не синхронно): к этому моменту inline-скрипт
  // страницы уже выставил флаг — поэтому на самом калькуляторе кнопка не мигает.
  function syncMainButton() {
    if (!tg || !tg.MainButton || window.__usesMainButton) return;
    try { tg.MainButton.hide(); } catch (e) {}
  }

  if (tg && tg.BackButton) { try { tg.BackButton.onClick(handleBack); } catch (e) {} }
  refresh();
  window.addEventListener('pageshow', refresh);
  window.addEventListener('pageshow', syncMainButton);

  // ── Доступ к модулям по роли ────────────────────────────────────────────────
  // Роль приходит от бота через ?r=<role> и хранится на сессию мини-аппа.
  // Кооперация (/), Металл (/calc*.html), Документы (/docs.html) — только
  // manager/admin. Допуски (/tolerances.html) — всем ролям (бесплатная справка).
  // Гейтим именно НАВИГАЦИЮ: лишние ссылки прячем, в оставшихся пробрасываем роль,
  // чтобы доступ сохранялся при переходах. (Защита данных — на API-эндпоинтах.)
  var ROLE = '';
  try {
    var _qr = new URLSearchParams(location.search).get('r');
    if (_qr) sessionStorage.setItem('mb_role', _qr);
    ROLE = sessionStorage.getItem('mb_role') || '';
  } catch (e) {}
  var FULL = (ROLE === 'manager' || ROLE === 'admin');
  function gateNav() {
    var links = document.querySelectorAll('.nav a, .nav-links a');
    for (var i = 0; i < links.length; i++) {
      var a = links[i], href = a.getAttribute('href') || '';
      var isTol = href.indexOf('tolerances') >= 0;   // Допуски — всем
      if (!isTol && !FULL) { a.style.display = 'none'; continue; }
      if (ROLE && href && href.charAt(0) === '/' && href.indexOf('r=') < 0) {
        a.setAttribute('href', href + (href.indexOf('?') >= 0 ? '&' : '?') + 'r=' + encodeURIComponent(ROLE));
      }
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', gateNav);
  else gateNav();
  window.addEventListener('pageshow', gateNav);
})();
