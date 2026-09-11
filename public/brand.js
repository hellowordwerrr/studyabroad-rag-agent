// 留学智库品牌兜底：替换 favicon，并把页面标题里残留的 Chainlit 换成品牌名
// （服务端 [UI] name 已设置标题，此处只兜底未生效的情况）
(function () {
  try {
    var icon =
      document.querySelector("link[rel='icon']") ||
      document.querySelector("link[rel='shortcut icon']");
    if (icon) icon.setAttribute("href", "/public/favicon.svg");
  } catch (e) {}
  try {
    var title = document.querySelector("title");
    if (title && /chainlit/i.test(title.textContent || "")) {
      title.textContent = "留学智库";
    }
  } catch (e) {}
})();
