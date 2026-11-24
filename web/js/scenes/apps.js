// Apps scene: a grid of PC apps configured in config.json ("apps").
// Tapping a tile asks the server to launch that entry by id (desko/collectors/apps.py).
Desko.scenes.apps = (function () {
  var E = {};
  var renderedKey = null;

  function launch(tile, id) {
    tile.classList.remove("launched", "failed");
    fetch("/api/apps/" + encodeURIComponent(id) + "/launch", { method: "POST", headers: { "X-Desko": "1" } })
      .then(function (r) { tile.classList.add(r.ok ? "launched" : "failed"); })
      .catch(function () { tile.classList.add("failed"); })
      .then(function () { setTimeout(function () { tile.classList.remove("launched", "failed"); }, 1200); });
  }

  function render(apps) {
    if (!E.grid) return;
    var items = (apps && apps.items) || [];
    var key = JSON.stringify(items);
    if (key === renderedKey) return;
    renderedKey = key;
    E.grid.innerHTML = "";
    items.forEach(function (it) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "app-tile";
      if (it.icon) {
        var img = document.createElement("img");
        img.alt = "";
        img.src = "/api/app-icon/" + encodeURIComponent(it.id) + "?v=" + encodeURIComponent(it.iconVer || "");
        b.appendChild(img);
      } else {
        var f = document.createElement("i");
        f.className = "app-fallback";
        f.textContent = (it.name || "?").charAt(0).toUpperCase();
        b.appendChild(f);
      }
      var label = document.createElement("span");
      label.textContent = it.name;
      b.appendChild(label);
      b.addEventListener("click", function (e) { e.stopPropagation(); launch(b, it.id); });
      // A tap on a tile must not count toward the double-tap-anywhere gesture.
      b.addEventListener("touchend", function (e) { e.stopPropagation(); }, { passive: true });
      E.grid.appendChild(b);
    });
    if (E.meta) E.meta.textContent = items.length + (items.length === 1 ? " APP" : " APPS");
    if (E.empty) E.empty.hidden = items.length > 0;
  }

  return {
    onEnter: function (state) {
      E = { grid: document.getElementById("a-grid"), meta: document.getElementById("a-meta"), empty: document.getElementById("a-empty") };
      renderedKey = null;
      render(state.apps);
    },
    onStateChange: function (state) { render(state.apps); },
    onTick: function () {},
    onExit: function () {},
  };
})();
