from collections import defaultdict

import frappe
from frappe.utils import cint, now

from ecommerce_integrations.controllers.inventory import (
	get_inventory_levels,
	get_inventory_levels_of_group_warehouse,
	update_inventory_sync_status,
	get_batchwise_inventory_levels,
	get_batchwise_inventory_levels_of_group_warehouse,
)
from ecommerce_integrations.controllers.scheduling import need_to_run
from ecommerce_integrations.unicommerce.api_client import UnicommerceAPIClient
from ecommerce_integrations.unicommerce.constants import MODULE_NAME, SETTINGS_DOCTYPE

# Note: Undocumented but currently handles ~1000 inventory changes in one request.
# Remaining to be done in next interval.
MAX_INVENTORY_UPDATE_IN_REQUEST = 1000


def update_inventory_on_unicommerce(client=None, force=False):
	"""Update ERPnext warehouse wise inventory to Unicommerce.

	This function gets called by scheduler every minute. The function
	decides whether to run or not based on configured sync frequency.

	force=True ignores the set frequency.
	"""
	try:
		settings = frappe.get_cached_doc(SETTINGS_DOCTYPE)

		if not settings.is_enabled() or not settings.enable_inventory_sync:
			frappe.log_error("Unicommerce Inventory Sync Skipped", "Integration disabled in settings.")
			return

		if not force and not need_to_run(SETTINGS_DOCTYPE, "inventory_sync_frequency", "last_inventory_sync"):
			frappe.log_error("Unicommerce Inventory Sync Skipped", "Sync frequency condition not met.")
			return

		warehouses = settings.get_erpnext_warehouses()
		wh_to_facility_map = settings.get_erpnext_to_integration_wh_mapping()

		if client is None:
			client = UnicommerceAPIClient()

		success_map: dict[str, bool] = defaultdict(lambda: True)
		inventory_synced_on = now()

		for warehouse in warehouses:
			frappe.log_error("Syncing Inventory", f"Warehouse: {warehouse}")

			is_group_warehouse = cint(frappe.db.get_value("Warehouse", warehouse, "is_group"))

			if is_group_warehouse:
				batchwise_data = get_batchwise_inventory_levels_of_group_warehouse(warehouse, MODULE_NAME)
			else:
				batchwise_data = get_batchwise_inventory_levels(warehouse, MODULE_NAME)

			if not batchwise_data:
				frappe.log_error("No Inventory Data", f"No batch-wise inventory for warehouse: {warehouse}")
				continue

			inventory_data = batchwise_data[:MAX_INVENTORY_UPDATE_IN_REQUEST]
			facility_code = wh_to_facility_map.get(warehouse)

			if not facility_code:
				frappe.log_error("Missing Facility Mapping", f"Warehouse '{warehouse}' has no facility mapping.")
				continue

			frappe.log_error("Pushing Inventory", f"Pushing {len(inventory_data)} records to Unicommerce (Facility: {facility_code})")

			response, status = client.bulk_inventory_update(
				facility_code=facility_code,
				inventory_data=inventory_data
			)

			if not status:
				frappe.log_error("Unicommerce Inventory Update Failed", f"Warehouse: {warehouse}, Response: {frappe.as_json(response)}")
			else:
				for row in inventory_data:
					ecom_item = row["ecom_item"]
					sku = row["integration_item_code"]
					success = response.get(sku, False)
					success_map[ecom_item] = success_map[ecom_item] and success
					frappe.log_error(
						"Inventory Sync Result",
						f"SKU: {sku}, Ecom Item: {ecom_item}, Batch: {row.get('batch_no')}, Qty: {row.get('actual_qty')}, Success: {success}"
					)

		_update_inventory_sync_status(success_map, inventory_synced_on)
		frappe.log_error("Inventory Sync Completed", f"Timestamp: {inventory_synced_on}")

	except Exception:
		frappe.log_error("Unicommerce Inventory Sync - Fatal Error", frappe.get_traceback())



def _update_inventory_sync_status(ecom_item_success_map: dict[str, bool], timestamp: str) -> None:
	for ecom_item, status in ecom_item_success_map.items():
		if status:
			update_inventory_sync_status(ecom_item, timestamp)
