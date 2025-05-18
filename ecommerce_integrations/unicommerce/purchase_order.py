import frappe
from frappe.utils import now_datetime
from ecommerce_integrations.unicommerce.api_client import UnicommerceAPIClient
from ecommerce_integrations.unicommerce.constants import SETTINGS_DOCTYPE


def sync_purchase_orders():
	settings = frappe.get_cached_doc(SETTINGS_DOCTYPE)
	client = UnicommerceAPIClient()

	for mapping in settings.get("warehouse_mapping", []):
		if not mapping.enabled:
			continue

		facility_code = mapping.unicommerce_facility_code
		target_warehouse = mapping.erpnext_warehouse
		last_sync = mapping.get("last_po_sync_at") or "2024-01-01 00:00:00"
		updated_since = _minutes_since(last_sync)

		po_codes = client.search_purchase_orders(
			facility_code=facility_code, updated_since=updated_since
		)

		if not po_codes:
			continue

		for po_code in po_codes:
			po_data = client.get_purchase_order_details(po_code, facility_code)

			if not po_data:
				continue

			try:
				create_material_request(po_data, target_warehouse)
			except Exception as e:
				frappe.log_error(frappe.get_traceback(), f"Failed to create MR for {po_code}")

		# update last sync time
		frappe.db.set_value(
			"Unicommerce Warehouses",
			mapping.name,
			"last_po_sync_at",
			now_datetime()
		)


def _minutes_since(last_sync_time):
	from frappe.utils import get_datetime, now_datetime
	diff = now_datetime() - get_datetime(last_sync_time)
	return int(diff.total_seconds() / 60)


def create_material_request(po_data, target_warehouse):
	from frappe.utils import nowdate
	mr = frappe.new_doc("Material Request")
	mr.material_request_type = "Material Transfer"
	mr.company = frappe.defaults.get_user_default("Company")
	mr.set_warehouse = target_warehouse
	mr.set_from_warehouse = "Kiwi Kisan Window Factory - KKW"
	mr.transaction_date = nowdate()
	mr.schedule_date = nowdate()

	for item in po_data.get("purchaseOrderItems", []):
		mr.append("items", {
			"item_code": item["itemSKU"],
			"qty": item["quantity"],
			"rate": item["unitPrice"],
			"schedule_date": nowdate()
		})

	mr.insert(ignore_permissions=True)

	# Add comment for metadata
	mr.add_comment("Comment", f"PO Code: {po_data.get('code')}, Vendor: {po_data.get('vendorName')}")
