import base64
from typing import Any

import frappe
import requests
from frappe import _
from frappe.utils import cint, cstr, get_datetime
from pytz import timezone
import json

from ecommerce_integrations.unicommerce.constants import SETTINGS_DOCTYPE
from ecommerce_integrations.unicommerce.utils import create_unicommerce_log

JsonDict = dict[str, Any]


class UnicommerceAPIClient:
	"""Wrapper around Unicommerce REST API

	API docs: https://documentation.unicommerce.com/
	"""

	def __init__(
		self,
		url: str | None = None,
		access_token: str | None = None,
	):
		self.settings = frappe.get_doc(SETTINGS_DOCTYPE)
		self.base_url = url or f"https://{self.settings.unicommerce_site}"
		self.access_token = access_token
		self.__initialize_auth()

	def __initialize_auth(self):
		"""Initialize and setup authentication details"""
		if not self.access_token:
			self.settings.renew_tokens()
			self.access_token = self.settings.get_password("access_token")

		self._auth_headers = {"Authorization": f"Bearer {self.access_token}"}

	def request(
		self,
		endpoint: str,
		method: str = "POST",
		headers: JsonDict | None = None,
		body: JsonDict | None = None,
		params: JsonDict | None = None,
		files: JsonDict | None = None,
		log_error=True,
	) -> tuple[JsonDict, bool]:

		if headers is None:
			headers = {}

		headers.update(self._auth_headers)

		url = self.base_url + endpoint

		# ✅ Log outgoing request
		try:
			outgoing_request = {
				"url": url,
				"method": method,
				"headers": headers,
				"params": params,
				"body": body
			}
			frappe.log_error(
				title=f"Unicommerce Outgoing Request - {endpoint}",
				message=json.dumps(outgoing_request, indent=2)
			)
		except Exception:
			pass

		try:
			response = requests.request(
				url=url, method=method, headers=headers, json=body, params=params, files=files
			)
		except Exception as e:
			if log_error:
				create_unicommerce_log(status="Error", make_new=True, message=str(e))
			return None, False

		# ✅ Always try to parse response safely
		try:
			resp_json = response.json()
			status = resp_json.get("successful", True)
		except Exception:
			resp_json = {"raw_text": response.text}
			status = False

		# ✅ Log incoming response
		try:
			frappe.log_error(
				title=f"Unicommerce Response - {endpoint}",
				message=json.dumps({
					"status_code": response.status_code,
					"response_body": resp_json
				}, indent=2)
			)
		except Exception:
			pass

		# ✅ Raise error if necessary
		if not response.ok:
			if log_error:
				create_unicommerce_log(status="Error", response_data=resp_json, make_new=True)
			return resp_json, False

		return resp_json, status


	def get_unicommerce_item(self, sku: str, log_error=True) -> JsonDict | None:
		"""Get Unicommerce item data for specified SKU code.

		ref: https://documentation.unicommerce.com/docs/itemtype-get.html
		"""
		item, status = self.request(
			endpoint="/services/rest/v1/catalog/itemType/get", body={"skuCode": sku}, log_error=log_error
		)
		if status:
			return item

	def create_update_item(self, item_dict: JsonDict, update=False) -> tuple[JsonDict, bool]:
		"""Create/update item on unicommerce."""
		endpoint = "/services/rest/v1/catalog/itemType/createOrEdit"
		if update:
			endpoint = "/services/rest/v1/catalog/itemType/edit"
		
		# ✅ Log payload separately for bundle
		if item_dict.get("type") == "BUNDLE":
			try:
				frappe.log_error(
					title=f"Bundle Upload Payload - {item_dict.get('skuCode')}",
					message=json.dumps(item_dict, indent=2)
				)
			except Exception:
				pass

		return self.request(endpoint=endpoint, body={"itemType": item_dict})


	def get_sales_order(self, order_code: str) -> JsonDict | None:
		"""Get details for a sales order.

		ref: https://documentation.unicommerce.com/docs/saleorder-get.html
		"""

		order, status = self.request(
			endpoint="/services/rest/v1/oms/saleorder/get", body={"code": order_code}
		)
		if status and "saleOrderDTO" in order:
			return order["saleOrderDTO"]

	def search_sales_order(
		self,
		from_date: str | None = None,
		to_date: str | None = None,
		status: str | None = None,
		channel: str | None = None,
		facility_codes: list[str] | None = None,
		updated_since: int | None = None,
	) -> list[JsonDict] | None:
		"""Search sales order using specified parameters and return search results.

		ref: https://documentation.unicommerce.com/docs/saleorder-search.html
		"""
		body = {
			"status": status,
			"channel": channel,
			"facility_codes": facility_codes,
			"fromDate": _utc_timeformat(from_date) if from_date else None,
			"toDate": _utc_timeformat(to_date) if to_date else None,
			"updatedSinceInMinutes": updated_since,
		}

		# remove None values.
		body = {k: v for k, v in body.items() if v is not None}

		search_results, status = self.request(endpoint="/services/rest/v1/oms/saleOrder/search", body=body)

		if status and "elements" in search_results:
			return search_results["elements"]

	def get_inventory_snapshot(
		self, sku_codes: list[str], facility_code: str, updated_since: int = 1430
	) -> JsonDict | None:
		"""Get current inventory snapshot.

		ref: https://documentation.unicommerce.com/docs/inventory-snapshot.html
		"""

		extra_headers = {"Facility": facility_code}

		body = {"itemTypeSKUs": sku_codes, "updatedSinceInMinutes": updated_since}

		response, status = self.request(
			endpoint="/services/rest/v1/inventory/inventorySnapshot/get",
			headers=extra_headers,
			body=body,
		)

		if status:
			return response

	def convert_to_epoch(self, date_str):
		try:
			return int(get_datetime(date_str).timestamp() * 1000)
		except Exception:
			return int(get_datetime(nowdate()).timestamp() * 1000)  # fallback to today

	def bulk_inventory_update(self, facility_code: str, inventory_data: list[dict]):
		"""Send batch-wise inventory with batchDetails only (no batchCode) as per Unicommerce's latest spec."""

		extra_headers = {"Facility": facility_code}
		inventory_adjustments = []

		for row in inventory_data:
			qty = cint(row.get("actual_qty") or 0)
			if qty < 0:
				qty = 0

			batch_no = row.get("batch_no") or "NO-BATCH"
			mfg_date = row.get("mfg_date") or nowdate()

			adjustment = {
				"itemSKU": row["integration_item_code"],
				"quantity": qty,
				"shelfCode": "DEFAULT",
				"inventoryType": "GOOD_INVENTORY",
				"adjustmentType": "REPLACE",
				"facilityCode": facility_code,
				"batchDetails": {
					"vendorBatchNumber": batch_no,
					"mfd": self.convert_to_epoch(mfg_date)
				}
			}

			inventory_adjustments.append(adjustment)

		payload = {"inventoryAdjustments": inventory_adjustments}

		# 🔁 Make the actual API request
		response, status = self.request(
			endpoint="/services/rest/v1/inventory/adjust/bulk",
			headers=extra_headers,
			body=payload
		)

		# ✅ Log request + response after the call
		frappe.log_error(
			title="Unicommerce Inventory Sync - Request and Response",
			message=json.dumps({
				"facility": facility_code,
				"payload": payload,
				"response": response
			}, indent=2)
		)

		# ✅ Post-process and return
		try:
			item_wise_response = response.get("inventoryAdjustmentResponses", [])
			item_wise_status = {
				item["facilityInventoryAdjustment"]["itemSKU"]: item["successful"]
				for item in item_wise_response
			}
			if False in item_wise_status.values():
				create_unicommerce_log(
					status="Failure",
					response_data=response,
					message="Inventory sync failed for some items",
					make_new=True,
				)
			return item_wise_status, status
		except Exception:
			frappe.log_error("Unicommerce Inventory Sync - Parse Error", frappe.get_traceback())
			return response, False



	def get_sales_invoice(
		self, shipping_package_code: str, facility_code: str, is_return: bool = False
	) -> JsonDict | None:
		"""Get invoice details

		ref: https://documentation.unicommerce.com/docs/invoice-getdetails.html
		"""
		extra_headers = {"Facility": facility_code}
		response, status = self.request(
			endpoint="/services/rest/v1/invoice/details/get",
			body={"shippingPackageCode": shipping_package_code, "return": is_return},
			headers=extra_headers,
		)

		if status:
			return response

	def update_shipping_package(
		self,
		shipping_package_code: str,
		facility_code: str,
		package_type_code: str,
		weight: int = 0,
		length: int = 0,
		width: int = 0,
		height: int = 0,
	):
		"""Update shipping package dimensions and other details on Unicommerce.

		ref: https://documentation.unicommerce.com/docs/shippingpackage-edit.html
		"""

		body = {
			"shippingPackageCode": shipping_package_code,
			"shippingPackageTypeCode": package_type_code,
		}

		def _positive(numbers):
			for number in numbers:
				if cint(number) <= 0:
					return False
			return True

		if _positive([weight]):
			body["actualWeight"] = weight

		if _positive([length, width, height]):
			body["shippingBox"] = {"length": length, "width": width, "height": height}

		extra_headers = {"Facility": facility_code}
		return self.request(
			endpoint="/services/rest/v1/oms/shippingPackage/edit",
			body=body,
			headers=extra_headers,
		)

	def get_invoice_label(self, shipping_package_code: str, facility_code: str) -> str | None:
		"""Get the generated label for a given shipping package.

		ref: undocumented.
		"""
		extra_headers = {"Facility": facility_code}
		pdf, status = self.request(
			endpoint="/services/rest/v1/oms/shipment/show",
			method="GET",
			params={"shippingPackageCodes": shipping_package_code},
			headers=extra_headers,
		)
		if status and pdf:
			return base64.b64encode(pdf)

	def create_and_close_shipping_manifest(
		self,
		channel: str,
		shipping_provider_code: str,
		shipping_method_code: str,
		shipping_packages: list[str],
		facility_code: str,
		third_party_shipping: bool = True,
	):
		"""Create and close the shipping manifest in Unicommerce

		Ref: https://documentation.unicommerce.com/docs/pos-shippingmanifest-create-close.html"""

		# Even though docs dont mention it, facility code is a required header.
		extra_headers = {"Facility": facility_code}
		body = {
			"channel": channel,
			"shippingProviderCode": shipping_provider_code,
			"shippingMethodCode": shipping_method_code,
			"thirdPartyShipping": third_party_shipping,
			"shippingPackageCodes": shipping_packages,
		}

		response, status = self.request(
			endpoint="/services/rest/v1/oms/shippingManifest/createclose",
			body=body,
			headers=extra_headers,
		)

		if status:
			return response

	def get_shipping_manifest(self, shipping_manifest_code, facility_code):
		extra_headers = {"Facility": facility_code}
		response, status = self.request(
			endpoint="/services/rest/v1/oms/shippingManifest/get",
			body={"shippingManifestCode": shipping_manifest_code},
			headers=extra_headers,
		)
		if status:
			return response
	
	def search_purchase_orders(
		self,
		updated_since: int = 1440,  # default 24 hrs
		facility_code: str = None,
		status: str = "COMPLETE"
	) -> list[str] | None:
		"""Search approved POs from Unicommerce by facility.

		Ref: https://documentation.unicommerce.com/docs/purchaseorder_search.html
		"""
		headers = {"Facility": facility_code}
		body = {
			"status": status,
			"updatedSinceInMinutes": updated_since
		}

		response, status = self.request(
			endpoint="/services/rest/v1/oms/purchaseOrder/search",
			headers=headers,
			body=body
		)

		if status and "purchaseOrderCodes" in response:
			return response["purchaseOrderCodes"]
		

	def get_purchase_order_details(self, po_code: str, facility_code: str) -> JsonDict | None:
		"""Get full PO details.

		Ref: https://documentation.unicommerce.com/docs/get_purchase_order_details.html
		"""
		headers = {"Facility": facility_code}
		body = {"code": po_code}

		response, status = self.request(
			endpoint="/services/rest/v1/oms/purchaseOrder/get",
			headers=headers,
			body=body
		)

		if status:
			return response



	def search_shipping_packages(
		self,
		facility_code: str,
		channel: str | None = None,
		statuses: list[str] | None = None,
		updated_since: int | None = 6 * 60,
	):
		"""Search shipping packages on unicommerce matching specified criterias.

		Ref: https://documentation.unicommerce.com/docs/pos-shippingpackage-search.html"""
		body = {
			"statuses": statuses,
			"channelCode": channel,
			"updatedSinceInMinutes": updated_since,
		}
		extra_headers = {"Facility": facility_code}

		# remove None values.
		body = {k: v for k, v in body.items() if v is not None}

		search_results, statuses = self.request(
			endpoint="/services/rest/v1/oms/shippingPackage/search",
			body=body,
			headers=extra_headers,
		)

		if statuses and "elements" in search_results:
			return search_results["elements"]

	def create_import_job(
		self,
		job_name: str,
		csv_filename: str,
		facility_code: str,
		job_type: str = "CREATE_NEW",
	):
		"""Create import job by specifying job name and CSV file

		args:
		        job_name: import job code string specified by unicommerce
		        csv_filename: name of csv file.
		        facility_code: facility where import should happen
		        job_type: create / or update code.
		"""

		url_params = {"name": job_name, "importOption": job_type}

		extra_headers = {
			"Facility": facility_code,
			"cache-control": "no-cache",
		}

		file_obj = _safe_open_csv(csv_filename)
		files = [("file", (csv_filename, file_obj, "text/csv"))]

		response, status = self.request(
			endpoint="/services/rest/v1/data/import/job/create",
			params=url_params,
			files=files,
			headers=extra_headers,
		)

		file_obj.close()
		return response


def _utc_timeformat(datetime) -> str:
	"""Get datetime in UTC/GMT as required by Unicommerce"""
	return get_datetime(datetime).astimezone(timezone("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_open_csv(csv_name):
	from frappe.utils.file_manager import get_file_path

	if csv_name.split(".")[-1].lower().strip() != "csv":
		frappe.throw(_("Only CSV files can be uploaded."))

	filepath = get_file_path(csv_name)
	return open(filepath, "rb")
