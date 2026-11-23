// The home page's charts (templates/charts.html): sort the bars, show all of them, and say a bar's
// numbers when it is pointed at or focused. The page works without this file.

function sortBars(list, order) {
  const rows = Array.from(list.children);
  rows.sort(function (first, second) {
    const byName = first.dataset.name.localeCompare(second.dataset.name);
    if (order === "name") {
      return byName;
    }
    let difference = 0;
    if (order === "reported") {
      difference = Number(second.dataset.reported) - Number(first.dataset.reported);
    } else if (order === "unreported") {
      difference = Number(first.dataset.reported) - Number(second.dataset.reported);
    }
    if (difference === 0) {
      difference = Number(second.dataset.total) - Number(first.dataset.total);
    }
    return difference || byName;
  });
  rows.forEach(function (row, index) {
    row.classList.toggle("extra", index >= 15);
    list.appendChild(row);
  });
}

function setUpChart(chart) {
  const list = chart.querySelector("[data-bars]");
  if (!list) {
    return;
  }
  const controls = chart.querySelector(".chart-controls");
  const sort = chart.querySelector("[data-sort]");
  const more = chart.querySelector("[data-more]");
  const detail = chart.querySelector("[data-detail]");
  const note = detail.textContent;
  controls.hidden = false;

  sort.addEventListener("change", function () {
    sortBars(list, sort.value);
  });
  if (more) {
    const label = more.textContent;
    more.addEventListener("click", function () {
      const showing = chart.classList.toggle("all");
      more.textContent = showing ? "Show fewer" : label;
    });
  }
  function say(event) {
    const row = event.target.closest(".bar-row");
    detail.textContent = row ? row.dataset.detail : note;
  }
  list.addEventListener("pointerover", say);
  list.addEventListener("focusin", say);
  list.addEventListener("pointerleave", function () {
    detail.textContent = note;
  });
}

document.querySelectorAll("[data-chart]").forEach(setUpChart);
