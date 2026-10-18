"use strict";

(() => {
  const target = document.querySelector(".header-actions") || document.querySelector("header");
  if (!target || document.getElementById("plan-review-entry")) return;
  const link = document.createElement("a");
  link.id = "plan-review-entry";
  link.href = "/plans";
  link.className = "status-pill";
  link.textContent = "Plan review ↗";
  link.setAttribute("aria-label", "Open decision graph review");
  target.append(link);
})();
