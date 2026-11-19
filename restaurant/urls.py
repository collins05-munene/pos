from django.urls import path

from . import api_views, views as v

app_name = "restaurant"

urlpatterns = [
    path("", v.dashboard, name="dashboard"),

    # floor
    path("floor/", v.floor, name="floor"),
    path("floor/edit/", v.floor_editor, name="floor-editor"),
    path("api/floor/", v.floor_data, name="floor-data"),
    path("api/floor/layout/", v.layout_save, name="layout-save"),
    path("api/table/save/", v.table_save, name="table-save"),
    path("api/table/<int:pk>/delete/", v.table_delete, name="table-delete"),
    path("api/table/<int:pk>/<str:action>/", v.table_action, name="table-action"),
    path("api/zone/save/", v.zone_save, name="zone-save"),

    # orders
    path("orders/", v.orders_list, name="orders"),
    path("order/<int:pk>/", v.order_page, name="order"),
    path("api/order/create/", v.order_create, name="order-create"),
    path("api/order/<int:pk>/", v.order_data, name="order-data"),
    path("api/order/<int:pk>/<str:action>/", v.order_action, name="order-action"),
    path("api/menu/", v.menu_data, name="menu-data"),

    # kitchen + waiter
    path("kitchen/", v.kds, name="kds"),
    path("api/kds/", v.kds_data, name="kds-data"),
    path("api/ticket/<int:pk>/<str:action>/", v.ticket_action, name="ticket-action"),
    path("waiter/", v.waiter, name="waiter"),
    path("api/waiter/", v.waiter_data, name="waiter-data"),

    # delivery
    path("delivery/", v.delivery_board, name="delivery"),

    # menu / recipes / ingredients / waste
    path("menu/", v.menu_list, name="menu"),
    path("menu/new/", v.menu_edit, name="menu-new"),
    path("menu/<int:pk>/", v.menu_edit, name="menu-edit"),
    path("menu/<int:pk>/recipe/", v.recipe_edit, name="recipe"),
    path("menu/recost/", v.recost, name="recost"),
    path("ingredients/", v.ingredient_list, name="ingredients"),
    path("ingredients/new/", v.ingredient_edit, name="ingredient-new"),
    path("ingredients/<int:pk>/", v.ingredient_edit, name="ingredient-edit"),
    path("waste/", v.waste, name="waste"),

    # money
    path("shift/", v.shift_page, name="shift"),
    path("shifts/", v.shifts_history, name="shifts"),

    # setup
    path("settings/", v.settings_page, name="settings"),
    path("reports/", v.reports, name="reports"),
    path("setup/<str:key>/", v.crud_list, name="crud"),
    path("setup/<str:key>/new/", v.crud_edit, name="crud-new"),
    path("setup/<str:key>/<int:pk>/", v.crud_edit, name="crud-edit"),

    # public online-ordering API (API key, no login)
    path("api/v1/menu/", api_views.menu, name="api-menu"),
    path("api/v1/orders/", api_views.orders, name="api-orders"),
    path("api/v1/orders/<str:ref>/", api_views.order_status, name="api-order-status"),
]
