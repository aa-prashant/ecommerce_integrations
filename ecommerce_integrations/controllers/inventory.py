import frappe
from frappe import _dict
from frappe.query_builder import DocType
from frappe.query_builder.functions import Max, Sum
from frappe.utils import now
from frappe.utils.nestedset import get_descendants_of


def get_inventory_levels(warehouses: tuple[str], integration: str) -> list[_dict]:
	"""
	Get list of dict containing items for which the inventory needs to be updated on Integeration.

	New inventory levels are identified by checking Bin modification timestamp,
	so ensure that if you sync the inventory with integration, you have also
	updated `inventory_synced_on` field in related Ecommerce Item.

	returns: list of _dict containing ecom_item, item_code, integration_item_code, variant_id, actual_qty, warehouse, reserved_qty
	"""
	EcommerceItem = DocType("Ecommerce Item")
	Bin = DocType("Bin")

	query = (
		frappe.qb.from_(EcommerceItem)
		.join(Bin)
		.on(EcommerceItem.erpnext_item_code == Bin.item_code)
		.select(
			EcommerceItem.name.as_("ecom_item"),
			Bin.item_code.as_("item_code"),
			EcommerceItem.integration_item_code,
			EcommerceItem.variant_id,
			Bin.actual_qty,
			Bin.warehouse,
			Bin.reserved_qty,
		)
		.where(
			(Bin.warehouse.isin(warehouses))
			& (Bin.modified > EcommerceItem.inventory_synced_on)
			& (EcommerceItem.integration == integration)
		)
	)

	return query.run(as_dict=1)


def get_inventory_levels_of_group_warehouse(warehouse: str, integration: str):
	"""Get updated inventory for a single group warehouse.

	If warehouse mapping is done to a group warehouse then consolidation of all
	leaf warehouses is required"""

	child_warehouse = get_descendants_of("Warehouse", warehouse)
	all_warehouses = (*tuple(child_warehouse), warehouse)

	EcommerceItem = DocType("Ecommerce Item")
	Bin = DocType("Bin")

	query = (
		frappe.qb.from_(EcommerceItem)
		.join(Bin)
		.on(EcommerceItem.erpnext_item_code == Bin.item_code)
		.select(
			EcommerceItem.name.as_("ecom_item"),
			Bin.item_code.as_("item_code"),
			EcommerceItem.integration_item_code,
			EcommerceItem.variant_id,
			Sum(Bin.actual_qty).as_("actual_qty"),
			Sum(Bin.reserved_qty).as_("reserved_qty"),
			Max(Bin.modified).as_("last_updated"),
			Max(EcommerceItem.inventory_synced_on).as_("last_synced"),
		)
		.where((Bin.warehouse.isin(all_warehouses)) & (EcommerceItem.integration == integration))
		.groupby(EcommerceItem.erpnext_item_code)
		.having(Max(Bin.modified) > Max(EcommerceItem.inventory_synced_on))
	)

	data = query.run(as_dict=1)

	# add warehouse as group warehouse for sending to integrations
	for item in data:
		item.warehouse = warehouse

	return data


def update_inventory_sync_status(ecommerce_item, time=None):
	"""Update `inventory_synced_on` timestamp to specified time or current time (if not specified).

	After updating inventory levels to any integration, the Ecommerce Item should know about when it was last updated.
	"""
	if time is None:
		time = now()

	frappe.db.set_value("Ecommerce Item", ecommerce_item, "inventory_synced_on", time)

def get_batchwise_inventory_levels(warehouse: str, integration: str) -> list[dict]:
	from frappe.query_builder import DocType
	from frappe.query_builder.functions import Sum

	Batch = DocType("Batch")
	Bin = DocType("Bin")
	Ecom = DocType("Ecommerce Item")

	query = (
		frappe.qb.from_(Batch)
		.join(Bin).on(Batch.item == Bin.item_code)
		.join(Ecom).on(Ecom.erpnext_item_code == Bin.item_code)
		.select(
			Batch.item.as_("item_code"),
			Batch.name.as_("batch_no"),
			Sum(Bin.actual_qty).as_("actual_qty"),
			Ecom.integration_item_code,
			Ecom.name.as_("ecom_item"),
		)
		.where(
			(Bin.warehouse == warehouse) &
			(Ecom.integration == integration)
		)
		.groupby(Batch.name)
	)

	return query.run(as_dict=1)


def get_batchwise_inventory_levels_of_group_warehouse(warehouse: str, integration: str) -> list[dict]:
	"""Get accurate batch-wise inventory for a group warehouse (SLE + Serial and Batch Entry)."""

	from frappe.query_builder import DocType
	from frappe.query_builder.functions import Sum

	child_warehouses = get_descendants_of("Warehouse", warehouse)
	all_warehouses = (*tuple(child_warehouses), warehouse)

	SLE = DocType("Stock Ledger Entry")
	EI = DocType("Ecommerce Item")
	SBB = DocType("Serial and Batch Bundle")
	SBE = DocType("Serial and Batch Entry")

	# Part A: Direct batch_no from SLE
	part_a_query = (
		frappe.qb.from_(SLE)
		.join(EI).on(SLE.item_code == EI.erpnext_item_code)
		.select(
			SLE.item_code,
			SLE.batch_no,
			SLE.warehouse,
			EI.integration_item_code,
			EI.name.as_("ecom_item"),
			Sum(SLE.actual_qty).as_("actual_qty")
		)
		.where(
			(SLE.docstatus < 2) &
			(SLE.is_cancelled == 0) &
			(SLE.batch_no.isnotnull()) &
			(SLE.batch_no != "") &
			(SLE.warehouse.isin(all_warehouses)) &
			(EI.integration == integration)
		)
		.groupby(SLE.item_code, SLE.batch_no, SLE.warehouse)
	)

	part_a = part_a_query.run(as_dict=True)

	# Part B: Batch info from Serial and Batch Entry
	part_b_query = (
		frappe.qb.from_(SLE)
		.inner_join(SBB).on(SBB.name == SLE.serial_and_batch_bundle)
		.inner_join(SBE).on(SBE.parent == SBB.name)
		.join(EI).on(SLE.item_code == EI.erpnext_item_code)
		.select(
			SLE.item_code,
			SBE.batch_no,
			SBE.warehouse,
			EI.integration_item_code,
			EI.name.as_("ecom_item"),
			Sum(SBE.qty).as_("actual_qty")
		)
		.where(
			(SLE.docstatus < 2) &
			(SLE.is_cancelled == 0) &
			(SLE.has_batch_no == 1) &
			(SBE.batch_no.isnotnull()) &
			(SBE.batch_no != "") &
			(SBE.warehouse.isin(all_warehouses)) &
			(EI.integration == integration)
		)
		.groupby(SLE.item_code, SBE.batch_no, SBE.warehouse)
	)

	part_b = part_b_query.run(as_dict=True)

	# Combine both datasets
	data = part_a + part_b

	# Override child warehouse with group warehouse name
	for row in data:
		row["warehouse"] = warehouse

	return data
