/* responsive.js - wraps stand-alone tables so wide ones scroll sideways
   instead of pushing the whole page wider than the phone screen.
   Load with: <script src="{% static 'js/responsive.js' %}" defer></script> */
(function () {
    'use strict';

    var SKIP_PARENTS = '.table-wrapper, .table-wrap, .table-responsive, .table-scroll, .receipt-paper';
    var SKIP_TABLES  = '.empty-form-template';

    function wrapTables(root) {
        var scope = root.querySelector ? root : document;
        var tables = scope.querySelectorAll('.page-content table');
        Array.prototype.forEach.call(tables, function (table) {
            if (table.matches(SKIP_TABLES)) return;
            if (table.closest(SKIP_PARENTS)) return;
            if (table.parentElement && table.parentElement.classList.contains('table-scroll')) return;

            var wrapper = document.createElement('div');
            wrapper.className = 'table-scroll';
            table.parentNode.insertBefore(wrapper, table);
            wrapper.appendChild(table);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { wrapTables(document); });
    } else {
        wrapTables(document);
    }
}());
