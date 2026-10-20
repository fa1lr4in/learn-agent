// 在样式和页面加载前应用外观，避免已保存的深色主题先闪出浅色。
const themeKey = "learn-agent-theme";
const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
let preference = "system";
try {
  const saved = localStorage.getItem(themeKey);
  if (["system", "light", "dark"].includes(saved)) preference = saved;
} catch {
  // 浏览器禁止存储时，本次页面仍可切换主题。
}

function applyTheme() {
  document.documentElement.dataset.theme = preference === "system"
    ? (systemTheme.matches ? "dark" : "light") : preference;
}

applyTheme();
systemTheme.addEventListener("change", () => {
  if (preference === "system") applyTheme();
});

document.addEventListener("DOMContentLoaded", () => {
  const select = document.getElementById("theme");
  select.value = preference;
  select.addEventListener("change", () => {
    preference = select.value;
    applyTheme();
    try { localStorage.setItem(themeKey, preference); } catch { /* 仅保留本次选择。 */ }
  });
});
