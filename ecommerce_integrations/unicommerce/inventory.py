from collections import defaultdict

import frappe
from frappe.utils import cint, now

from ecommerce_integrations.controllers.inventory import (
	get_inventory_levels,
	get_inventory_levels_of_group_warehouse,
	update_inventory_sync_status,
	get_batchwise_inventory_levels,
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
	settings = frappe.get_cached_doc(SETTINGS_DOCTYPE)

	if not settings.is_enabled() or not settings.enable_inventory_sync:
		return

	# check if need to run based on configured sync frequency
	if not force and not need_to_run(SETTINGS_DOCTYPE, "inventory_sync_frequency", "last_inventory_sync"):
		return

	# get configured warehouses
	warehouses = settings.get_erpnext_warehouses()
	wh_to_facility_map = settings.get_erpnext_to_integration_wh_mapping()

	if client is None:
		client = UnicommerceAPIClient()

	# track which ecommerce item was updated successfully
	success_map: dict[str, bool] = defaultdict(lambda: True)
	inventory_synced_on = now()

	for warehouse in warehouses:
		batchwise_data = get_batchwise_inventory_levels(warehouse, MODULE_NAME)
		if not batchwise_data:
			continue

		inventory_data = batchwise_data[:MAX_INVENTORY_UPDATE_IN_REQUEST]
		facility_code = wh_to_facility_map[warehouse]

		response, status = client.bulk_inventory_update(
			facility_code=facility_code,
			inventory_data=inventory_data
		)

		if status:
			for row in inventory_data:
				ecom_item = row["ecom_item"]
				sku = row["integration_item_code"]
				success = response.get(sku, False)
				success_map[ecom_item] = success_map[ecom_item] and success


	_update_inventory_sync_status(success_map, inventory_synced_on)


def _update_inventory_sync_status(ecom_item_success_map: dict[str, bool], timestamp: str) -> None:
	for ecom_item, status in ecom_item_success_map.items():
		if status:
			update_inventory_sync_status(ecom_item, timestamp)
