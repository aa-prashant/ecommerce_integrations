frappe.ui.form.on('Material Request', {
    onload(frm) {
        if (frm.doc.custom_unicommerce_purchase_order && frm.doc.set_from_warehouse) {
            frm.set_intro(
                `📦 This Material Request is generated from Unicommerce (PO: ${frm.doc.custom_unicommerce_purchase_order}). Please plan accordingly. Source Warehouse: ${frm.doc.set_from_warehouse}.`
            );
        }
    },

    refresh(frm) {
        if (frm.doc.docstatus === 1 && frm.doc.custom_unicommerce_purchase_order) {
            frm.add_custom_button("Create Internal Delivery Note", () => {
                frappe.model.open_mapped_doc({
                    method: "ecommerce_integrations.unicommerce.purchase_order.map_mr_to_dn",
                    frm: frm
                });
            });
        }
    }
});
