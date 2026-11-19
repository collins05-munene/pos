/* Charts for Financial Overview and Analysis (Chart.js). Each chart has a text table fallback underneath. */
(function () {
  var PAL = ['#2563eb', '#16a34a', '#f59e0b', '#dc2626', '#7c3aed', '#0891b2', '#db2777', '#65a30d'];
  function money(v) { return Number(v).toLocaleString(undefined, {maximumFractionDigits: 0}); }
  function make(id, cfg) {
    var el = document.getElementById(id);
    if (!el || !window.Chart) return;
    cfg.options = Object.assign({responsive: true, maintainAspectRatio: false,
      plugins: {legend: {position: 'bottom'}, tooltip: {callbacks: {label: function (c) { return ' ' + c.dataset.label + ': ' + money(c.parsed.y !== undefined ? c.parsed.y : c.parsed); }}}},
      scales: cfg.type === 'doughnut' ? {} : {y: {ticks: {callback: money}}}}, cfg.options || {});
    new Chart(el, cfg);
  }
  function ds(label, data, i, extra) { return Object.assign({label: label, data: data, backgroundColor: PAL[i], borderColor: PAL[i], tension: .25}, extra || {}); }
  window.finOverviewCharts = function (c) {
    make('ch-rev-exp', {type: 'bar', data: {labels: c.labels, datasets: [ds('Revenue', c.revenue, 0), ds('Expenses', c.expenses, 3)]}});
    make('ch-profit', {type: 'line', data: {labels: c.labels, datasets: [ds('Gross profit', c.gross, 1), ds('Net profit', c.net, 0)]}});
    make('ch-sales', {type: 'line', data: {labels: c.labels, datasets: [ds('Net sales', c.sales, 0, {fill: true, backgroundColor: 'rgba(37,99,235,.1)'})]}});
    make('ch-opex', {type: 'bar', data: {labels: c.labels, datasets: [ds('Operating expenses', c.opex, 2)]}});
    make('ch-cat', {type: 'doughnut', data: {labels: c.cat_labels, datasets: [{data: c.cat_values, backgroundColor: PAL}]}});
    make('ch-branch', {type: 'bar', data: {labels: c.branch_labels, datasets: [ds('Net profit', c.branch_values, 4)]}});
    make('ch-cash', {type: 'bar', data: {labels: c.labels, datasets: [ds('Cash in', c.cash_in, 1), ds('Cash out', c.cash_out, 3)]}});
  };
  window.finAnalysisCharts = function (c) {
    make('ch-a-sales', {type: 'bar', data: {labels: c.labels, datasets: [ds('Net sales', c.sales, 0), ds('Operating expenses', c.opex, 2)]}});
    make('ch-a-net', {type: 'line', data: {labels: c.labels, datasets: [ds('Net profit', c.net, 1), ds('Net cash', c.net_cash, 0)]}});
  };
})();
