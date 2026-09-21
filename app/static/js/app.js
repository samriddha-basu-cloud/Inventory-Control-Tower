// Global search (Ctrl/Cmd+K command palette-lite) and small UI helpers.
document.addEventListener("DOMContentLoaded", function () {
  const input = document.getElementById("ict-search-input");
  const resultsBox = document.getElementById("ict-search-results");
  if (input) {
    let timer = null;
    input.addEventListener("input", function () {
      clearTimeout(timer);
      const q = input.value.trim();
      if (!q) { resultsBox.innerHTML = ""; resultsBox.classList.add("d-none"); return; }
      timer = setTimeout(function () {
        fetch("/search?q=" + encodeURIComponent(q))
          .then((r) => r.json())
          .then((data) => {
            resultsBox.innerHTML = "";
            if (!data.length) { resultsBox.classList.add("d-none"); return; }
            data.forEach((r) => {
              const a = document.createElement("a");
              a.href = r.url;
              a.className = "list-group-item list-group-item-action py-2";
              a.innerHTML = `<span class="badge bg-secondary me-2">${r.type}</span>${r.label}`;
              resultsBox.appendChild(a);
            });
            resultsBox.classList.remove("d-none");
          });
      }, 200);
    });
    document.addEventListener("click", function (e) {
      if (!resultsBox.contains(e.target) && e.target !== input) {
        resultsBox.classList.add("d-none");
      }
    });
  }

  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === "k") {
      e.preventDefault();
      if (input) input.focus();
    }
  });
});
