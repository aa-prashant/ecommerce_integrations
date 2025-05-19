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
		start = get_datetime(last_sync).isoformat() + "Z"
		end = now_datetime().isoformat() + "Z"

		# 🔍 Search Purchase Orders
		po_codes = client.search_purchase_orders(
			facility_code=facility_code,
			start_date=start,
			end_date=end
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
			now_datetime()
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

	# Add metadata as comment
	mr.add_comment(
		"Comment",
		f"📦 PO Code: {po_data.get('code')}, Vendor: {po_data.get('vendorName')}, "
		f"Facility: {facility_code}, Created By: {po_data.get('createdBy')}"
	)


@frappe.whitelist()
def map_mr_to_dn(source_name, target_doc=None):
	from frappe.model.mapper import get_mapped_doc
	from ecommerce_integrations.unicommerce.constants import SETTINGS_DOCTYPE

	def set_missing_values(source, target):
		settings = frappe.get_cached_doc(SETTINGS_DOCTYPE)

		target.custom_reference_no = source.custom_unicommerce_purchase_order
		target.set_from_warehouse = source.set_from_warehouse
		target.set_warehouse = settings.default_in_transit_warehouse
		target.customer = frappe.db.get_single_value("Selling Settings", "customer")
		target.company = source.company

	return get_mapped_doc(
		"Material Request",
		source_name,
		{
			"Material Request": {
				"doctype": "Delivery Note",
				"field_map": {
					"transaction_date": "posting_date",
				},
				"validation": {
					"docstatus": ["=", 1]
				}
			},
			"Material Request Item": {
				"doctype": "Delivery Note Item",
				"field_map": {
					"name": "material_request_item",
					"parent": "material_request"
				},
				"add_if_empty": True
			}
		},
		target_doc,
		set_missing_values
	)
