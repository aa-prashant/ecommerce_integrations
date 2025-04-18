from dataclasses import dataclass

import frappe
import json
from erpnext.stock.doctype.batch.batch import Batch
from frappe import _
from frappe.utils import cint, getdate
from frappe.utils.csvutils import UnicodeWriter
from frappe.utils.file_manager import save_file
from frappe.utils import now

from ecommerce_integrations.unicommerce.api_client import UnicommerceAPIClient
from ecommerce_integrations.unicommerce.constants import (
	GRN_STOCK_ENTRY_TYPE,
	MODULE_NAME,
	SETTINGS_DOCTYPE,
)
from ecommerce_integrations.unicommerce.utils import remove_non_alphanumeric_chars

CSV_HEADER_LINE = (
	"Vendor Code*,Vendor Invoice Number*,Purchase Order Code,Vendor Invoice Date*,Sku"
	" Code*,Qty*,Item Code,Item Details,Shelf Code,MRP,Unit Price,Manufacturing Date,Expiry date"
	" as dd/MM/yyyy,Vendor Batch Number\r\n"
)


@dataclass
class GRNItemRow:
	vendor_code: str
	vendor_invoice_number: str
	invoice_date: str
	sku: str
	qty: int
	item_code: str
	purchase_order: str = ""
	manufacturing_date: str = ""
	expiry_date: str = ""
	batch_number: str = ""
	shelf_code: str = ""
	item_details: str = ""
	mrp: str = 0.0
	unit_price: str = 0.0

	def get_ordered_fields(self):
		return [
			self.vendor_code,
			self.vendor_invoice_number,
			self.purchase_order,
			self.invoice_date,
			self.sku,
			self.qty,
			self.item_code,
			self.item_details,
			self.shelf_code,
			self.mrp,
			self.unit_price,
			self.manufacturing_date,
			self.expiry_date,
			self.batch_number,
		]


def is_unicommerce_grn(stock_entry) -> bool:
	if stock_entry.stock_entry_type != GRN_STOCK_ENTRY_TYPE:
		return False

	grn_enabled = frappe.db.get_single_value(SETTINGS_DOCTYPE, "use_stock_entry_for_grn")
	if not grn_enabled:
		frappe.throw(
			_("Auto GRN not enabled in Unicommerce settings. Can not use Stock Entry Type: {}").format(
				GRN_STOCK_ENTRY_TYPE
			)
		)
	return True


def validate_stock_entry_for_grn(doc, method=None):
	stock_entry = doc
	if not is_unicommerce_grn(stock_entry):
		return

	settings = frappe.get_doc(SETTINGS_DOCTYPE)

	if not settings.is_enabled():
		return

	get_facility_code(stock_entry, settings)


def get_facility_code(stock_entry, unicommerce_settings) -> str:
	"""Validate that facility has single warehouse and return facility code."""

	target_warehouses = {d.t_warehouse for d in stock_entry.items}
	if len(target_warehouses) > 1:
		frappe.throw(
			_("{} only supports one target warehouse (unicommerce facility)").format(GRN_STOCK_ENTRY_TYPE)
		)

	warehouse = next(iter(target_warehouses))
	warehouse_mapping = unicommerce_settings.get_erpnext_to_integration_wh_mapping(all_wh=True)

	facility = warehouse_mapping.get(warehouse)
	if not facility:
		msg = _("{} warehouse does not have Unicommerce facilities mapped to it.").format(warehouse)
		frappe.throw(msg, title="Unmapped Unicommerce Facility")

	return facility


def upload_grn(doc, method=None):
	stock_entry = doc
	if not is_unicommerce_grn(stock_entry):
		return

	settings = frappe.get_doc(SETTINGS_DOCTYPE)
	facility_code = get_facility_code(stock_entry, settings)
	csv_file = _prepare_grn_import_csv(doc)

	response = create_auto_grn_import(csv_file, facility_code=facility_code)

	if not response or not response.successful:
		frappe.throw(
			_("GRN upload failed, Unicommerce reported errors.<br>{}").format(
				"<br>".join(response.errors if response else [])
			)
		)

	errors = response.errors
	if response.successful and not errors:
		msg = _("Successully queued GRN import to Unicommerce.")
		msg += _("Confirm the status on Import Log in Uniware.")
		frappe.msgprint(msg, title="Success")
	elif response.successful and errors:
		frappe.msgprint("Partial success, unicommerce reported errors:<br>{}".format("<br>".join(errors)))


def _prepare_grn_import_csv(stock_entry) -> str:
	"""Prepare CSV file in Unicommerce auto grn api format and attach it to Stock Entry
	returns: filename of generated csv.
	"""

	rows = []
	vendor_code = frappe.db.get_single_value(SETTINGS_DOCTYPE, "vendor_code")

	for item in stock_entry.items:
		price = frappe.db.get_value("Item", item.item_code, "standard_rate") or ""
		invoice_date = _get_unicommerce_format_date(stock_entry.posting_date)

		batch_details = frappe.db.get_value(
			"Batch", item.batch_no, fieldname=["manufacturing_date", "expiry_date"], as_dict=True
		)
		manufacturing_date = _get_unicommerce_format_date(
			batch_details.manufacturing_date if batch_details else getdate()
		)
		expiry_date = _get_unicommerce_format_date(
			batch_details.expiry_date if batch_details else getdate("2099-01-01")
		)

		sku = frappe.db.get_value(
			"Ecommerce Item",
			{"erpnext_item_code": item.item_code, "integration": MODULE_NAME},
			"integration_item_code",
		)
		if not sku:
			frappe.throw(_("Item {} does not have associated Unicommerce SKU.").format(item.item_code))

		row = GRNItemRow(
			vendor_code=vendor_code,
			vendor_invoice_number=stock_entry.name,
			invoice_date=invoice_date,
			sku=sku,
			qty=cint(item.qty),  # implicitly round down
			item_code=sku,
			manufacturing_date=manufacturing_date,
			expiry_date=expiry_date,
			batch_number=item.batch_no,
			mrp=price,
			unit_price=price,
		)
		rows.append(row)

	file_name = remove_non_alphanumeric_chars(stock_entry.name)
	file = save_file(
		fname=f"GRN-{file_name}.csv",
		content=_get_csv_content(rows),
		dt=stock_entry.doctype,
		dn=stock_entry.name,
	)
	return file.file_name


def _get_csv_content(rows: list[GRNItemRow]) -> bytes:
	writer = UnicodeWriter()

	for row in rows:
		writer.writerow(row.get_ordered_fields())

	csv_content = CSV_HEADER_LINE + writer.getvalue()
	return csv_content.encode("utf-8")


def _get_unicommerce_format_date(date) -> str:
	if date:
		return getdate(date).strftime("%d/%m/%Y")
	return ""


def create_auto_grn_import(csv_filename: str, facility_code: str, client=None):
	"""Create new import job for Auto GRN items"""
	if client is None:
		client = UnicommerceAPIClient()
	resp = client.create_import_job(
		job_name="Auto GRN Items", csv_filename=csv_filename, facility_code=facility_code
	)
	return resp


def prevent_grn_cancel(doc, method=None):
	if not is_unicommerce_grn(doc):
		return

	msg = _("This Stock Entry can not be cancelled.")
	msg += _("To undo this stock entry you need to move the Stock back") + " "
	msg += _("and remove stock from Unicommerce.")

	frappe.throw(msg, title="GRN Stock Entry can not be cancelled")





from frappe.utils import now

def sync_unicommerce_internal_receipts():
	"""Background Job: Sync Unicommerce GRNs and create Internal Purchase Receipts"""

	client = UnicommerceAPIClient()
	child_table_fieldname = "custom_unicommerce_grn"

	# 1. Fetch all eligible Delivery Notes
	delivery_notes = frappe.get_all(
		"Delivery Note",
		filters={
			"docstatus": 1,
			"custom_is_internal_transfer": 1,
			"custom_reference_no": ["!=", ""],
			"custom_unicommerce_grn_status": ["not in", ["GRN Completed"]]
		},
		fields=["name", "custom_reference_no"]
	)

	if not delivery_notes:
		frappe.log_error("Unicommerce GRN Sync", "No delivery notes found for syncing.")
		return

	for dn in delivery_notes:
		dn_doc = frappe.get_doc("Delivery Note", dn.name)

		try:
			# Log which DN is being processed
			frappe.log_error(
				title="Processing Delivery Note",
				message=f"Delivery Note: {dn.name}, Reference No: {dn.custom_reference_no}"
			)

			# 2. Fetch GRNs for this PO
			payload = {"purchaseOrderCode": dn.custom_reference_no}
			response, status = client.request(
				endpoint="/services/rest/v1/purchase/inflowReceipt/getInflowReceipts",
				method="POST",
				headers={"facility": "kiwikisan"},
				body=payload
			)

			if not status or not response.get("successful"):
				frappe.log_error(
					title="Unicommerce GRN Fetch Failed",
					message=f"Delivery Note: {dn.name}, Error: {response}"
				)
				continue

			grn_codes = response.get("inflowReceiptCodes", [])

			# Log fetched GRNs
			frappe.log_error(
				title="Fetched GRNs",
				message=json.dumps({
					"delivery_note": dn.name,
					"reference_no": dn.custom_reference_no,
					"grn_codes": grn_codes
				}, indent=2)
			)

			# 3. Insert any missing GRNs into child table
			existing_grns = {row.grn_code for row in (dn_doc.get(child_table_fieldname) or [])}
			new_grns = set(grn_codes) - existing_grns

			for grn_code in new_grns:
				dn_doc.append(child_table_fieldname, {
					"grn_code": grn_code,
					"status": "Pending",
					"last_checked_on": now()
				})

			dn_doc.save(ignore_permissions=True)

			# 4. Process each GRN
			for grn_row in (dn_doc.get(child_table_fieldname) or []):
				if grn_row.status not in ["Pending", "Error"]:
					continue

				try:
					# Fetch GRN Details
					payload = {"inflowReceiptCode": grn_row.grn_code}
					grn_response, grn_status = client.request(
						endpoint="/services/rest/v1/purchase/inflowReceipt/getInflowReceipt",
						method="POST",
						headers={"facility": "kiwikisan"},
						body=payload
					)

					if not grn_status or not grn_response.get("successful"):
						grn_row.status = "Error"
						grn_row.last_checked_on = now()
						continue

					inflow_receipt = grn_response.get("inflowReceipt", {})

					# Log fetched inflowReceipt
					frappe.log_error(
						title="Fetched InflowReceipt",
						message=json.dumps({
							"delivery_note": dn.name,
							"grn_code": grn_row.grn_code,
							"inflow_receipt": inflow_receipt
						}, indent=2)
					)

					# Check Header Status
					if inflow_receipt.get("statusCode") != "QC_COMPLETE":
						grn_row.status = "Pending"
						grn_row.last_checked_on = now()
						continue

					inflow_items = inflow_receipt.get("inflowReceiptItems") or []
					# Check Items Status
					if not inflow_items or any(item.get("status") != "QC_COMPLETE" for item in inflow_items):
						grn_row.status = "Pending"
						grn_row.last_checked_on = now()
						continue

					# Log Decision
					frappe.log_error(
						title="GRN Processing Decision",
						message=json.dumps({
							"delivery_note": dn.name,
							"grn_code": grn_row.grn_code,
							"header_status": inflow_receipt.get("statusCode"),
							"items_status": [
								{
									"item_sku": item.get("itemSKU"),
									"item_status": item.get("status")
								}
								for item in inflow_items
							],
							"final_decision": "Proceed"
						}, indent=2)
					)

					# Create Internal Purchase Receipt (Draft)
					pr_doc = frappe.get_doc(
						erpnext.stock.doctype.delivery_note.delivery_note.make_inter_company_transaction(
							"Delivery Note", dn.name
						)
					)

					# Update PR fields from GRN
					pr_doc.supplier_invoice_no = inflow_receipt.get("vendorInvoiceNumber")
					pr_doc.supplier_invoice_date = inflow_receipt.get("vendorInvoiceDate")

					# Map batches by SKU
					sku_to_batch = {}
					for item in inflow_items:
						if item.get("batchDTO") and item["batchDTO"].get("batchFieldsDTO"):
							vendor_batch_no = item["batchDTO"]["batchFieldsDTO"].get("vendorBatchNumber")
							sku_to_batch[item["itemSKU"]] = vendor_batch_no

					for pr_item in pr_doc.items:
						batch_no = sku_to_batch.get(pr_item.item_code)
						if batch_no:
							pr_item.batch_no = batch_no

					# Save PR as Draft
					pr_doc.save(ignore_permissions=True)

					# Update GRN Tracking Row
					grn_row.purchase_receipt = pr_doc.name
					grn_row.status = "PR Created"
					grn_row.last_checked_on = now()

					frappe.log_error(
						title="Unicommerce GRN → PR Created",
						message=f"Delivery Note: {dn.name}, GRN: {grn_row.grn_code}, PR: {pr_doc.name}"
					)

				except Exception:
					grn_row.status = "Error"
					grn_row.last_checked_on = now()
					frappe.log_error(
						title="Unicommerce GRN Processing Error",
						message=frappe.get_traceback()
					)

			# Save Delivery Note after processing all GRNs
			dn_doc.save(ignore_permissions=True)

			# 5. After processing GRNs, check if PO fully GRNed
			payload = {"purchaseOrderCode": dn.custom_reference_no}
			po_response, po_status = client.request(
				endpoint="/services/rest/v1/purchase/purchaseOrder/getPurchaseOrderDetails",
				method="POST",
				headers={"facility": "kiwikisan"},
				body=payload
			)

			if po_status and po_response.get("successful"):
				purchase_order = po_response
				all_items = purchase_order.get("purchaseOrderItems", [])
				pending_qty = sum(item.get("pendingQuantity", 0) for item in all_items)

				if pending_qty == 0:
					dn_doc.custom_unicommerce_grn_status = "GRN Completed"
					dn_doc.save(ignore_permissions=True)

					frappe.log_error(
						title="Unicommerce PO Fully GRNed",
						message=f"Delivery Note {dn.name} marked GRN Completed"
					)

		except Exception:
			frappe.log_error(
				title="Unicommerce Full Sync Error",
				message=frappe.get_traceback()
		 )
