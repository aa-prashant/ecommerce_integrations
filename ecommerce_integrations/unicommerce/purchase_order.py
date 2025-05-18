import frappe
from frappe.utils import now_datetime, nowdate, get_datetime
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

		# ⏰ Get datetime window
		start = get_datetime(last_sync)
		end = now_datetime()

		# 🔍 Search Purchase Orders
		po_codes = client.search_purchase_orders(
			facility_code=facility_code,
			start_datetime=start,
			end_datetime=end
		)

		if not po_codes:
			continue

		for po_code in po_codes:
			po_data = client.get_purchase_order_details(po_code, facility_code)

			if not po_data:
				continue

			try:
				create_material_request(po_data, target_warehouse, facility_code)
			except Exception:
				frappe.log_error(frappe.get_traceback(), f"❌ Failed to create Material Request for PO {po_code}")

		# ✅ Update last sync timestamp
		frappe.db.set_value(
			"Unicommerce Warehouses",
			mapping.name,
			"last_po_sync_at",
			end
		)


def create_material_request(po_data, target_warehouse, facility_code):
	settings = frappe.get_cached_doc(SETTINGS_DOCTYPE)

	mr = frappe.new_doc("Material Request")
	mr.naming_series = settings.default_material_request_series or "MAT-MR-.YYYY.-"
	mr.material_request_type = "Material Transfer"
	mr.company = frappe.defaults.get_user_default("Company")
	mr.set_warehouse = target_warehouse
	mr.set_from_warehouse = settings.default_source_warehouse
	mr.transaction_date = nowdate()
	mr.schedule_date = nowdate()
	mr.custom_unicommerce_purchase_order = po_data.get("code")

	for item in po_data.get("purchaseOrderItems", []):
		mr.append("items", {
			"item_code": item["itemSKU"],
			"qty": item["quantity"],
			"rate": item["unitPrice"],
			"schedule_date": nowdate(),
			"warehouse": target_warehouse
		})

	mr.insert(ignore_permissions=True)

	mr.add_comment("Comment", f"📦 PO Code: {po_data.get('code')}, Vendor: {po_data.get('vendorName')}, Facility: {facility_code}, Created By: {po_data.get('createdBy')}")
