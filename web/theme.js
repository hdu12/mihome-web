/* 主题初始化：必须在首次绘制前生效，所以放 head 里同步引用。
   优先级：URL 参数 ?theme= > 用户上次的选择 > 跟随系统。
   切换后的持久化在 app.js 的 applyTheme 里。 */
(function () {
  var t = null;
  try { t = new URLSearchParams(location.search).get("theme"); } catch (e) {}
  if (t !== "light" && t !== "dark") {
    try { t = localStorage.getItem("mihome_theme"); } catch (e) {}
  }
  if (t !== "light" && t !== "dark") {
    t = (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches)
      ? "light" : "dark";
  }
  document.documentElement.setAttribute("data-theme", t);
})();
