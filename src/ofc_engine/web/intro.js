"use strict";

(() => {
  const root = document.getElementById("ofc-intro");
  const button = document.getElementById("ofc-intro-understand");
  const workspace = document.querySelector("body.studio-page > .shell");
  if (!root || !button || !workspace) return;

  const body = document.body;
  const prefersReducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches
    || document.documentElement.classList.contains("motion-off");
  let opened = false;

  // The server renders the existing workspace inert; the gate never uses storage.
  const unlock = () => {
    body.classList.add("ofc-intro-opened");
    workspace.removeAttribute("inert");
    workspace.removeAttribute("aria-hidden");
  };

  button.addEventListener("click", () => {
    if (opened) return;
    opened = true;
    button.disabled = true;

    if (prefersReducedMotion()) {
      unlock();
      root.remove();
      document.getElementById("main")?.focus({preventScroll: true});
      return;
    }

    const fragments = document.getElementById("ofc-intro-particles");
    if (fragments) {
      const pieces = document.createDocumentFragment();
      for (let i = 0; i < 16; i += 1) {
        const chip = document.createElement("span");
        chip.className = "ofc-intro__particle";
        pieces.append(chip);
      }
      fragments.append(pieces);
    }

    body.classList.add("ofc-intro-leaving");
    window.setTimeout(unlock, 680);
    window.setTimeout(() => {
      root.remove();
      body.classList.remove("ofc-intro-leaving");
      document.getElementById("main")?.focus({preventScroll: true});
    }, 1210);
  }, {once: true});
})();
